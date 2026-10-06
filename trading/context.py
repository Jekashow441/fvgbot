"""Causal structure and market-wide quote-volume context; no sentiment claims."""
import math
import pandas as pd


def closed_candles(df, interval, now_ms=None):
    if df.empty:
        return df.copy()
    now = pd.Timestamp.now(tz="UTC") if now_ms is None else pd.Timestamp(now_ms, unit="ms", tz="UTC")
    starts = pd.to_datetime(df.timestamp, unit="ms", utc=True)
    if str(interval).isdigit():
        ends = starts + pd.Timedelta(minutes=int(interval))
    elif interval in ("D", "W"):
        ends = starts + pd.Timedelta(days=1 if interval == "D" else 7)
    elif interval == "M":
        ends = starts.map(lambda x: x + pd.offsets.MonthBegin(1))
    else:
        raise ValueError(f"Unsupported timeframe: {interval}")
    return df.loc[ends <= now].sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def structure_context(df, pivot=3):
    """Pivots become available only after `pivot` right-hand candles close."""
    highs, lows = [], []
    direction, event = "RANGE", None
    broken_high, broken_low = set(), set()
    high_values, low_values, close_values = (df[c].to_numpy() for c in ("high", "low", "close"))
    for i in range(2 * pivot, len(df)):
        k = i - pivot
        high_window, low_window = high_values[k-pivot:k+pivot+1], low_values[k-pivot:k+pivot+1]
        if high_values[k] == high_window.max() and (high_window == high_values[k]).sum() == 1:
            highs.append((k, float(high_values[k])))
        if low_values[k] == low_window.min() and (low_window == low_values[k]).sum() == 1:
            lows.append((k, float(low_values[k])))
        close = float(close_values[i])
        if highs and close > highs[-1][1] and highs[-1][0] not in broken_high:
            event = "CHOCH_UP" if direction == "DOWN" else "BOS_UP"
            direction = "UP"
            broken_high.add(highs[-1][0])
        elif lows and close < lows[-1][1] and lows[-1][0] not in broken_low:
            event = "CHOCH_DOWN" if direction == "UP" else "BOS_DOWN"
            direction = "DOWN"
            broken_low.add(lows[-1][0])
    last = df.iloc[-1] if len(df) else None
    sweep = None
    if last is not None and lows and last.low < lows[-1][1] < last.close:
        sweep = "SELL_SIDE"
    if last is not None and highs and last.high > highs[-1][1] > last.close:
        sweep = "BUY_SIDE"
    return {"direction": direction, "event": event, "sweep": sweep,
            "swing_high": highs[-1][1] if highs else None,
            "swing_low": lows[-1][1] if lows else None}


def ticker_context(rows, min_turnover, max_spread):
    market = {}
    for row in rows:
        symbol = row.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        try:
            turnover, bid, ask, change = (float(row.get(k) or 0) for k in
                                         ("turnover24h", "bid1Price", "ask1Price", "price24hPcnt"))
            if not all(math.isfinite(x) for x in (turnover, bid, ask, change)) or turnover <= 0 or bid <= 0 or ask < bid:
                continue
            spread = (ask-bid) / ((ask+bid)/2) * 10000
        except (ValueError, TypeError):
            continue
        market[symbol] = {"turnover_24h": turnover, "spread_bps": spread, "change_24h": change,
                          "eligible": turnover >= min_turnover and spread <= max_spread}
        for source, name in (("fundingRate", "funding_rate"), ("openInterestValue", "open_interest_usdt"), ("nextFundingTime", "next_funding_ms")):
            try:
                number = float(row[source])
                market[symbol][name] = number if math.isfinite(number) else None
            except (KeyError, TypeError, ValueError):
                market[symbol][name] = None
    total = sum(x["turnover_24h"] for x in market.values())
    breadth = sum(x["turnover_24h"] for x in market.values() if x["change_24h"] > 0) / total if total else None
    for rank, (_, item) in enumerate(sorted(market.items(), key=lambda x: -x[1]["turnover_24h"]), 1):
        item.update(volume_rank=rank, volume_share=item["turnover_24h"]/total, breadth=breadth)
    return market


def trendline_context(df, pivot=3, atr=0, tolerance=0.25):
    """Two confirmed pivots define a line; subsequent closes can invalidate it."""
    import numpy as np
    result = {"support": None, "resistance": None, "bullish_retest": False, "bearish_retest": False}
    if len(df) < pivot*2+3 or not np.isfinite(atr) or atr <= 0:
        return result
    d = df.tail(120).reset_index(drop=True)
    last_index = len(d)-1
    for column, name, rising in (("low", "support", True), ("high", "resistance", False)):
        values = d[column].to_numpy()
        points = []
        for k in range(pivot, len(d)-pivot):
            window = values[k-pivot:k+pivot+1]
            extreme = window.min() if rising else window.max()
            if values[k] == extreme and (window == values[k]).sum() == 1:
                points.append(k)
        if len(points) < 2:
            continue
        a, b = points[-2:]
        slope = (values[b]-values[a])/(b-a)
        if (rising and slope <= 0) or (not rising and slope >= 0):
            continue
        projected = values[b]+slope*(last_index-b)
        indices = np.arange(b+pivot, len(d))
        line = values[b]+slope*(indices-b)
        closes = d.close.to_numpy()[indices]
        broken = (closes < line-atr*tolerance).any() if rising else (closes > line+atr*tolerance).any()
        if broken:
            continue
        result[name] = {"price": float(projected), "slope": float(slope), "anchors": [int(d.iloc[a].timestamp), int(d.iloc[b].timestamp)]}
        last = d.iloc[-1]
        if rising:
            result["bullish_retest"] = bool(abs(last.low-projected) <= atr*tolerance and last.close > projected and last.close > last.open)
        else:
            result["bearish_retest"] = bool(abs(last.high-projected) <= atr*tolerance and last.close < projected and last.close < last.open)
    return result
