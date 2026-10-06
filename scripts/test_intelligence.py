import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.settings import cfg
from trading.news import archive_events, news_context, classify, matches_symbol, parse_rss
from trading.intelligence import microstructure_context, technical_blockers
from trading.context import trendline_context
from trading.research import research_gate, fingerprint


class IntelligenceTests(unittest.TestCase):
    def test_news_cannot_be_seen_before_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"events.db"
            row = dict(title="Bitcoin exploit", url="https://example.com/event", source="test", published_at=100000, observed_at=200000, **classify("Bitcoin exploit"))
            archive_events([row], path)
            self.assertEqual(news_context("BTCUSDT", 199999, path)["events"], [])
            self.assertEqual(len(news_context("BTCUSDT", 200000, path)["events"]), 1)
            row["observed_at"] = 300000
            archive_events([row], path)
            self.assertEqual(len(news_context("BTCUSDT", 200000, path)["events"]), 1)

    def test_ambiguous_ticker_not_matched_to_prose(self):
        self.assertFalse(matches_symbol("ONE thing to know about trading", "ONEUSDT"))
        self.assertTrue(matches_symbol("$ONE network upgrade", "ONEUSDT"))
        self.assertTrue(matches_symbol("Ethereum network upgrade", "ETHUSDT"))

    def test_rss_rejects_dtd_and_future_news(self):
        with self.assertRaises(ValueError):
            parse_rss(b'<!DOCTYPE rss><rss/>', "test", 0)
        data = b'<rss><channel><item><title>Bitcoin news</title><link>https://example.com</link><pubDate>Tue, 08 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>'
        self.assertEqual(parse_rss(data, "test", 1000), [])

    def test_stale_book_and_depth(self):
        raw = {"book": {"ts": 100000, "b": [["100", "10"]], "a": [["100.01", "20"]]}}
        micro = microstructure_context(raw, now_ms=100001)
        self.assertEqual(micro["status"], "OK")
        self.assertAlmostEqual(micro["bid_depth_usdt"], 1000)
        self.assertEqual(microstructure_context(raw, now_ms=200000)["status"], "STALE")

    def test_btc_correlation_is_conditional(self):
        coin = {"status": "OK", "regime": "TREND", "benchmark": {"correlation": .9}}
        self.assertIn("correlated_btc_opposition", technical_blockers("LONG", coin, "DOWN"))
        coin["benchmark"]["correlation"] = .1
        self.assertNotIn("correlated_btc_opposition", technical_blockers("LONG", coin, "DOWN"))

    def test_paper_gate_does_not_claim_qualification(self):
        settings = cfg.model_copy(deep=True)
        settings.require_backtest_pass = True
        settings.research_gate_mode = "paper"
        q, blockers = research_gate(None, settings)
        self.assertEqual(q["status"], "PENDING")
        self.assertEqual(blockers, [])
        settings.research_gate_mode = "strict"
        self.assertEqual(research_gate(None, settings)[1], ["backtest_pending"])

    def test_profile_invalidates_old_research(self):
        settings = cfg.model_copy(deep=True)
        settings.strategy_profile = "legacy"
        first = fingerprint(settings)
        settings.strategy_profile = "balanced"
        self.assertNotEqual(first, fingerprint(settings))

    def test_paper_gate_blocks_only_clearly_negative_coins(self):
        from trading import research
        settings = cfg.model_copy(deep=True)
        settings.research_gate_mode, settings.require_backtest_pass, settings.research_min_trades = "paper", True, 30
        def q(upper):
            return {"status": "REJECT", "reasons": ["nonpositive_expectancy"], "sample": {"trades": 40, "expectancy_r": -0.1, "expectancy_r_upper95": upper}}
        with patch.object(research, "qualify", return_value=q(0.2)):
            self.assertEqual(research.research_gate({}, settings)[1], [])
        with patch.object(research, "qualify", return_value=q(-0.05)):
            self.assertEqual(research.research_gate({}, settings)[1], ["coin_backtest_clearly_negative"])

    def test_partial_news_is_visible_in_paper_mode(self):
        from trading.news import NEWS_STATE
        now = 1000000
        states = {"Bybit announcements": {"status": "OK", "checked_at": now},
                  cfg.news_feeds[0]: {"status": "OK", "checked_at": now}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(NEWS_STATE, {"sources": states}), patch.object(cfg, "research_gate_mode", "paper"), patch.object(cfg, "news_require_coverage", True):
            result = news_context("BTCUSDT", now, Path(directory)/"missing.db")
            self.assertEqual(result["coverage"], "PARTIAL")
            self.assertNotIn("news_coverage_incomplete", result["blockers"])
            with patch.object(cfg, "research_gate_mode", "strict"):
                self.assertIn("news_coverage_incomplete", news_context("BTCUSDT", now, Path(directory)/"missing.db")["blockers"])
        # A full feed outage in paper mode is reported as coverage, never as a global entry block.
        with tempfile.TemporaryDirectory() as directory, patch.dict(NEWS_STATE, {"sources": {}}), patch.object(cfg, "research_gate_mode", "paper"), patch.object(cfg, "news_require_coverage", True):
            outage = news_context("BTCUSDT", now, Path(directory)/"missing.db")
            self.assertEqual(outage["coverage"], "UNAVAILABLE")
            self.assertNotIn("news_coverage_incomplete", outage["blockers"])

    def test_trendline_confirmation_and_break(self):
        lows = [105,104,100,104,105,106,103,106,107,107,106,105]
        df = pd.DataFrame({"timestamp": range(len(lows)), "low": lows})
        df["open"], df["close"], df["high"] = df.low+1, df.low+2, df.low+3
        ctx = trendline_context(df, pivot=2, atr=2)
        self.assertIsNotNone(ctx["support"])
        self.assertIsNone(trendline_context(df.iloc[:8], pivot=2, atr=2)["support"])
        df.loc[len(df)-1, "close"] = 90
        self.assertIsNone(trendline_context(df, pivot=2, atr=2)["support"])

    def test_loss_pause_expires(self):
        from datetime import datetime, timedelta
        from trading import risk_manager as risk
        with patch.object(risk, "current_loss_streak", return_value=10), patch.object(risk, "get_closed_today", return_value=[]), patch.object(risk, "last_any_loss_time", return_value=(datetime.now()-timedelta(days=2)).isoformat()):
            self.assertTrue(risk.is_trading_allowed())


if __name__ == "__main__":
    unittest.main()
