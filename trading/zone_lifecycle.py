"""Observed wick invalidation is allowed; unclosed candles never confirm CE."""
from trading.context import closed_candles


def invalidation_reason(zone, frame, timeframe, now_ms, strict_ce=True):
    known_at=min(now_ms,frame.attrs.get('observed_at_ms',now_ms))
    after = frame[(frame.timestamp > zone['formed_at']) & (frame.timestamp <= known_at)]
    if after.empty:
        return None
    if zone['type'] == 'BULLISH':
        if (after.low <= zone['bottom']).any():
            return 'fvg_fully_filled'
    elif (after.high >= zone['top']).any():
        return 'fvg_fully_filled'
    closed = closed_candles(after,timeframe,min(now_ms,frame.attrs.get('observed_at_ms',now_ms)))
    if strict_ce and not closed.empty:
        failed = (closed.close < zone['ce']).any() if zone['type']=='BULLISH' else (closed.close > zone['ce']).any()
        if failed:
            return 'fvg_ce_invalidated'
    return None
