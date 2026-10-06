"""Candle replay exits. Conservative ambiguity handling, no future prices."""


def candle_exit(side, sl, tp, bar, held_bars, max_hold_bars=0):
    stop = bar.low <= sl if side == 1 else bar.high >= sl
    target = bar.high >= tp if side == 1 else bar.low <= tp
    if stop:
        return (min(bar.open, sl) if side == 1 else max(bar.open, sl)), "SL"
    if target:
        return tp, "TP"
    if max_hold_bars and held_bars >= max_hold_bars:
        return float(bar.close), "TIMEOUT"
    return None
