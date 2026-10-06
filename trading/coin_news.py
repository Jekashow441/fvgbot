"""Per-coin headline watch with keyword sentiment. Headlines are evidence, never instructions."""
import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote_plus

import aiohttp

from core.settings import cfg, DATA_DIR
from trading.news import parse_rss, matches_symbol, read_capped

COIN_NEWS_DB = Path(DATA_DIR)/"news.sqlite3"
COIN_NEWS_STATE = {}
METHOD = "headline_keywords_not_verified_event"

NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "Ripple", "DOGE": "Dogecoin", "ADA": "Cardano",
    "AVAX": "Avalanche", "BNB": "BNB", "DOT": "Polkadot", "LINK": "Chainlink", "TON": "Toncoin", "TRX": "TRON",
    "SHIB": "Shiba Inu", "PEPE": "Pepe", "LTC": "Litecoin", "BCH": "Bitcoin Cash", "NEAR": "NEAR Protocol",
    "APT": "Aptos", "SUI": "Sui", "ARB": "Arbitrum", "OP": "Optimism", "INJ": "Injective", "TIA": "Celestia",
    "SEI": "Sei", "ATOM": "Cosmos", "FIL": "Filecoin", "ICP": "Internet Computer", "ETC": "Ethereum Classic",
    "UNI": "Uniswap", "AAVE": "Aave", "MKR": "MakerDAO", "LDO": "Lido", "RNDR": "Render", "RENDER": "Render",
    "FET": "Fetch.ai", "TAO": "Bittensor", "WIF": "dogwifhat", "BONK": "Bonk", "FLOKI": "Floki", "JUP": "Jupiter",
    "PYTH": "Pyth Network", "ENA": "Ethena", "ONDO": "Ondo Finance", "STX": "Stacks", "IMX": "Immutable",
    "HBAR": "Hedera", "XLM": "Stellar", "ALGO": "Algorand", "VET": "VeChain", "EOS": "EOS", "XTZ": "Tezos",
    "SAND": "The Sandbox", "MANA": "Decentraland", "AXS": "Axie Infinity", "GALA": "Gala", "APE": "ApeCoin",
    "CRV": "Curve", "DYDX": "dYdX", "GMX": "GMX", "WLD": "Worldcoin", "ORDI": "ORDI", "KAS": "Kaspa",
    "HYPE": "Hyperliquid", "TRUMP": "Official Trump", "POL": "Polygon", "MATIC": "Polygon", "ZEC": "Zcash",
    "XMR": "Monero", "NOT": "Notcoin", "PENGU": "Pudgy Penguins", "VIRTUAL": "Virtuals Protocol",
}

# (pattern, weight, label, critical). Weights are a crude headline tone, not a forecast.
LEXICON = [
    (r"\b(hack(?:ed|er|ers)?|exploit(?:ed)?|drain(?:ed)?|stolen|rug ?pull|security breach)\b", -1.0, "security", True),
    (r"\b(delist(?:s|ed|ing)?|trading (?:halt|suspension)|suspend(?:s|ed)? (?:trading|withdrawals)|halts? withdrawals)\b", -1.0, "delisting", True),
    (r"\b(insolven(?:t|cy)|bankrupt(?:cy)?|collapse[sd]?)\b", -0.9, "insolvency", True),
    (r"\b(sec (?:sues|charges)|charged by|indict(?:ed|ment)|lawsuit|sued)\b", -0.7, "legal", False),
    (r"\b(token unlock|unlocks?|vesting)\b", -0.5, "supply_unlock", False),
    (r"\b(plunge[sd]?|crash(?:es|ed)?|tumble[sd]?|sell-?off|dump(?:s|ed|ing)?|capitulat\w*)\b", -0.6, "selloff", False),
    (r"\b(outage|downtime|network halt|chain halt|bug)\b", -0.5, "outage", False),
    (r"\b(investigation|probe|fine[sd]?|ban(?:s|ned)?)\b", -0.4, "regulatory", False),
    (r"\b(fall(?:s|ing)?|drop(?:s|ped)?|slide[sd]?|decline[sd]?|slump\w*|bearish|outflows?)\b", -0.3, "weakness", False),
    (r"\b(etf approv\w*|approves? (?:spot )?etf|etf launch\w*)\b", 0.8, "etf", False),
    (r"\b(lists?|listing|listed) (?:on|at|by) (?:binance|coinbase|upbit|bybit|okx|kraken|robinhood)\b", 0.8, "listing", False),
    (r"\b(partner(?:s|ship)? with|acquir(?:es|ed|ition)|integrat(?:es|ion)|adopt(?:s|ion))\b", 0.5, "adoption", False),
    (r"\b(mainnet|upgrade|hard fork|launch(?:es|ed)?)\b", 0.4, "development", False),
    (r"\b(buyback|token burn|burns?)\b", 0.5, "supply_reduction", False),
    (r"\b(all-time high|record high|ath|surge[sd]?|soar(?:s|ed)?|rall(?:y|ies|ied)|skyrocket\w*|jump(?:s|ed)?|inflows?)\b", 0.5, "momentum", False),
    (r"\b(rise[sd]?|gain(?:s|ed)?|climb(?:s|ed)?|bullish|rebound\w*)\b", 0.3, "strength", False),
]


def base_asset(symbol):
    return re.sub(r"^\d+", "", symbol.removesuffix("USDT")) or symbol


def search_url(symbol, hours=None):
    base = base_asset(symbol)
    name = NAMES.get(base)
    terms = f'"{name}" OR "{base}"' if name and name != base else f'"{base}"'
    days = max(1, math.ceil((hours or cfg.coin_news_max_age_hours)/24))
    query = quote_plus(f"{terms} crypto when:{days}d")
    return f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


def headline_tone(title):
    score, labels, critical = 0.0, [], False
    for pattern, weight, label, is_critical in LEXICON:
        if re.search(pattern, title, re.I):
            score += weight
            labels.append(label)
            critical = critical or is_critical
    return max(-1.0, min(1.0, score)), labels, critical


def relevant(title, symbol):
    base = base_asset(symbol)
    name = NAMES.get(base)
    # Strip longer asset names first: "Bitcoin" inside "Bitcoin Cash" is a different coin.
    for other, other_name in NAMES.items():
        if other != base and name and other_name.lower().startswith(name.lower()+" "):
            title = re.sub(r"\b"+re.escape(other_name)+r"\b", " ", title, flags=re.I)
    if name and name != base and re.search(r"\b"+re.escape(name)+r"\b", title, re.I):
        return True
    return matches_symbol(title, symbol)


def _schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS coin_events (id TEXT, symbol TEXT, published_at INTEGER, observed_at INTEGER, payload TEXT, PRIMARY KEY(id, symbol))")
    con.execute("CREATE INDEX IF NOT EXISTS coin_events_symbol_time ON coin_events(symbol, published_at)")


def archive(symbol, rows, db_path=None):
    path = Path(db_path or COIN_NEWS_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con, con:
        _schema(con)
        for row in rows:
            key = hashlib.sha256((row["url"]+row["title"]).encode()).hexdigest()
            con.execute("INSERT OR IGNORE INTO coin_events VALUES (?,?,?,?,?)",
                        (key, symbol, row["published_at"], row["observed_at"], json.dumps(row, ensure_ascii=False)))


def ingest(symbol, payload, observed_at, db_path=None):
    rows = []
    for row in parse_rss(payload, "Google News search", observed_at):
        if not relevant(row["title"], symbol):
            continue
        tone, labels, critical = headline_tone(row["title"])
        rows.append(dict(row, tone=tone, tone_labels=labels, critical=critical, method=METHOD))
    archive(symbol, rows, db_path)
    return rows


def coin_news(symbol, as_of=None, db_path=None):
    """Decayed headline tone over the lookback; only items observed by `as_of` count."""
    now = int(time.time()*1000) if as_of is None else as_of
    path = Path(db_path or COIN_NEWS_DB)
    rows = []
    if path.exists():
        with closing(sqlite3.connect(path)) as con, con:
            try:
                rows = [json.loads(r[0]) for r in con.execute(
                    "SELECT payload FROM coin_events WHERE symbol=? AND observed_at<=? AND published_at<=? AND published_at>=? ORDER BY published_at DESC",
                    (symbol, now, now, now-cfg.coin_news_max_age_hours*3600000))]
            except sqlite3.OperationalError:
                rows = []
    seen, events = set(), []
    for row in rows:
        key = re.sub(r"\s+-\s+[^-]+$", "", row["title"]).casefold()
        if key not in seen:
            seen.add(key)
            events.append(row)
    half_life_h = 12
    weights = [0.5 ** ((now-e["published_at"])/3600000/half_life_h) for e in events]
    total = sum(weights)
    sentiment = sum(w*e.get("tone", 0) for w, e in zip(weights, events))/total if total else None
    critical = [e for e in events if e.get("critical") and now-e["published_at"] <= 24*3600000]
    day = sum(1 for e in events if now-e["published_at"] <= 24*3600000)
    state = COIN_NEWS_STATE.get(symbol, {})
    label = None if sentiment is None else "BULLISH" if sentiment >= 0.25 else "BEARISH" if sentiment <= -0.25 else "NEUTRAL"
    return {"symbol": symbol, "sentiment": None if sentiment is None else round(sentiment, 3), "label": label,
            "count": len(events), "count_24h": day, "critical": bool(critical),
            "critical_titles": [e["title"] for e in critical[:3]],
            "headlines": [{k: e.get(k) for k in ("title", "url", "published_at", "tone", "tone_labels", "critical")} for e in events[:8]],
            "checked_at": state.get("checked_at"), "status": state.get("status", "NOT_CHECKED"), "method": METHOD}


def due_symbols(priority, background, now=None):
    """Priority symbols refresh faster; each symbol appears once, most urgent first."""
    now = time.time() if now is None else now
    due = []
    for group, ttl in ((priority, cfg.coin_news_priority_ttl_minutes), (background, cfg.coin_news_ttl_minutes)):
        for symbol in group:
            checked = COIN_NEWS_STATE.get(symbol, {}).get("checked_at_s", 0)
            if symbol not in due and now-checked >= ttl*60:
                due.append(symbol)
    return due


async def fetch(session, symbol):
    now = int(time.time()*1000)
    try:
        async with session.get(search_url(symbol), headers={"User-Agent": "Mozilla/5.0 FVGResearchBot/3.0 RSS reader"}) as response:
            response.raise_for_status()
            payload = await read_capped(response)
        rows = ingest(symbol, payload, now)
        COIN_NEWS_STATE[symbol] = {"status": "OK", "checked_at": now, "checked_at_s": now/1000, "items": len(rows)}
    except Exception as exc:
        # Failed symbols wait a full TTL before retrying so outages cannot hammer the feed.
        COIN_NEWS_STATE[symbol] = {"status": "ERROR", "checked_at": now, "checked_at_s": now/1000, "error": type(exc).__name__}


async def coin_news_worker(priority_source, background_source):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        while True:
            if not (cfg.is_running and cfg.enable_news and cfg.enable_coin_news):
                await asyncio.sleep(10)
                continue
            try:
                due = due_symbols(priority_source(), background_source())
            except Exception:
                due = []
            if not due:
                await asyncio.sleep(15)
                continue
            await fetch(session, due[0])
            await asyncio.sleep(cfg.coin_news_interval_seconds)


def prune(max_age_hours=None, db_path=None, now=None):
    """Headlines older than twice the lookback can no longer influence anything."""
    now = int(time.time()*1000) if now is None else now
    hours = 2*(max_age_hours or cfg.coin_news_max_age_hours)
    path = Path(db_path or COIN_NEWS_DB)
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path)) as con, con:
        _schema(con)
        removed = con.execute("DELETE FROM coin_events WHERE published_at<?", (now-hours*3600000,)).rowcount
        try:
            removed += con.execute("DELETE FROM events WHERE published_at<?", (now-7*86400000,)).rowcount
        except sqlite3.OperationalError:
            pass
    return removed
