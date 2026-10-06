"""Shadow outcome tracking and learning from it; temporary database, no network."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import database
from core.settings import cfg


def candidate(side="LONG", score=80, factors=("htf_uptrend",), setup="fvg_retest", regime="TREND"):
    entry, sl, tp = (100.0, 99.0, 102.0) if side == "LONG" else (100.0, 101.0, 98.0)
    return {"signal": side, "entry": entry, "sl": sl, "tp": tp, "score": score, "factors": list(factors), "setup": setup, "regime": regime}


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = patch.object(database, "DB_PATH", str(Path(self.dir.name)/"s.db"))
        self.patch.start()
        from trading import shadow, learning
        self.shadow, self.learning = shadow, learning
        learning._CACHE.update(rows=None, at=0)
        self.saved = cfg.model_copy(deep=True)
        cfg.fee_bps, cfg.slippage_bps, cfg.timeframe, cfg.shadow_max_hold_bars = 0, 0, "5", 12

    def tearDown(self):
        for name in type(cfg).model_fields:
            setattr(cfg, name, getattr(self.saved, name))
        self.patch.stop()
        self.dir.cleanup()

    def test_each_zone_tracked_once_and_resolved_stop_first(self):
        self.assertIsNotNone(self.shadow.track("AUSDT", "k1", candidate(), [], now=0))
        self.assertIsNone(self.shadow.track("AUSDT", "k1", candidate(), [], now=10))
        self.assertEqual(self.shadow.update("AUSDT", 100.5, now=20), [])
        closed = self.shadow.update("AUSDT", 98.5, now=30)
        self.assertEqual(closed[0]["reason"], "SL")
        self.assertAlmostEqual(closed[0]["r"], -1.5)  # gap through the stop fills worse

    def test_target_and_timeout_in_r(self):
        self.shadow.track("BUSDT", "k2", candidate("SHORT"), ["thin_orderbook"], now=0)
        self.assertAlmostEqual(self.shadow.update("BUSDT", 97.9, now=5)[0]["r"], 2.0)
        self.shadow.track("CUSDT", "k3", candidate(), [], now=0)
        closed = self.shadow.update("CUSDT", 100.5, now=12*300)
        self.assertEqual(closed[0]["reason"], "TIMEOUT")
        self.assertAlmostEqual(closed[0]["r"], 0.5)
        rows = self.shadow.outcomes()
        self.assertEqual({r["exit_reason"] for r in rows}, {"TP", "TIMEOUT"})
        self.assertEqual([r["blockers"] for r in rows if r["exit_reason"] == "TP"], [["thin_orderbook"]])

    def test_costs_reduce_r(self):
        cfg.fee_bps = 50
        self.shadow.track("DUSDT", "k4", candidate(), [], now=0)
        self.assertAlmostEqual(self.shadow.update("DUSDT", 102, now=1)[0]["r"], 2 - 1.0)


def rows(spec):
    """spec: list of (factors, blockers, r)."""
    return [{"side": "LONG", "setup": "fvg_retest", "regime": "TREND", "factors": list(f), "blockers": list(b), "r": r}
            for f, b, r in spec]


class LearningTests(unittest.TestCase):
    def test_effect_is_shrunk_on_small_samples(self):
        from trading.learning import factor_performance
        data = rows([(["x"], [], 2.0)]*3 + [([], [], -1.0)]*30)
        small = factor_performance(min_trades=1, rows=data)["x"]
        self.assertGreater(small["effect_r"], 0)
        self.assertLess(small["effect_r"], small["avg_r"] - (-1*30+6)/33)  # well below the raw gap
        big = factor_performance(min_trades=1, rows=rows([(["x"], [], 2.0)]*60 + [([], [], -1.0)]*60))["x"]
        self.assertGreater(big["effect_r"], small["effect_r"])

    def test_ubiquitous_factor_carries_no_signal(self):
        from trading.learning import factor_performance
        data = rows([(["always"], [], 1.0)]*20 + [(["always"], [], -1.0)]*20)
        self.assertFalse(factor_performance(min_trades=1, rows=data)["always"]["sample_ok"])

    def test_blocker_verdicts(self):
        from trading.learning import blocker_report
        data = rows([([], ["good_filter"], -1.0)]*15 + [([], ["costly_filter"], 1.5)]*15 + [([], [], 0.5)]*15)
        verdicts = {b["blocker"]: b["verdict"] for b in blocker_report(data)}
        self.assertTrue(verdicts["good_filter"].startswith("полезен"))
        self.assertTrue(verdicts["costly_filter"].startswith("режет"))
        self.assertIn("__passed__", verdicts)

    def test_context_estimate_falls_back_to_broader_bucket(self):
        from trading.learning import context_estimate
        data = rows([([], [], 1.0)]*12 + [([], [], -1.0)]*8)
        est = context_estimate({"signal": "LONG", "setup": "fvg_retest", "regime": "TREND"}, data)
        self.assertEqual(est["level"], "context")
        self.assertAlmostEqual(est["win_rate_est"], 100*(12 + 20*0.6)/40, places=1)
        other = context_estimate({"signal": "LONG", "setup": "range_reclaim", "regime": "RANGE"}, data)
        self.assertEqual(other["level"], "side")
        self.assertEqual(context_estimate({"signal": "SHORT"}, data)["level"], "insufficient")

    def test_signal_gets_estimate_and_bounded_adjustment(self):
        from trading import learning
        perf = {"x": {"sample_ok": True, "effect_r": 5.0}}
        with patch.object(learning, "factor_performance", return_value=perf), patch.object(learning, "evidence", return_value=[]), \
             patch.object(cfg, "enable_factor_learning", True), patch.object(cfg, "factor_score_adjustment_cap", 15), patch.object(cfg, "min_signal_score", 0):
            sig = learning.apply_learning_to_signal({"signal": "LONG", "score": 70, "factors": ["x"]})
        self.assertEqual(sig["learning_adjustment"], 15)
        self.assertIn("learn_bonus:x", sig["factors"])
        self.assertEqual(sig["history_estimate"]["level"], "insufficient")


if __name__ == "__main__":
    unittest.main()
