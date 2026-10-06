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


if __name__ == "__main__":
    unittest.main()
