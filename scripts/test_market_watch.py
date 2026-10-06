"""Coin news, trader commentary and liquidity targeting; no network."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from core.settings import cfg
from trading import coin_news, market_watch
from trading.context import liquidity_levels


def rss(items, now):
    body = "".join(f"<item><title>{t}</title><link>https://example.com/{i}</link><pubDate>"
                   f"{time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(now/1000-h*3600))}</pubDate></item>"
                   for i, (t, h) in enumerate(items))
    return f"<rss><channel>{body}</channel></rss>".encode()


class CoinNewsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name)/"n.sqlite3"
        coin_news.COIN_NEWS_STATE.clear()

    def tearDown(self):
        self.dir.cleanup()

    def test_relevance_separates_similar_names(self):
        self.assertFalse(coin_news.relevant("Bitcoin Cash jumps 10%", "BTCUSDT"))
        self.assertTrue(coin_news.relevant("Bitcoin Cash jumps 10%", "BCHUSDT"))
        self.assertTrue(coin_news.relevant("Pepe rallies as memecoins rebound", "1000PEPEUSDT"))
        self.assertFalse(coin_news.relevant("Solana ETF filing", "ADAUSDT"))

    def test_tone_and_critical_flags(self):
        tone, _, critical = coin_news.headline_tone("Hackers drain $40M from Sui DEX")
        self.assertLess(tone, 0)
        self.assertTrue(critical)
        tone, _, critical = coin_news.headline_tone("Solana ETF approved, SOL surges")
        self.assertGreater(tone, 0.5)
        self.assertFalse(critical)

    def test_ingest_filters_irrelevant_and_never_sees_future(self):
        now = 1_800_000_000_000
        rows = coin_news.ingest("SUIUSDT", rss([("Sui exploited for $10M - CoinDesk", 2), ("Cardano upgrade ships", 1)], now), now, self.db)
        self.assertEqual(len(rows), 1)
        summary = coin_news.coin_news("SUIUSDT", now, self.db)
        self.assertTrue(summary["critical"])
        self.assertEqual(summary["label"], "BEARISH")
        self.assertEqual(coin_news.coin_news("SUIUSDT", now-1, self.db)["count"], 0)

    def test_recent_headlines_weigh_more(self):
        now = 1_800_000_000_000
        coin_news.ingest("SOLUSDT", rss([("Solana rallies to record high", 1), ("Solana falls after outage", 40)], now), now, self.db)
        self.assertGreater(coin_news.coin_news("SOLUSDT", now, self.db)["sentiment"], 0)

    def test_priority_symbols_refresh_first_and_respect_ttl(self):
        now = 10_000
        coin_news.COIN_NEWS_STATE["AUSDT"] = {"checked_at_s": now-60}
        self.assertEqual(coin_news.due_symbols(["AUSDT", "BUSDT"], ["CUSDT", "BUSDT"], now), ["BUSDT", "CUSDT"])


class DecisionTests(unittest.TestCase):
    def test_news_tone_nudges_score_by_direction(self):
        from trading.engine import apply_news_tone
        long_sig = {"signal": "LONG", "score": 70, "factors": []}
        apply_news_tone(long_sig, {"sentiment": 0.6})
        self.assertEqual(long_sig["score"], 70 + cfg.coin_news_score_weight)
        self.assertIn("news_tailwind", long_sig["factors"])
        short_sig = {"signal": "SHORT", "score": 70, "factors": []}
        apply_news_tone(short_sig, {"sentiment": 0.6})
        self.assertIn("news_headwind", short_sig["factors"])
        neutral = {"signal": "LONG", "score": 70, "factors": []}
        apply_news_tone(neutral, {"sentiment": None})
        self.assertEqual(neutral["score"], 70)

    def test_liquidity_levels_only_unbroken_swings(self):
        d = pd.DataFrame([dict(open=100., high=101., low=99., close=100.) for _ in range(30)])
        d.loc[5, "high"] = 110
        d.loc[12, "high"] = 105
        d.loc[16, ["high", "close"]] = [106.5, 106]
        levels = liquidity_levels(d, 3)
        self.assertIn(110, levels["above"])
        self.assertNotIn(105, levels["above"])
        self.assertIsNotNone(levels["range_position"])


class CommentaryTests(unittest.TestCase):
    def setUp(self):
        market_watch.FEED.clear()

    def test_commentary_explains_wait_and_news(self):
        zone = {"type": "BULLISH", "top": 10.0, "bottom": 9.0, "candle_time": 1}
        info = {"intelligence": {"trend": "UP", "adx": 32, "relative_volume": 3.0, "distance_vwap_atr": 3.1},
                "htf_context": {"trend": "UP"}, "market": {"funding_rate": 0.0008},
                "zones": [zone], "assessments": {"1BULLISH": {"status": "ENTRY APPROACHING", "score": 90, "rejections": ["no_recent_retest"]}},
                "news": {"coin": {"count": 1, "count_24h": 1, "label": "BULLISH", "headlines": [{"title": "X listed on Binance"}]}}}
        c = market_watch.commentary("XUSDT", info)
        text = " ".join(c["lines"])
        for phrase in ("тренд сильный", "VWAP", "толпа в лонгах", "X listed on Binance"):
            self.assertIn(phrase, text)
        self.assertIn("ретест", c["verdict"])
        self.assertEqual(c["bias"], "LONG")

    def test_critical_news_is_called_out(self):
        c = market_watch.commentary("XUSDT", {"news": {"coin": {"critical": True, "critical_titles": ["X hacked"]}}})
        self.assertTrue(any("Критичная" in line for line in c["lines"]))

    def test_feed_records_transitions_newest_first(self):
        before = {"assessments": {"k": {"status": "POTENTIAL"}}, "intelligence": {"relative_volume": 1}}
        after = {"assessments": {"k": {"status": "ENTRY APPROACHING", "score": 88}}, "zones": [], "intelligence": {"relative_volume": 4}}
        market_watch.observe("XUSDT", before, after, now=100)
        market_watch.observe("YUSDT", {}, {"signal": {"signal": "SHORT", "score": 90, "entry": 2, "sl": 2.1, "tp": 1.7, "signal_id": "s"}}, now=200)
        kinds = [e["kind"] for e in market_watch.feed()]
        self.assertEqual(kinds[0], "signal")
        self.assertIn("setup", kinds)
        self.assertIn("volume", kinds)

    def test_brief_ranks_movers_and_regime(self):
        market = {"AUSDT": {"eligible": True, "change_24h": 0.2, "breadth": 0.7, "funding_rate": 0.001},
                  "BUSDT": {"eligible": True, "change_24h": -0.1, "breadth": 0.7, "funding_rate": -0.0005}}
        brief = market_watch.market_brief({}, market, {"BTCUSDT": {"trend": "UP", "adx": 30}})
        self.assertEqual(brief["regime"], "risk-on")
        self.assertEqual(brief["gainers"][0]["symbol"], "AUSDT")
        self.assertEqual(brief["losers"][0]["symbol"], "BUSDT")
        self.assertIn("Обзор рынка", market_watch.format_brief(brief))


if __name__ == "__main__":
    unittest.main()
