"""Closed, contiguous numeric OHLCV; explicit decision time for live and replay."""
import numpy as np
from trading.context import closed_candles


def validate_ohlcv(df, interval):
    if df.empty:
        raise ValueError('empty_candles')
    columns = ['timestamp','open','high','low','close','volume']
    values = df[columns].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError('nonfinite_candles')
    if (df[['open','high','low','close']] <= 0).any().any() or (df.volume < 0).any():
        raise ValueError('invalid_candle_values')
    if (df.high < df[['open','low','close']].max(axis=1)).any() or (df.low > df[['open','high','close']].min(axis=1)).any():
        raise ValueError('invalid_candle_geometry')
    if not str(interval).isdigit() or int(interval) <= 0:
        raise ValueError('unsupported_numeric_interval')
    step = int(interval)*60000
    if len(df) > 1 and not (np.diff(df.timestamp.to_numpy()) == step).all():
        raise ValueError('noncontiguous_candles')


def decision_candles(df, interval, now_ms, minimum=200):
    # Validate raw timestamps before closed_candles can deduplicate/sort them.
    validate_ohlcv(df, interval)
    # A response begun before a boundary cannot prove the final close at it.
    as_of = min(now_ms, df.attrs.get('observed_at_ms', now_ms))
    closed = closed_candles(df, interval, as_of)
    if len(closed) < minimum:
        raise ValueError('insufficient_closed_candles')
    step = int(interval)*60000
    age = now_ms-(int(closed.iloc[-1].timestamp)+step)
    if not 0 <= age < step:
        raise ValueError('stale_closed_candles')
    return closed


def observed_price(df, interval, now_ms):
    """Latest ticker-like candle close, only for price observation, never setup."""
    if df.empty or 'timestamp' not in df or not str(interval).isdigit():
        return None
    row = df.iloc[-1]
    try:
        age = now_ms-float(row.timestamp)
        price = float(row.close)
        if np.isfinite(price) and price > 0 and 0 <= age < int(interval)*60000+30000:
            return price
    except (TypeError,ValueError):
        pass
    return None
