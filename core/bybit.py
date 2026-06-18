import asyncio
import aiohttp
import pandas as pd
import time
from typing import Dict, Any

# Simple memory cache to avoid spamming Bybit with exact same requests
_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 3.0  # seconds

async def get_klines_async(symbol: str, interval: str, limit: int = 200, retries: int = 3) -> pd.DataFrame:
    """
    Fetches klines from Bybit linear futures API asynchronously.
    Returns a pandas DataFrame. Uses caching and retry logic.
    """
    cache_key = f"{symbol}_{interval}_{limit}"
    
    # Check cache
    if cache_key in _CACHE:
        cached = _CACHE[cache_key]
        if time.time() - cached["time"] < CACHE_TTL:
            return cached["df"].copy()
            
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=5) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    
                    if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                        # Bybit returns newest first, so we reverse it to chronological order
                        klines = data["result"]["list"][::-1]
                        df = pd.DataFrame(klines, columns=[
                            "timestamp", "open", "high", "low", "close", "volume", "turnover"
                        ])
                        df["timestamp"] = pd.to_numeric(df["timestamp"])
                        df["open"] = pd.to_numeric(df["open"])
                        df["high"] = pd.to_numeric(df["high"])
                        df["low"] = pd.to_numeric(df["low"])
                        df["close"] = pd.to_numeric(df["close"])
                        df["volume"] = pd.to_numeric(df["volume"])
                        
                        # Save to cache
                        _CACHE[cache_key] = {"time": time.time(), "df": df}
                        return df.copy()
        except Exception as e:
            if attempt == retries - 1:
                print(f"Error fetching klines for {symbol} after {retries} attempts: {e}")
            else:
                await asyncio.sleep(2 ** attempt) # Exponential backoff
                
    return pd.DataFrame()

# Fallback sync wrapper if needed anywhere (though we should avoid using it in async contexts)
def get_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    """
    Synchronous fallback wrapper. Avoid using inside async loops.
    """
    try:
        loop = asyncio.get_running_loop()
        # If we are already in an event loop, we shouldn't run this synchronously.
        # But we can try to use run_coroutine_threadsafe or just raise an error.
        print(f"Warning: get_klines called synchronously from within an async loop for {symbol}!")
    except RuntimeError:
        pass
        
    return asyncio.run(get_klines_async(symbol, interval, limit))
