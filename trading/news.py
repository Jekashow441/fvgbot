"""Source-linked event risk. Headlines are evidence to inspect, never instructions."""
import asyncio
from contextlib import closing
import hashlib
import html
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
import aiohttp
from core.settings import cfg, DATA_DIR
from core.bybit import public_get

NEWS_DB = Path(DATA_DIR)/"news.sqlite3"
NEWS_STATE = {"sources": {}, "updated_at": None}
ALIASES = {"BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"],
           "XRP": ["ripple"], "DOGE": ["dogecoin"], "ADA": ["cardano"], "AVAX": ["avalanche"],
           "BNB": ["binance coin"], "DOT": ["polkadot"], "LINK": ["chainlink"]}
AMBIGUOUS = {"ONE", "GAS", "NEAR", "W", "S", "ON", "OFF", "MOVE", "TRUMP", "THE", "US", "FUN"}
RULES = [
    ("security", r"\b(hack(?:ed)?|exploit(?:ed)?|security breach|stolen funds)\b", "high", "Возможны отток средств, падение ликвидности и резкое расширение спреда; направление не гарантировано."),
    ("delisting", r"\b(delist(?:ing)?|trading suspension|suspend(?:s|ed)? withdrawals)\b", "high", "Возможны потеря ликвидности, разрывы цены и ограничения торговли."),
    ("macro", r"\b(fomc|monetary policy|interest rate|federal funds|inflation|consumer price index|nonfarm payrolls)\b", "high", "Изменение ожиданий по ставкам может переоценить риск во всём рынке; реакцию нужно подтвердить ценой."),
    ("regulation", r"\b(lawsuit|indictment|sec approves|sec rejects|regulatory approval|etf approval)\b", "medium", "Возможна переоценка регуляторного риска и спроса; заголовок не определяет итоговую реакцию."),
    ("supply", r"\b(token unlock|token burn|unlocking tokens)\b", "medium", "Изменение доступного предложения может усилить продажи или спрос; важны размер события и ожидания."),
    ("listing", r"\b(listing|mainnet|network upgrade|hard fork)\b", "medium", "Возможен всплеск интереса и объёма, включая продажу после выхода новости."),
]


def plain(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def classify(title):
    for kind, pattern, severity, mechanism in RULES:
        if re.search(pattern, title, re.I):
            return {"kind": kind, "severity": severity, "possible_impact": mechanism, "method": "headline_keywords_not_verified_event"}
    return {"kind": "other", "severity": "unknown", "possible_impact": "Недостаточно сведений для оценки влияния; требуется контекст и реакция рынка.", "method": "headline_keywords_not_verified_event"}


def matches_symbol(title, symbol):
    base = re.sub(r"^\d+", "", symbol.removesuffix("USDT"))
    if not base:
        return False
    if any(re.search(r"\b"+re.escape(alias)+r"\b", title, re.I) for alias in ALIASES.get(base, [])):
        return True
    if re.search(r"\$"+re.escape(base)+r"\b|\b"+re.escape(symbol)+r"\b", title):
        return True
    return len(base) >= 3 and base not in AMBIGUOUS and bool(re.search(r"\b"+re.escape(base)+r"\b", title))


def parse_rss(payload, source, observed_at):
    if len(payload) > 2_000_000 or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("Unsupported or oversized RSS document")
    root = ET.fromstring(payload)
    rows = []
    for item in root.findall(".//item")[:200]:
        title, url, date = plain(item.findtext("title")), item.findtext("link", ""), item.findtext("pubDate")
        try:
            dt = parsedate_to_datetime(date)
            if dt.tzinfo is None:
                continue
            published = int(dt.timestamp()*1000)
        except (ValueError, TypeError, OverflowError):
            continue
        if not title or urlparse(url).scheme != "https" or published > observed_at:
            continue
        rows.append({"title": title[:500], "url": url, "source": source, "published_at": published,
                     "observed_at": observed_at, **classify(title)})
    return rows


def archive_events(rows, db_path=None):
    path = Path(db_path or NEWS_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, published_at INTEGER, observed_at INTEGER, payload TEXT)")
        for row in rows:
            # A revised title gets its own observation time. Never backdate updated content.
            key = hashlib.sha256((row["url"]+row["title"]).encode()).hexdigest()
            con.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)", (key, row["published_at"], row["observed_at"], json.dumps(row, ensure_ascii=False)))


async def refresh_news():
    now = int(time.time()*1000)
    async def feed(url):
        try:
            if urlparse(url).scheme != "https":
                raise ValueError("RSS requires HTTPS")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
                async with session.get(url, headers={"User-Agent": "FVGResearchBot/3.0 RSS reader"}) as response:
                    response.raise_for_status()
                    payload = await response.content.read(2_000_001)
            rows = parse_rss(payload, url, now)
            if not rows:
                raise ValueError("Feed contains no usable dated items")
            archive_events(rows)
            NEWS_STATE["sources"][url] = {"status": "OK", "checked_at": now, "items": len(rows)}
        except Exception as exc:
            NEWS_STATE["sources"][url] = {"status": "ERROR", "checked_at": now, "error": type(exc).__name__}
    async def announcements():
        source = "Bybit announcements"
        try:
            result = await public_get("/v5/announcements/index", {"locale": "en-US", "limit": 50})
            rows = []
            for item in result.get("list", []):
                title = plain(item.get("title"))
                published = int(item.get("publishTime") or 0)
                url = item.get("url", "")
                if not title or published <= 0 or published > now or urlparse(url).scheme != "https":
                    continue
                rows.append({"title": title[:500], "url": url, "source": source, "published_at": published,
                             "observed_at": now, **classify(title)})
            if not rows:
                raise ValueError("No dated announcements")
            archive_events(rows)
            NEWS_STATE["sources"][source] = {"status": "OK", "checked_at": now, "items": len(rows)}
        except Exception as exc:
            NEWS_STATE["sources"][source] = {"status": "ERROR", "checked_at": now, "error": type(exc).__name__}
    await asyncio.gather(*(feed(url) for url in cfg.news_feeds), announcements())
    NEWS_STATE["updated_at"] = now


def news_context(symbol, as_of=None, db_path=None):
    now = int(time.time()*1000) if as_of is None else as_of
    path = Path(db_path or NEWS_DB)
    rows = []
    if path.exists():
        with closing(sqlite3.connect(path)) as con, con:
            try:
                rows = [json.loads(r[0]) for r in con.execute(
                    "SELECT payload FROM events WHERE observed_at<=? AND published_at<=? AND published_at>=? ORDER BY published_at DESC",
                    (now, now, now-cfg.news_max_age_hours*3600000))]
            except sqlite3.OperationalError:
                rows = []
    events, seen = [], set()
    for row in rows:
        # Macro events affect all pairs. Other stories require explicit asset attribution.
        if row["kind"] != "macro" and not matches_symbol(row["title"], symbol):
            continue
        key = row["title"].casefold()
        if key in seen:
            continue
        seen.add(key)
        events.append(row)
    fresh = [name for name, state in NEWS_STATE["sources"].items() if state["status"] == "OK" and 0 <= now-state["checked_at"] <= cfg.news_refresh_seconds*2*1000]
    expected = set(cfg.news_feeds) | {"Bybit announcements"}
    coverage = "AVAILABLE" if expected.issubset(fresh) else "PARTIAL" if fresh else "UNAVAILABLE"
    blockers = []
    sufficient = coverage == "AVAILABLE"
    if cfg.research_gate_mode == "paper":
        sufficient = "Bybit announcements" in fresh and len(fresh) >= 2
    if cfg.news_require_coverage and not sufficient:
        blockers.append("news_coverage_incomplete")
    if any(e["severity"] == "high" and now-e["published_at"] < cfg.news_blackout_minutes*60000 for e in events):
        blockers.append("recent_high_impact_headline")
    return {"coverage": coverage, "blockers": blockers, "events": events[:10], "sources_ok": fresh,
            "interpretation": "Keyword screening; absence of matching headlines does not mean absence of news."}
