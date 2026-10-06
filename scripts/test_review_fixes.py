"""Regressions for review findings on shadow labels, feed reads, Telegram trimming, retention."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import database
from core.settings import cfg
import pandas as pd


class ShadowLabelTests(unittest.TestCase):
    def labels(self, **kw):
        from trading import engine
        captured = {}
        zone = {"candle_time": 1, "type": "BULLISH"}
        raw = {"score": kw.pop("score", 80), "signal": "LONG"}
        assessments = {"1BULLISH": {"shadow": raw, **kw.pop("assessment", {})}}
        with patch.object(engine.shadow, "track", side_effect=lambda s, k, c, b, ctx: captured.setdefault("b", b)), \
             patch.object(cfg, "min_signal_score", 70), patch.object(cfg, "timeframe", "5"):
            engine.track_shadows("XUSDT", [zone], assessments, kw.get("skip", []), kw.get("chosen"),
                                 kw.get("final", []), kw.get("emitted", False), {})
        return captured["b"]

    def test_only_emitted_signal_counts_as_passed(self):
        self.assertEqual(self.labels(chosen="XUSDT_5_1_BULLISH", emitted=True), [])
        self.assertEqual(self.labels(chosen="XUSDT_5_1_BULLISH", final=["learning_score_below_threshold"]), ["learning_score_below_threshold"])
        self.assertEqual(self.labels(chosen="XUSDT_5_1_BULLISH"), ["not_emitted"])

    def test_unevaluated_zones_get_a_reason(self):
        self.assertEqual(self.labels(skip=["illiquid_market"]), ["illiquid_market"])
        self.assertEqual(self.labels(), ["not_evaluated"])
        self.assertEqual(self.labels(assessment={"execution_rejections": ["fvg_fully_filled"]}), ["fvg_fully_filled"])
        self.assertEqual(self.labels(score=50), ["score_below_threshold"])


class FeedReadTests(unittest.TestCase):
    def test_reads_every_chunk_and_caps_size(self):
        from trading.news import read_capped
        class Content:
            def __init__(self, parts): self.parts = parts
            async def iter_chunked(self, n):
                for part in self.parts:
                    yield part
        class Response:
            def __init__(self, parts, length=None): self.content, self.content_length = Content(parts), length
        self.assertEqual(asyncio.run(read_capped(Response([b"a"*10, b"b"*10]))), b"a"*10 + b"b"*10)
        with self.assertRaises(ValueError):
            asyncio.run(read_capped(Response([b"x"*8]*3), limit=20))
        with self.assertRaises(ValueError):
            asyncio.run(read_capped(Response([], length=10**9)))


class TelegramFitTests(unittest.TestCase):
    def test_trims_on_line_boundaries(self):
        from tg.handlers import fit
        text = "\n".join(["&amp;&lt;b&gt;" * 20] * 50)
        out = fit(text, 500)
        self.assertLessEqual(len(out), 500)
        self.assertTrue(all(line.endswith("&gt;") for line in out.split("\n")[:-1]))


class RetentionTests(unittest.TestCase):
    def test_heartbeat_keeps_long_lived_setup(self):
        with tempfile.TemporaryDirectory() as d, patch.object(database, "DB_PATH", str(Path(d)/"r.db")), \
             patch("trading.coin_news.COIN_NEWS_DB", Path(d)/"n.sqlite3"):
            from trading import setup_journal, telemetry
            now = time.time()
            setup_journal.record("ALIVE", "POTENTIAL", "r", {"assessment": {"score": 1}}, now=now-60*86400)
            setup_journal.record("ALIVE", "POTENTIAL", "r", {"assessment": {"score": 1, "current_price": 2}}, now=now-60)
            setup_journal.record("DEAD", "POTENTIAL", "r", {"assessment": {"score": 1}}, now=now-60*86400)
            telemetry.prune(cfg, now)
            with database._conn() as con:
                kept = {r[0] for r in con.execute("SELECT DISTINCT setup_id FROM setup_events")}
            self.assertEqual(kept, {"ALIVE"})


class CriticalFixTests(unittest.TestCase):
    def test_malformed_trade_features_do_not_stop_management(self):
        from trading import paper_trading as paper
        saved = cfg.paper_balance
        rows = [dict(id=1, symbol="TESTUSDT", side="LONG", entry=100, sl=99, tp=103, ml_features="null"),
                dict(id=2, symbol="TESTUSDT", side="LONG", entry=100, sl=99, tp=103, ml_features="{broken")]
        try:
            with patch.object(paper, "get_active_signals", return_value=rows), patch.object(paper, "settle", return_value=(-10, saved-10)) as settle:
                asyncio.run(paper.check_active_trades(None, "TESTUSDT", 98.5, 98.5))
            self.assertEqual(settle.call_count, 2)
        finally:
            cfg.paper_balance = saved

    def test_settings_save_keeps_ledger_balance(self):
        from core import settings as settings_mod
        with tempfile.TemporaryDirectory() as d, patch.object(database, "DB_PATH", str(Path(d)/"b.db")), \
             patch.object(settings_mod, "CONFIG_FILE", str(Path(d)/"cfg.json")), patch.object(settings_mod, "DATA_DIR", d), \
             patch.object(settings_mod, "_mutate_global") as mutate:
            from trading import paper_account
            paper_account.reset_balance(9400)
            stale = cfg.model_copy(deep=True)
            stale.paper_balance = 10000
            settings_mod.save_settings(stale)
            self.assertEqual(mutate.call_args.args[0].paper_balance, 9400)

    def test_sweep_ignores_level_already_closed_through(self):
        from trading.context import structure_context
        d = pd.DataFrame([dict(open=102., high=103., low=101.5, close=102.) for _ in range(30)])
        d.loc[5, "low"] = 100.0      # older swing low
        d.loc[12, "low"] = 101.0     # newer swing low
        d.loc[20, ["low", "close"]] = [99.4, 99.5]   # closes through both
        d.loc[21:28, ["low", "close", "open", "high"]] = [100.6, 101.2, 101.2, 101.8]
        d.loc[29, ["open", "low", "close", "high"]] = [100.6, 99.8, 100.4, 100.9]  # wicks under 100 only
        self.assertIsNone(structure_context(d, 3)["sweep"])

    def test_daily_timeframe_shadows_are_tracked(self):
        from trading import shadow
        with tempfile.TemporaryDirectory() as d, patch.object(database, "DB_PATH", str(Path(d)/"s.db")), patch.object(cfg, "timeframe", "D"):
            self.assertIsNotNone(shadow.track("XUSDT", "k", {"signal": "LONG", "entry": 100, "sl": 99, "tp": 102, "score": 80}, []))


if __name__ == "__main__":
    unittest.main()
