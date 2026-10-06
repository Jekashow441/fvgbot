import asyncio
import time
from typing import Dict, Any, Optional

import aiohttp
import pandas as pd

from core.settings import cfg

_CACHE: Dict[str, Dict[str, Any]] = {}
_SESSION: Optional[aiohttp.ClientSession] = None
_SESSION_LOOP: Optional[asyncio.AbstractEventLoop] = None


async def public_get(path: str, params: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            session = await _get_session()
            async with session.get("https://api.bybit.com" + path, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit {path}: {data.get('retMsg')}")
            return data.get("result", {})
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            from trading.telemetry import event
            event('API_REQUEST_FAILED',path=path,symbol=params.get('symbol'),timeframe=params.get('interval'),attempt=attempt+1,error=type(exc).__name__)
            if attempt + 1 == retries:
                raise
            await asyncio.sleep(2 ** attempt)


async def get_history(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Explicit bounded pagination. Missing pages raise instead of returning success."""
    rows, cursor = [], int(end_ms)
    while cursor >= start_ms:
        result = await public_get("/v5/market/kline", {"category": "linear", "symbol": symbol,
            "interval": interval, "start": int(start_ms), "end": cursor, "limit": 1000})
        page = result.get("list") or []
        if not page:
            break
        oldest = min(int(row[0]) for row in page)
        if oldest > cursor:
            raise RuntimeError("History pagination did not advance")
        rows.extend(page)
        cursor = oldest - 1
        if len(page) < 1000:
            break
        await asyncio.sleep(0.1)
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
    for column in frame:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
    return frame.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


async def get_coin_microstructure(symbol: str) -> dict:
    # Optional OI failure must not discard an independently valid orderbook.
    book, oi = await asyncio.gather(
        public_get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": 1000}),
        public_get("/v5/market/open-interest", {"category": "linear", "symbol": symbol, "intervalTime": "1h", "limit": 25}),return_exceptions=True)
    if isinstance(book,Exception):raise book
    return {"book": book, "book_limit":1000,"open_interest": [] if isinstance(oi,Exception) else oi.get("list", []), "oi_status":'UNAVAILABLE' if isinstance(oi,Exception) else 'OK',"observed_at": int(time.time()*1000)}


async def get_market_tickers(retries: int = 3) -> list:
    """Fetch the universe snapshot with bounded retries.

    Tickers are the prerequisite for every symbol scan. A transient exchange
    or network error should not discard the entire cycle immediately.
    """
    last_exc = None
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            session = await _get_session()
            async with session.get("https://api.bybit.com/v5/market/tickers", params={"category": "linear"}) as resp:
                resp.raise_for_status()
                data = await resp.json()
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit tickers: {data.get('retMsg')}")
            rows = data.get("result", {}).get("list") or []
            if not rows:
                raise ValueError("empty_ticker_response")
            return rows
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            last_exc = exc
            from trading.telemetry import event
            event('API_REQUEST_FAILED', path='/v5/market/tickers', symbol=None,
                  timeframe=None, attempt=attempt + 1, error=type(exc).__name__)
            if attempt + 1 < attempts:
                await asyncio.sleep(min(2 ** attempt, 8))
    raise last_exc or RuntimeError("market_tickers_failed")


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION, _SESSION_LOOP
    loop = asyncio.get_running_loop()
    if _SESSION is None or _SESSION.closed or _SESSION_LOOP is not loop:
        timeout = aiohttp.ClientTimeout(total=cfg.bybit_request_timeout)
        _SESSION = aiohttp.ClientSession(timeout=timeout)
        _SESSION_LOOP = loop
    return _SESSION


async def close_bybit_session() -> None:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None


def _cache_get(key: str) -> Optional[pd.DataFrame]:
    cached = _CACHE.get(key)
    if not cached:
        return None
    interval = key.rsplit('_',2)[-2]
    same_bar = not interval.isdigit() or int(cached['time']//(int(interval)*60)) == int(time.time()//(int(interval)*60))
    if same_bar and time.time() - cached["time"] < cfg.bybit_cache_ttl:
        return cached["df"].copy()
    _CACHE.pop(key, None)
    return None


def invalidate_klines(symbol: str, interval: str, limit: int) -> None:
    _CACHE.pop(f"{symbol}_{interval}_{max(3, min(int(limit), 1000))}", None)


def _cache_set(key: str, df: pd.DataFrame) -> None:
    _CACHE[key] = {"time": time.time(), "df": df.copy()}


async def get_klines_async(symbol: str, interval: str, limit: int = 200, retries: int = 3) -> pd.DataFrame:
    limit = max(3, min(int(limit), 1000))
    cache_key = f"{symbol}_{interval}_{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}

    for attempt in range(retries):
        try:
            request_started_ms = int(time.time()*1000)
            session = await _get_session()
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit retCode={data.get('retCode')} retMsg={data.get('retMsg')}")

            raw_klines = data.get("result", {}).get("list") or []
            if not raw_klines:
                raise ValueError('empty_candle_response')

            df = pd.DataFrame(raw_klines[::-1], columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
            for col in ["timestamp", "open", "high", "low", "close", "volume", "turnover"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
            df.attrs['observed_at_ms'] = request_started_ms
            _cache_set(cache_key, df)
            return df.copy()
        except Exception as e:
            from trading.telemetry import event
            event('API_REQUEST_FAILED',path='/v5/market/kline',symbol=symbol,timeframe=interval,attempt=attempt+1,error=type(e).__name__)
            if attempt == retries - 1:
                print(f"Error fetching klines for {symbol} after {retries} attempts: {e}")
            else:
                await asyncio.sleep(min(2 ** attempt, 8))

    return pd.DataFrame()


def get_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(get_klines_async(symbol, interval, limit))
    raise RuntimeError("get_klines() cannot be called from an active event loop; use get_klines_async().")
