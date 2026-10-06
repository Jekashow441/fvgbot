import pandas as pd
import numpy as np
from typing import Dict, Optional, List

from core.settings import cfg as default_cfg
from trading.context import structure_context, trendline_context
from trading.market_reasoning import market_state, setup_reasoning


def _calc_ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].ewm(span=period, adjust=False).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = _wilder(delta.clip(lower=0), period)
    loss = _wilder((-delta).clip(lower=0), period)
    rs = gain / loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).mask((loss == 0) & (gain > 0), 100).mask((loss == 0) & (gain == 0), 50)


def _calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = np.abs(df["high"] - df["close"].shift())
    low_close = np.abs(df["low"] - df["close"].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    return ranges.max(axis=1).rolling(period).mean()


def _calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    # Wilder smoothing: the 20/25/30 ADX regime thresholds are defined on it.
    atr = _wilder(tr, period).replace(0, np.nan)
    plus_di = 100 * _wilder(plus_dm, period) / atr
    minus_di = 100 * _wilder(minus_dm, period) / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    return _wilder(dx, period)


def _check_displacement(candle: pd.Series, settings=None) -> bool:
    cfg = settings or default_cfg
    body = abs(candle["close"] - candle["open"])
    wick_to_wick = candle["high"] - candle["low"]
    if wick_to_wick <= 0:
        return False
    return (body / wick_to_wick) * 100 >= cfg.fvg_min_body_pct


def market_context(df: pd.DataFrame, settings=None) -> Dict:
    cfg = settings or default_cfg
    """Return compact trend/volatility context for the latest candle."""
    if df.empty or len(df) < 60:
        return {"trend": "UNKNOWN", "adx": None, "rsi": None, "atr_pct": None}

    d = df.copy().reset_index(drop=True)
    d["ema_fast"] = _calc_ema(d, 50)
    d["ema_slow"] = _calc_ema(d, cfg.ema_period)
    d["rsi"] = _calc_rsi(d)
    d["atr"] = _calc_atr(d)
    d["adx"] = _calc_adx(d)
    last = d.iloc[-1]
    close = float(last["close"])

    if close > last["ema_slow"] and last["ema_fast"] > last["ema_slow"]:
        trend = "UP"
    elif close < last["ema_slow"] and last["ema_fast"] < last["ema_slow"]:
        trend = "DOWN"
    else:
        trend = "RANGE"

    atr_pct = None
    if not pd.isna(last["atr"]) and close > 0:
        atr_pct = float(last["atr"] / close * 100)

    return {
        "trend": trend,
        "adx": None if pd.isna(last["adx"]) else float(last["adx"]),
        "rsi": None if pd.isna(last["rsi"]) else float(last["rsi"]),
        "atr_pct": atr_pct,
    }


def detect_fvgs(df: pd.DataFrame, settings=None, prepared=False) -> List[Dict]:
    cfg = settings or default_cfg
    if df.empty or len(df) < 3 + cfg.fvg_confirmations + 20:
        return []
    volume_mean = df["vol_sma"].to_numpy() if prepared else df["volume"].shift(1).rolling(20).mean().to_numpy()
    prior_atr = df["atr_previous"].to_numpy() if prepared else _calc_atr(df).shift(1).to_numpy()
    op, hi, lo, cl, vol, ts = (df[c].to_numpy() for c in ("open", "high", "low", "close", "volume", "timestamp"))
    zones = []
    start = len(df)-1-max(1, cfg.fvg_confirmations)
    stop = max(2, len(df)-cfg.fvg_lookback-1)
    for i in range(start, stop, -1):
        k = i-1
        body, span = abs(cl[k]-op[k]), hi[k]-lo[k]
        if span <= 0 or body/span*100 < cfg.fvg_min_body_pct or not np.isfinite(prior_atr[k]) or body < cfg.displacement_atr*prior_atr[k]:
            continue
        if cfg.fvg_require_volume and (not np.isfinite(volume_mean[k]) or vol[k] <= volume_mean[k]):
            continue
        if lo[i] > hi[i-2] and cl[k] > op[k]:
            kind, bottom, top, denominator = "BULLISH", hi[i-2], lo[i], hi[i-2]
        elif hi[i] < lo[i-2] and cl[k] < op[k]:
            kind, bottom, top, denominator = "BEARISH", hi[i], lo[i-2], lo[i-2]
        else:
            continue
        if denominator <= 0:
            continue
        gap = (top-bottom)/denominator*100
        if gap < cfg.fvg_min_size_pct or top-bottom > cfg.fvg_max_size_atr*prior_atr[k]:
            continue
        ce = (top+bottom)/2
        if kind == "BULLISH":
            invalid = (lo[i+1:] <= bottom).any() or (cfg.fvg_strict_mitigation and (cl[i+1:] < ce).any())
        else:
            invalid = (hi[i+1:] >= top).any() or (cfg.fvg_strict_mitigation and (cl[i+1:] > ce).any())
        if invalid:
            continue
        visits = (lo[i+1:] <= top) & (hi[i+1:] >= bottom)
        touches = int((visits & ~np.r_[False, visits[:-1]]).sum())
        penetration = top-float(lo[i+1:].min()) if kind == "BULLISH" else float(hi[i+1:].max())-bottom
        fill_fraction = max(0.0, min(1.0, penetration/(top-bottom)))
        zones.append({"type": kind, "top": float(top), "bottom": float(bottom), "ce": float(ce),
                      "candle_time": int(ts[k]), "formed_at": int(ts[i]), "gap_size": float(gap), "index": i,
                      "touches": touches, "touch_bars": int(visits.sum()), "fill_fraction": fill_fraction, "displacement_atr": float(body/prior_atr[k]),
                      "age": len(df)-1-i, "state": "PARTIAL" if fill_fraction > 0 else "RETESTED" if touches else "FRESH"})
    return zones


def detect_fvg(df: pd.DataFrame) -> Optional[Dict]:
    zones = detect_fvgs(df)
    return zones[0] if zones else None


def _safe_dynamic_rr(base_rr: float, atr: float, price: float, adx: Optional[float], settings=None) -> float:
    cfg = settings or default_cfg
    if price <= 0 or pd.isna(atr) or atr <= 0:
        return float(base_rr)
    if not cfg.dynamic_rr:
        return float(base_rr)

    volatility_pct = atr / price * 100
    vol_factor = max(0.75, min(volatility_pct, 1.4))
    trend_factor = 1.1 if adx is not None and adx >= cfg.adx_strong else 1.0
    rr = base_rr * vol_factor * trend_factor
    return float(min(cfg.rr_max, max(cfg.rr_min, rr)))


def _score_signal(side: str, fvg: Dict, ctx: Dict, htf_ctx: Optional[Dict], rsi: Optional[float], adx: Optional[float], settings=None,
                  structure: Optional[Dict] = None, trendline_retest: bool = False,
                  relative_volume: Optional[float] = None) -> tuple[int, List[str]]:
    cfg = settings or default_cfg
    score = 50
    factors: List[str] = []
    expected = "UP" if side == "LONG" else "DOWN"

    if structure:
        if structure.get("direction") == expected:
            score += 8
            factors.append("structure_" + expected.lower())
        if structure.get("sweep") == ("SELL_SIDE" if side == "LONG" else "BUY_SIDE"):
            score += 8
            factors.append("liquidity_sweep")

    if trendline_retest:
        score += 8
        factors.append("trendline_retest")

    if fvg.get("touches", 0) <= 1 and fvg.get("fill_fraction", 0) < 0.5:
        score += 5
        factors.append("fresh_zone")

    if fvg.get("displacement_atr", 0) >= 1.5:
        score += 5
        factors.append("strong_displacement")

    if relative_volume is not None and relative_volume >= 1.5:
        score += 5
        factors.append("high_rvol")

    if fvg.get("gap_size", 0) >= cfg.fvg_min_size_pct * 2:
        score += 10
        factors.append("large_fvg")

    if side == "LONG" and ctx.get("trend") == "UP":
        score += 12
        factors.append("ltf_uptrend")
    if side == "SHORT" and ctx.get("trend") == "DOWN":
        score += 12
        factors.append("ltf_downtrend")

    if htf_ctx:
        if side == "LONG" and htf_ctx.get("trend") == "UP":
            score += 18
            factors.append("htf_uptrend")
        elif side == "SHORT" and htf_ctx.get("trend") == "DOWN":
            score += 18
            factors.append("htf_downtrend")
        elif htf_ctx.get("trend") not in ("UNKNOWN", "RANGE"):
            score -= 25
            factors.append("htf_against")

    if adx is not None and adx >= cfg.adx_strong:
        score += 10
        factors.append("strong_adx")
    elif adx is not None and adx < 15:
        score -= 10
        factors.append("weak_adx")

    if rsi is not None:
        if side == "LONG" and 35 <= rsi <= 65:
            score += 5
            factors.append("healthy_rsi")
        if side == "SHORT" and 35 <= rsi <= 65:
            score += 5
            factors.append("healthy_rsi")

    return max(0, min(100, score)), factors


def validate_signal(df: pd.DataFrame, fvg: Optional[Dict], htf_ctx: Optional[Dict] = None, market: Optional[Dict] = None, settings=None, diagnostics=None) -> Optional[Dict]:
    def reject(reason):
        if diagnostics is not None:
            diagnostics[reason] = diagnostics.get(reason, 0)+1
        return None
    cfg = settings or default_cfg
    if not fvg or df.empty or len(df) < 30:
        return reject("missing_setup")

    side = "LONG" if fvg["type"] == "BULLISH" else "SHORT"
    last = df.iloc[-1]
    last_close = float(last["close"])
    # Balanced entries may confirm a recent retest instead of requiring another touch.
    recent = df.tail(cfg.retest_window_bars if cfg.strategy_profile in ("balanced", "contextual") else 1)
    touched = recent[(recent.low <= fvg["top"]) & (recent.high >= fvg["bottom"])]
    touched = touched[touched.timestamp > fvg.get("formed_at", fvg["candle_time"])]
    continuation = cfg.strategy_profile == "contextual" and touched.empty and fvg.get("age", 99) <= 2
    if touched.empty and not continuation:
        return reject("no_recent_retest")
    rejection = last if continuation else touched.iloc[-1]
    # Require a rejection close, not simply a candle somewhere above/below the zone.
    if side == "LONG" and (last_close <= last["open"] or last_close < fvg["ce"]):
        return reject("no_bullish_rejection")
    if side == "SHORT" and (last_close >= last["open"] or last_close > fvg["ce"]):
        return reject("no_bearish_rejection")
    if fvg.get("touches", 0) > 2:
        return reject("zone_exhausted")
    if fvg.get("touch_bars", 0) > cfg.fvg_max_touch_bars:
        return reject("prolonged_zone_residence")

    df = df.copy().reset_index(drop=True)
    df["ema_fast"] = _calc_ema(df, 50)
    df["ema_slow"] = _calc_ema(df, cfg.ema_period)
    df["rsi"] = _calc_rsi(df)
    df["atr"] = _calc_atr(df)
    df["adx"] = _calc_adx(df)

    last = df.iloc[-1]
    last_close = float(last["close"])
    atr = last["atr"]
    adx = None if pd.isna(last["adx"]) else float(last["adx"])
    rsi = None if pd.isna(last["rsi"]) else float(last["rsi"])
    ctx = market_context(df, cfg)
    state = market_state(df, adx, float(atr))
    setup = "impulse_continuation" if continuation else "fvg_retest"
    range_reclaim = (cfg.strategy_profile == "contextual" and state["regime"] == "RANGE"
                     and state["liquidity_sweep"] == ("SELL_SIDE" if side == "LONG" else "BUY_SIDE") and not continuation)
    if range_reclaim:
        setup = "range_reclaim"
    if continuation:
        prior = df.iloc[-4:-1]
        breakout = last.close > prior.high.max() if side == "LONG" else last.close < prior.low.min()
        if state["regime"] != "TREND" or state["relative_volume"] < cfg.continuation_min_rvol or not breakout:
            return reject("continuation_not_confirmed")
    if cfg.strategy_profile == "contextual" and state["regime"] == "SHOCK":
        return reject("volatility_shock")

    if pd.isna(last_close) or last_close <= 0 or pd.isna(atr) or atr <= 0:
        return reject("invalid_price_atr")

    baseline = df["volume"].iloc[-21:-1].mean()
    relative_volume = float(last["volume"] / baseline) if baseline > 0 else 0.0
    if not np.isfinite(relative_volume) or relative_volume < cfg.min_relative_volume:
        return reject("low_relative_volume")
    if market is not None and not market.get("eligible", False):
        return reject("illiquid_market")
    if abs(last_close - fvg["ce"]) > atr * cfg.max_entry_distance_atr:
        return reject("entry_too_far")
    structure = structure_context(df, cfg.structure_pivot)
    expected = "UP" if side == "LONG" else "DOWN"
    if cfg.require_structure and structure["direction"] != expected and not range_reclaim:
        if cfg.strategy_profile == "legacy" or structure["direction"] not in ("RANGE", expected):
            return reject("structure_opposition")
    lines = trendline_context(df, cfg.structure_pivot, float(atr), cfg.trendline_tolerance_atr)
    extra_factors = [setup, "regime_" + state["regime"].lower(), "relative_volume"]
    if market and market.get("breadth") is not None:
        breadth = market["breadth"]
        if (side == "LONG" and breadth < 0.2) or (side == "SHORT" and breadth > 0.8):
            return reject("market_breadth_opposition")
        extra_factors.append("market_breadth")

    direction = 1 if side == "LONG" else -1
    if not range_reclaim and (direction * (last_close - last["ema_slow"]) < 0 or direction * (last["ema_fast"] - last["ema_slow"]) < 0):
        return reject("ema_opposition")

    if htf_ctx and htf_ctx.get("trend") == ("DOWN" if side == "LONG" else "UP"):
        return reject("htf_opposition")

    if rsi is not None:
        if (side == "LONG" and rsi > cfg.rsi_overbought) or (side == "SHORT" and rsi < cfg.rsi_oversold):
            return reject("rsi_extreme")

    near_edge, far_edge = (fvg["top"], fvg["bottom"]) if side == "LONG" else (fvg["bottom"], fvg["top"])
    wick = "low" if side == "LONG" else "high"
    if not continuation and not (direction * (near_edge - rejection[wick]) >= 0 and direction * (last_close - far_edge) > 0):
        return reject("no_recent_retest")
    if cfg.fvg_strict_mitigation and direction * (last_close - fvg["ce"]) < 0:
        return reject("ce_invalid")
    sl = float(far_edge)
    if cfg.struct_sl:
        if cfg.strategy_profile in ("balanced", "contextual"):
            swing = float(rejection[wick])
        else:
            recent_wicks = df[wick].tail(cfg.swing_lookback)
            swing = float(recent_wicks.min() if side == "LONG" else recent_wicks.max())
        extreme = min(sl, swing) if side == "LONG" else max(sl, swing)
        sl = extreme - direction * float(atr * cfg.sl_atr_buffer)
    risk = direction * (last_close - sl)
    if pd.isna(risk) or risk <= 0:
        return reject("invalid_risk")
    if cfg.sl_max_atr > 0 and risk > float(atr * cfg.sl_max_atr):
        return reject("stop_too_wide")
    rr = _safe_dynamic_rr(cfg.risk_reward, atr, last_close, adx, cfg)
    score, factors = _score_signal(side, fvg, ctx, htf_ctx, rsi, adx, cfg, structure=structure,
                                   trendline_retest=lines["bullish_retest" if side == "LONG" else "bearish_retest"],
                                   relative_volume=relative_volume)
    factors += extra_factors
    if score < cfg.min_signal_score:
        return reject("score_below_threshold")
    return {"signal": side, "entry": last_close, "sl": float(sl), "tp": float(last_close + direction * risk * rr), "rr": rr, "score": score,
            "rsi": rsi, "adx": adx, "trend": ctx.get("trend"), "htf": None if not htf_ctx else htf_ctx.get("trend"), "factors": factors,
            "structure": structure, "reasoning": setup_reasoning(side, setup, state), "setup": setup, "regime": state["regime"],
            "trendlines": lines, "relative_volume": relative_volume, "market": market, "strategy_version": "structure_v3"}
