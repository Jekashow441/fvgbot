"""Dashboard view, depth scaling and breakeven-neutral risk counters; no network."""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import database
from core.settings import cfg


class DepthTests(unittest.TestCase):
    def test_required_depth_scales_with_position_and_is_bounded(self):
        from trading.intelligence import required_book_depth
        s = cfg.model_copy(deep=True)
        s.min_book_depth_usdt, s.min_book_depth_floor_usdt, s.book_depth_position_multiple = 25000, 5000, 10
        self.assertEqual(required_book_depth(100, s), 5000)
        self.assertEqual(required_book_depth(1000, s), 10000)
        self.assertEqual(required_book_depth(10000, s), 25000)
        self.assertEqual(required_book_depth(None, s), 25000)
        self.assertEqual(required_book_depth(0, s), 25000)

    def test_live_blockers_use_scaled_depth(self):
        from trading.intelligence import live_blockers
        coin = {"status": "OK", "benchmark": {}}
        micro = {"status": "OK", "spread_bps": 1, "ask_depth_usdt": 12000, "bid_depth_usdt": 12000}
        market = {"funding_rate": 0}
        self.assertNotIn("thin_orderbook", live_blockers("LONG", coin, "UP", market, micro, notional=1000))
        self.assertIn("thin_orderbook", live_blockers("LONG", coin, "UP", market, micro, notional=None))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = patch.object(database, "DB_PATH", str(Path(self.dir.name)/"t.db"))
        self.patch.start()
        database.init_db()
        from trading import paper_account
        self.account = paper_account
        self.account.balance(10000)

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def close(self, symbol, outcome, pnl, reason):
        sig = dict(symbol=symbol, side="LONG", score=80, entry=100, sl=99, tp=103, rsi=None, trend=None, factors=[],
                   ml_features={"position_size_usdt": 1000}, entry_ts=0)
        self.account.settle(database.log_signal(sig), outcome, 100, pnl, reason, 10000)

    def test_breakeven_exit_is_not_a_loss_for_risk_guard(self):
        self.close("AUSDT", "LOSS", -1, "SL")
        self.close("BUSDT", "LOSS", 0.0, "BE")
        self.assertEqual(database.current_loss_streak(), 1)
        self.assertIsNone(database.last_loss_time("BUSDT"))
        self.assertIsNotNone(database.last_loss_time("AUSDT"))

    def test_overview_is_json_safe_and_counts_breakeven_separately(self):
        from trading.dashboard import overview, equity_history
        self.close("AUSDT", "WIN", 1.5, "TP")
        self.close("BUSDT", "LOSS", 0.0, "BE")
        self.close("CUSDT", "LOSS", -1, "SL")
        zone = {"type": "BULLISH", "top": 10.0, "bottom": 9.0, "ce": 9.5, "candle_time": 7, "age": 3, "touches": 1}
        latest = {"XUSDT": {"current_price": 11.0, "zones": [zone], "intelligence": {"atr_pct": 2.0},
                            "assessments": {"7BULLISH": {"score": 88, "status": "POTENTIAL", "checks": {"Zone eligibility": True},
                                                         "rejections": ["no_recent_retest"], "execution_rejections": ["thin_orderbook"]}},
                            "blockers": ["no_recent_retest"], "signal": {"signal": "LONG", "score": float("nan"), "entry": 1, "sl": 0.9, "tp": 1.3}}}
        data = overview(latest, {"selected": 1}, {"XUSDT": {"eligible": True, "breadth": 0.6}}, {}, {"status": "HEALTHY"})
        json.dumps(data, allow_nan=False)
        self.assertEqual((data["stats"]["wins"], data["stats"]["losses"], data["stats"]["breakeven"]), (1, 1, 1))
        self.assertEqual(data["stats"]["win_rate"], 50.0)
        self.assertAlmostEqual(data["stats"]["net_pnl_usdt"], 5.0)
        row = data["watchlist"][0]
        self.assertEqual(row["missing"], ["no_recent_retest", "thin_orderbook"])
        self.assertAlmostEqual(row["distance_atr"], 100*1/11/2.0, places=2)
        self.assertEqual(data["signals"], [])  # blocked symbols are not live signals
        self.assertEqual({b["reason"] for b in data["blockers"]}, {"no_recent_retest", "thin_orderbook"})
        self.assertEqual([e["balance"] for e in equity_history()], [10015.0, 10015.0, 10005.0])


if __name__ == "__main__":
    unittest.main()
