import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.settings import cfg
from trading.context import closed_candles, ticker_context, structure_context
from trading.strategy import detect_fvgs, _calc_rsi, _calc_adx, _score_signal, validate_signal, _safe_dynamic_rr
from trading.backtest import metrics, run_backtest


def candles(n=45):
    return pd.DataFrame([dict(timestamp=i*900000, open=100., high=101., low=99., close=100., volume=100.) for i in range(n)])


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.saved = cfg.model_copy(deep=True)
        cfg.timeframe = '15'

    def tearDown(self):
        for name in type(cfg).model_fields:
            setattr(cfg, name, getattr(self.saved, name))

    def zone_data(self):
        d = candles()
        d.loc[39, ["open", "high", "low", "close", "volume"]] = [100, 106, 99.5, 105.5, 300]
        d.loc[40:, ["open", "high", "low", "close"]] = [104, 107, 103, 105]
        d.loc[41:, "low"] = 103.5
        return d

    def test_closed_boundary(self):
        d = candles(3)
        self.assertEqual(len(closed_candles(d, "15", 1800000)), 2)
        self.assertEqual(len(closed_candles(d, "15", 1799999)), 1)

    def test_month_boundary(self):
        d = candles(1)
        d.timestamp = [int(pd.Timestamp("2024-02-01", tz="UTC").timestamp()*1000)]
        self.assertEqual(len(closed_candles(d, "M", int(pd.Timestamp("2024-03-01", tz="UTC").timestamp()*1000))), 1)

    def test_fvg_creation_and_invalidation(self):
        d = self.zone_data()
        zones = detect_fvgs(d)
        self.assertTrue(zones)
        self.assertEqual((zones[0]["bottom"], zones[0]["top"]), (101, 103))
        d.loc[44, "low"] = 100
        self.assertFalse(detect_fvgs(d))

    def test_wrong_displacement_direction(self):
        d = self.zone_data()
        d.loc[39, ["open", "close"]] = [105.5, 100]
        self.assertFalse(detect_fvgs(d))

    def test_volume_baseline_excludes_impulse(self):
        d = self.zone_data()
        d.loc[39, "volume"] = 100
        self.assertFalse(detect_fvgs(d))

    def test_bearish_symmetry(self):
        d = self.zone_data()
        original = d.copy()
        d["open"], d["close"] = 200-original.open, 200-original.close
        d["high"], d["low"] = 200-original.low, 200-original.high
        zones = detect_fvgs(d)
        self.assertTrue(zones)
        self.assertEqual(zones[0]["type"], "BEARISH")

    def test_rsi_monotonic_and_flat(self):
        d = candles()
        self.assertEqual(_calc_rsi(d).iloc[-1], 50)
        d.close = range(100, 145)
        self.assertEqual(_calc_rsi(d).iloc[-1], 100)

    def test_quote_volume_and_spread(self):
        rows = [dict(symbol="BTCUSDT", turnover24h="10000", bid1Price="100", ask1Price="100.01", price24hPcnt=".1"),
                dict(symbol="ETHUSDT", turnover24h="5000", bid1Price="100", ask1Price="110", price24hPcnt="-.1")]
        market = ticker_context(rows, 100, 15)
        self.assertTrue(market["BTCUSDT"]["eligible"])
        self.assertFalse(market["ETHUSDT"]["eligible"])
        self.assertAlmostEqual(market["BTCUSDT"]["breadth"], 2/3)
        self.assertEqual(market["BTCUSDT"]["volume_rank"], 1)

    def test_pivot_not_known_until_confirmed(self):
        d = candles(8)
        d.loc[4, "high"] = 110
        self.assertIsNone(structure_context(d.iloc[:7], 3)["swing_high"])
        self.assertEqual(structure_context(d, 3)["swing_high"], 110)

    def test_uncertainty_and_empty_backtest(self):
        self.assertIsNone(metrics([])["win_rate_pct"])
        m = metrics([{"net_r": 1}, {"net_r": -1}])
        self.assertEqual(m["win_rate_pct"], 50)
        self.assertLess(m["win_rate_95pct_interval"][0], 20)
        self.assertEqual(run_backtest(candles(300))["all"]["trades"], 0)

    def test_stop_not_compressed_into_zone(self):
        cfg.require_structure = False
        cfg.ema_period = 200
        cfg.sl_max_atr = .001
        cfg.min_signal_score = 0
        cfg.rsi_overbought = 100
        d = self.zone_data()
        zone = detect_fvgs(d)[0]
        d.loc[44, ["open", "low", "close"]] = [102.5, 102, 103.5]
        self.assertIsNone(validate_signal(d, zone))

    def test_backtest_next_open_stop_first_costs(self):
        cfg.ema_period = 20
        cfg.rr_min = 1
        d = candles(105)
        # Signal candle cannot exit the trade; next candle touches both levels.
        d.loc[101, ["high", "low"]] = [104, 98]
        zone = {"candle_time": 1, "type": "BULLISH"}
        sig = {"signal": "LONG", "entry": 100, "sl": 99, "tp": 103}
        with patch("trading.backtest.detect_fvgs", return_value=[zone]), patch("trading.backtest.validate_signal", return_value=sig):
            report = run_backtest(d)
        self.assertEqual(report["all"]["trades"], 1)
        self.assertLess(report["all"]["net_r"], -1)

    def test_oversized_stop_filter_has_positive_control(self):
        cfg.require_structure = False
        cfg.ema_period = 200
        cfg.sl_max_atr = 0
        cfg.min_signal_score = 0
        cfg.rsi_overbought = 100
        cfg.max_entry_distance_atr = 5
        d = self.zone_data()
        zone = detect_fvgs(d)[0]
        d.loc[44, ["open", "low", "close"]] = [102.5, 102, 103.5]
        self.assertIsNotNone(validate_signal(d, zone))
        cfg.sl_max_atr = .001
        self.assertIsNone(validate_signal(d, zone))

    def test_delayed_retest_is_balanced_only(self):
        cfg.require_structure = False
        cfg.ema_period = 200
        cfg.sl_max_atr = 0
        cfg.min_signal_score = 0
        cfg.rsi_overbought = 100
        cfg.max_entry_distance_atr = 5
        d = self.zone_data()
        zone = detect_fvgs(d)[0]
        d.loc[43, ["open", "low", "close"]] = [102.5, 102, 103.5]
        d.loc[44, ["open", "low", "close"]] = [103.5, 103.25, 104]
        cfg.strategy_profile = "balanced"
        self.assertIsNotNone(validate_signal(d, zone))
        cfg.strategy_profile = "legacy"
        diagnostics = {}
        self.assertIsNone(validate_signal(d, zone, diagnostics=diagnostics))
        self.assertIn("no_recent_retest", diagnostics)

    def test_cost_gate_uses_net_threshold_not_gross_rr(self):
        cfg.ema_period = 20
        cfg.rr_min = 1.5
        cfg.min_net_rr = .8
        d = candles(105)
        d.loc[102, "high"] = 102
        d.loc[101:, "low"] = 99.5
        zone = {"candle_time": 1, "type": "BULLISH"}
        signal = {"signal": "LONG", "entry": 100, "sl": 99, "tp": 101.5}
        with patch("trading.backtest.detect_fvgs", return_value=[zone]), patch("trading.backtest.validate_signal", return_value=signal):
            result = run_backtest(d)
        self.assertEqual(result["all"]["trades"], 1)
        self.assertGreater(result["all"]["net_r"], 0)

    def test_wilder_adx_separates_trend_from_chop(self):
        trend = candles(120)
        trend.close = [100 + i for i in range(120)]
        trend.open, trend.high, trend.low = trend.close - .5, trend.close + 1, trend.close - 1
        chop = candles(120)
        chop.close = [100 + (1 if i % 2 else -1) for i in range(120)]
        chop.open, chop.high, chop.low = 100., chop.close.clip(lower=100) + .5, chop.close.clip(upper=100) - .5
        self.assertGreater(_calc_adx(trend).iloc[-1], 50)
        self.assertLess(_calc_adx(chop).iloc[-1], 20)

    def test_score_rewards_smc_confluence(self):
        zone = {"gap_size": 0, "touches": 1, "fill_fraction": .2, "displacement_atr": 2}
        base, _ = _score_signal("LONG", {"gap_size": 0, "touches": 2, "fill_fraction": .8}, {}, None, None, None)
        score, factors = _score_signal("LONG", zone, {}, None, None, None,
                                       structure={"direction": "UP", "sweep": "SELL_SIDE"},
                                       trendline_retest=True, relative_volume=2)
        self.assertEqual(score - base, 8 + 8 + 8 + 5 + 5 + 5)
        for tag in ("structure_up", "liquidity_sweep", "trendline_retest", "fresh_zone", "strong_displacement", "high_rvol"):
            self.assertIn(tag, factors)
        _, opposite = _score_signal("SHORT", zone, {}, None, None, None, structure={"direction": "UP", "sweep": "SELL_SIDE"})
        self.assertNotIn("liquidity_sweep", opposite)
        self.assertNotIn("structure_up", opposite)

    def test_sweep_of_older_unbroken_swing_low(self):
        d = candles(20)
        # The latest swing (95) is below the wick; only the older 97 pool is swept.
        d.loc[4, "low"] = 97
        d.loc[11, "low"] = 95
        d.loc[19, ["open", "low", "close"]] = [100, 96, 100.5]
        self.assertEqual(structure_context(d, 3)["sweep"], "SELL_SIDE")

    def test_target_front_runs_resting_liquidity(self):
        cfg.require_structure = False
        cfg.ema_period = 200
        cfg.sl_max_atr = 0
        cfg.min_signal_score = 0
        cfg.rsi_overbought = 100
        cfg.max_entry_distance_atr = 5
        cfg.rr_min = 1.5
        d = self.zone_data()
        zone = detect_fvgs(d)[0]
        d.loc[44, ["open", "low", "close"]] = [102.5, 102, 103.5]
        empty = {"above": [], "below": [], "range_position": .4}
        with patch("trading.strategy.liquidity_levels", return_value=empty):
            base = validate_signal(d, zone)
        risk = base["entry"] - base["sl"]
        level = base["entry"] + 2 * risk
        with patch("trading.strategy.liquidity_levels", return_value=dict(empty, above=[level])):
            sig = validate_signal(d, zone)
        self.assertLess(sig["tp"], level)
        self.assertGreater(sig["tp"], base["entry"] + 1.5 * risk)
        self.assertIn("liquidity_target", sig["factors"])
        with patch("trading.strategy.liquidity_levels", return_value=dict(empty, above=[base["entry"] + .5 * risk])):
            near = validate_signal(d, zone)
        self.assertEqual(near["tp"], base["tp"])
        self.assertEqual(near["score"], max(0, base["score"] - 10))
        self.assertIn("opposing_liquidity_near", near["factors"])

    def test_dynamic_rr_uses_relative_volatility_not_raw_atr_pct(self):
        s = cfg.model_copy(deep=True)
        s.dynamic_rr, s.rr_min, s.rr_max, s.adx_strong = True, 1.0, 5.0, 99
        # 0.2% ATR on a 5m chart used to pin the factor to 0.75 regardless of conditions.
        self.assertAlmostEqual(_safe_dynamic_rr(3, 0.2, 100, None, s), 3.0)
        self.assertAlmostEqual(_safe_dynamic_rr(3, 0.2, 100, None, s, atr_ratio=1.2), 3.6)
        self.assertAlmostEqual(_safe_dynamic_rr(3, 0.2, 100, None, s, atr_ratio=0.5), 2.25)
        s.dynamic_rr = False
        self.assertEqual(_safe_dynamic_rr(3, 0.2, 100, None, s, atr_ratio=1.4), 3)


if __name__ == "__main__":
    unittest.main()
