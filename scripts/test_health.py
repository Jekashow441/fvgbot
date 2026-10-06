"""System health thresholds and boundary-safe candle loading; no network."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core import database


def cycle(counts, htf=None, higher=None):
    return json.dumps({"selected": sum(counts.values()), "counts": counts, "symbols": {}, "htf": htf or {}, "higher_4h": higher or {}})


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.patch = patch.object(database, "DB_PATH", str(Path(self.dir.name)/"h.db"))
        self.patch.start()
        from trading import telemetry
        self.t = telemetry
        telemetry.WORKERS.clear()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def put(self, payload, completed):
        with database._conn() as con:
            self.t.schema(con)
            con.execute("INSERT INTO scan_cycles(started_at,completed_at,payload) VALUES(?,?,?)", (completed-30, completed, payload))

    def test_few_bad_symbols_stay_healthy(self):
        now = time.time()
        self.put(cycle({"fresh": 400, "stale": 3, "failed": 2, "skipped": 5}, {"fresh": 400, "unavailable": 2}), now-10)
        h = self.t.system_health(now)
        self.assertEqual(h["status"], "HEALTHY")
        self.assertTrue(h["notes"])

    def test_many_bad_symbols_warn_with_russian_reason(self):
        now = time.time()
        self.put(cycle({"fresh": 300, "stale": 60, "failed": 50, "skipped": 0}), now-10)
        h = self.t.system_health(now)
        self.assertEqual(h["status"], "WARNING")
        self.assertIn("110 из 410", h["details"][0])

    def test_stalled_scanner_is_critical(self):
        now = time.time()
        self.put(cycle({"fresh": 400, "stale": 0, "failed": 0, "skipped": 0}), now-600)
        self.assertEqual(self.t.system_health(now)["status"], "CRITICAL")

    def test_prune_drops_old_diagnostics_but_keeps_recent_and_open(self):
        from core.settings import cfg
        from trading import setup_journal
        database.init_db()
        now = time.time()
        self.put(cycle({"fresh": 1, "stale": 0, "failed": 0, "skipped": 0}), now-10*86400)
        self.put(cycle({"fresh": 1, "stale": 0, "failed": 0, "skipped": 0}), now-10)
        setup_journal.record("OLD", "POTENTIAL", "r", {"assessment": {}}, now=now-60*86400)
        setup_journal.record("OPEN", "CONFIRMED", "r", {"assessment": {}}, now=now-60*86400)
        setup_journal.record("NEW", "POTENTIAL", "r", {"assessment": {}}, now=now-60)
        database.log_signal(dict(symbol="X", side="LONG", score=1, entry=1, sl=.9, tp=1.2, rsi=None, trend=None, entry_ts=0,
                                 ml_features={"journal_id": "OPEN", "position_size_usdt": 1}))
        removed = self.t.prune(cfg, now)
        self.assertEqual(removed["scan_cycles"], 1)
        with database._conn() as con:
            kept = {r[0] for r in con.execute("SELECT DISTINCT setup_id FROM setup_events")}
        self.assertEqual(kept, {"OPEN", "NEW"})


class JournalTests(unittest.TestCase):
    def test_price_only_changes_do_not_append_events(self):
        with tempfile.TemporaryDirectory() as d, patch.object(database, "DB_PATH", str(Path(d)/"j.db")):
            from trading import setup_journal as j
            a = {"status": "POTENTIAL", "score": 80, "rejections": ["no_recent_retest"]}
            for i in range(5):
                j.record("K", "POTENTIAL", "r", {"assessment": dict(a, current_price=1+i/100, analysis_bar=i)}, now=i)
            j.record("K", "ENTRY APPROACHING", "r", {"assessment": dict(a, status="ENTRY APPROACHING", current_price=2, analysis_bar=9)}, now=9)
            events = j.history("K")
            self.assertEqual([e["status"] for e in events], ["POTENTIAL", "ENTRY APPROACHING"])
            j.record("K", "ENTRY APPROACHING", "r", {"assessment": dict(a, status="ENTRY APPROACHING", current_price=3, analysis_bar=10)}, now=12)
            last = j.latest_events(prefix="K")[0]
            self.assertEqual((last["observed_at"], last["payload"]["assessment"]["analysis_bar"]), (12, 10))


def bars(n, step, end_open):
    rows = [dict(timestamp=end_open-(n-1-i)*step, open=1., high=1.1, low=.9, close=1., volume=10.) for i in range(n)]
    return pd.DataFrame(rows)


class LoaderTests(unittest.TestCase):
    def test_request_started_before_boundary_is_refetched(self):
        from trading import engine
        step = 300000
        now = 1000*step + 1500
        early = bars(260, step, 999*step)
        early.attrs["observed_at_ms"] = 1000*step - 200   # started just before the 1000th bar opened
        late = bars(260, step, 1000*step)
        late.attrs["observed_at_ms"] = now
        calls = []
        async def fake(symbol, interval, limit):
            calls.append(1)
            return early if len(calls) == 1 else late
        with patch.object(engine, "get_klines_async", side_effect=fake), patch.object(engine, "invalidate_klines") as inv, \
             patch.object(engine.time, "time", return_value=now/1000):
            raw, closed = asyncio.run(engine.load_closed("XUSDT", "5", 260, 200))
        self.assertEqual(len(calls), 2)
        inv.assert_called_once()
        self.assertEqual(int(closed.iloc[-1].timestamp), 999*step)

    def test_new_listing_reports_insufficient_history(self):
        from trading import engine
        step = 300000
        now = 1000*step + 1500
        short = bars(50, step, 1000*step)
        short.attrs["observed_at_ms"] = now
        async def fake(symbol, interval, limit):
            return short
        with patch.object(engine, "get_klines_async", side_effect=fake), patch.object(engine.time, "time", return_value=now/1000):
            with self.assertRaisesRegex(ValueError, engine.INSUFFICIENT_HISTORY):
                asyncio.run(engine.load_closed("NEWUSDT", "5", 260, 200))
        self.assertEqual(engine._htf_status({"trend": "UNKNOWN", "error": engine.INSUFFICIENT_HISTORY}), "insufficient_history")
        self.assertEqual(engine._htf_status({"trend": "UP"}), "fresh")


if __name__ == "__main__":
    unittest.main()
