import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core import database
from core.settings import cfg
from trading import signal_delivery as delivery


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DB_PATH", str(Path(self.temp.name)/"test.db"))
        self.db_patch.start()
        self.saved = cfg.model_copy(deep=True)
        cfg.is_running, cfg.enable_news, cfg.enable_risk_guard, cfg.tg_chat_id = True, False, False, "123"
        self.signal = dict(signal="LONG", entry=100., sl=99., tp=102., rr=2., score=80)

    def tearDown(self):
        for field in type(cfg).model_fields:
            setattr(cfg, field, getattr(self.saved, field))
        self.db_patch.stop()
        self.temp.cleanup()

    def test_enqueue_deduplicates_and_expires(self):
        delivery.enqueue("one", "BTCUSDT", self.signal, "123")
        delivery.enqueue("one", "BTCUSDT", self.signal, "123")
        self.assertEqual(len(delivery.outbox_rows()), 1)
        self.assertEqual(delivery.outbox_rows(time.time()+cfg.signal_max_age_seconds+1), [])

    def test_delayed_enqueue_keeps_original_expiry(self):
        delivery.enqueue('delayed','BTCUSDT',self.signal,'123',created=1000)
        self.assertEqual(delivery.outbox_rows(1000+cfg.signal_max_age_seconds+1),[])

    def test_transient_failure_is_pending_then_success(self):
        delivery.enqueue("one", "BTCUSDT", self.signal, "123")
        bot = AsyncMock()
        bot.send_message.side_effect = OSError("offline")
        frame = pd.DataFrame([{"close":100.,"timestamp":int(time.time()*1000)}])
        with patch.object(delivery, "get_klines_async", new=AsyncMock(return_value=frame)):
            asyncio.run(delivery.deliver_once(bot))
            with database._conn() as con:
                row = dict(con.execute("SELECT * FROM signal_outbox").fetchone())
                self.assertEqual(row["state"], "PENDING")
                self.assertEqual(row["attempts"], 1)
                con.execute("UPDATE signal_outbox SET next_try=0")
            bot.send_message.side_effect = None
            asyncio.run(delivery.deliver_once(bot))
        self.assertEqual(delivery.outbox_rows(), [])
        with database._conn() as con:
            self.assertEqual(con.execute("SELECT state FROM signal_outbox").fetchone()[0], "SENT")

    def test_moved_price_does_not_send(self):
        delivery.enqueue("one", "BTCUSDT", self.signal, "123")
        bot = AsyncMock()
        with patch.object(delivery, "get_klines_async", new=AsyncMock(return_value=pd.DataFrame([{"close":102.,"timestamp":int(time.time()*1000)}]))):
            asyncio.run(delivery.deliver_once(bot))
        bot.send_message.assert_not_called()

    def test_cached_selection_ignores_stale_and_blocked(self):
        latest = {"FRESH": {"signal": self.signal, "signal_created":1000},
                  "OLD": {"signal": self.signal, "signal_created":1},
                  "BLOCKED": {"signal": self.signal, "signal_created":1000, "blockers":["news"]}}
        self.assertEqual([s for s,_ in delivery.cached_candidates(latest,1001)], ["FRESH"])

    def test_quick_empty_does_not_invent_signal(self):
        with patch("trading.engine.quick_scan", new=AsyncMock(return_value=0)):
            result = asyncio.run(delivery.quick_signal({}))
        self.assertIn("нет свежего", result)

    def test_quick_reuses_analysis_without_new_trade(self):
        latest = {"BTCUSDT": {"signal": self.signal, "signal_created":time.time()}}
        with patch.object(delivery, "get_klines_async", new=AsyncMock(return_value=pd.DataFrame([{"close":100.,"timestamp":int(time.time()*1000)}]))):
            result = asyncio.run(delivery.quick_signal(latest))
        self.assertIn("BTCUSDT", result)
        self.assertIn("новая paper-сделка не создаётся", result)

    def test_quick_runs_fresh_scan_when_cache_has_no_entry(self):
        with patch("trading.engine.quick_scan", new=AsyncMock(return_value=12)) as scan:
            result = asyncio.run(delivery.quick_signal({}))
        scan.assert_awaited_once_with(max_symbols=80)
        self.assertIn("12", result)

    def test_quick_returns_explicit_early_observation(self):
        now = time.time()
        latest = {"ETHUSDT": {
            "updated_at": __import__("datetime").datetime.fromtimestamp(now, __import__("datetime").timezone.utc).isoformat(),
            "current_price": 100.0,
            "intelligence": {"trend": "UP", "regime": "TREND", "relative_volume": 1.7},
            "zones": [{"candle_time": 10, "type": "BULLISH", "bottom": 99.0, "top": 101.0, "ce": 100.0}],
            "assessments": {"10BULLISH": {
                "score": 76, "checks": {"FVG": True, "HTF bias": True,
                    "Structure": True, "Liquidity": False, "Zone eligibility": True, "Entry": False},
                "rejections": ["no_recent_retest"]}}
        }}
        with patch("trading.engine.quick_scan", new=AsyncMock(return_value=0)):
            result = asyncio.run(delivery.quick_signal(latest))
        self.assertIn("ETHUSDT LONG", result)
        self.assertIn("Новая paper-сделка не создаётся", result)


if __name__ == "__main__":
    unittest.main()
