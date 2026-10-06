"""Explainable market/coin diagnostics, not a price prediction model."""
import math
import numpy as np
import pandas as pd
from core.settings import cfg
from trading.strategy import market_context, _calc_atr


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def benchmark_context(coin, benchmark):
    joined = coin[["timestamp", "close"]].merge(benchmark[["timestamp", "close"]], on="timestamp", suffixes=("_coin", "_btc")).tail(97)
    if len(joined) < 50:
        return {"status": "INSUFFICIENT", "correlation": None, "relative_return": None}
    returns = joined[["close_coin", "close_btc"]].pct_change().dropna()
    correlation = finite(returns.close_coin.corr(returns.close_btc))
    variance = returns.close_btc.var()
    beta = finite(returns.close_coin.cov(returns.close_btc)/variance) if variance > 0 else None
    relative = (joined.close_coin.iloc[-1]/joined.close_coin.iloc[0]-1) - (joined.close_btc.iloc[-1]/joined.close_btc.iloc[0]-1)
    return {"status": "OK", "correlation": correlation, "beta": beta, "relative_return": float(relative)}


def coin_context(df, benchmark=None, settings=None):
    settings = settings or cfg
    if len(df) < 60:
        return {"status": "INSUFFICIENT"}
    last = df.iloc[-1]
    atr = float(_calc_atr(df).iloc[-1])
    baseline = df.volume.iloc[-21:-1].mean()
    recent = df.tail(96)
    total = float(recent.volume.sum())
    vwap = float((((recent.high+recent.low+recent.close)/3)*recent.volume).sum()/total) if total > 0 else None
    context = market_context(df, settings)
    context.update(status="OK", atr=atr, relative_volume=float(last.volume/baseline) if baseline > 0 else None,
                   range_atr=float((last.high-last.low)/atr) if atr > 0 else None,
                   rolling_vwap=vwap, distance_vwap_atr=(float(last.close)-vwap)/atr if vwap and atr > 0 else None,
                   resistance=float(df.high.iloc[-97:-1].max()), support=float(df.low.iloc[-97:-1].min()),
                   benchmark=benchmark_context(df, benchmark) if benchmark is not None else {"status": "UNAVAILABLE"})
    context["regime"] = "SHOCK" if context["range_atr"] and context["range_atr"] > 3 else "TREND" if (context["adx"] or 0) >= 25 else "RANGE"
    return context


def microstructure_context(raw, settings=None, now_ms=None):
    settings = settings or cfg
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp()*1000) if now_ms is None else now_ms
    book = raw.get("book", {})
    try:
        if abs(now_ms-int(book["ts"])) > 60000:
            return {"status": "STALE"}
        bids, asks = np.array(book["b"], dtype=float), np.array(book["a"], dtype=float)
        if bids.ndim != 2 or asks.ndim != 2 or not np.isfinite(bids).all() or not np.isfinite(asks).all() or (bids <= 0).any() or (asks <= 0).any():
            return {"status": "INVALID"}
        bid, ask = bids[:, 0].max(), asks[:, 0].min()
        if ask <= bid:
            return {"status": "CROSSED"}
        mid = (bid+ask)/2
        band = settings.book_depth_bps/10000
        b = bids[bids[:, 0] >= mid*(1-band)]
        a = asks[asks[:, 0] <= mid*(1+band)]
        bid_depth, ask_depth = float((b[:, 0]*b[:, 1]).sum()), float((a[:, 0]*a[:, 1]).sum())
    except (KeyError, ValueError, TypeError, IndexError):
        return {"status": "INVALID"}
    oi = sorted([r for r in raw.get("open_interest", []) if finite(r.get("openInterest")) is not None], key=lambda r: int(r["timestamp"]))
    oi_change = None
    if len(oi) >= 2 and 0 <= now_ms-int(oi[-1]["timestamp"]) <= 2*3600000 and float(oi[0]["openInterest"]) > 0:
        oi_change = float(oi[-1]["openInterest"])/float(oi[0]["openInterest"])-1
    return {"status": "OK", "bid": float(bid), "ask": float(ask), "spread_bps": float((ask-bid)/mid*10000),
            "bid_depth_usdt": bid_depth, "ask_depth_usdt": ask_depth,
            "imbalance": (bid_depth-ask_depth)/(bid_depth+ask_depth) if bid_depth+ask_depth else None,
            "oi_change": oi_change, "oi_status":raw.get('oi_status','UNKNOWN'), "book_timestamp": int(book["ts"]), "host_observed_at_ms": now_ms,
            "depth_band_bps":settings.book_depth_bps,"bid_levels":len(bids),"ask_levels":len(asks),
            "bid_band_complete":bool(bids[:,0].min()<=mid*(1-band) or len(bids)<raw.get('book_limit',len(bids)+1)),
            "ask_band_complete":bool(asks[:,0].max()>=mid*(1+band) or len(asks)<raw.get('book_limit',len(asks)+1)),
            "caveat": "Single orderbook snapshot; orders can be cancelled, OI is not directional."}


def technical_blockers(side, coin, btc_trend, settings=None):
    settings = settings or cfg
    reasons = []
    if coin.get("status") != "OK":
        return ["coin_data_unavailable"]
    if coin.get("regime") == "SHOCK":
        reasons.append("volatility_shock")
    corr = coin.get("benchmark", {}).get("correlation")
    if corr is not None and corr >= settings.benchmark_correlation_min:
        if (side == "LONG" and btc_trend == "DOWN") or (side == "SHORT" and btc_trend == "UP"):
            reasons.append("correlated_btc_opposition")
    distance = coin.get("distance_vwap_atr")
    if distance is not None and ((side == "LONG" and distance > 3) or (side == "SHORT" and distance < -3)):
        reasons.append("extended_from_vwap")
    return reasons


def live_blockers(side, coin, btc_trend, market, micro, settings=None):
    settings = settings or cfg
    reasons = technical_blockers(side, coin, btc_trend, settings)
    funding = market.get("funding_rate")
    if funding is None:
        reasons.append("funding_unavailable")
    elif (side == "LONG" and funding > settings.max_funding_rate) or (side == "SHORT" and funding < -settings.max_funding_rate):
        reasons.append("expensive_crowded_funding")
    if micro.get("status") != "OK":
        reasons.append("orderbook_unavailable")
    else:
        if micro["spread_bps"] > settings.max_spread_bps:
            reasons.append("wide_spread")
        depth = micro["ask_depth_usdt"] if side == "LONG" else micro["bid_depth_usdt"]
        if depth < settings.min_book_depth_usdt:
            complete=micro.get('ask_band_complete' if side=='LONG' else 'bid_band_complete',True)
            reasons.append("thin_orderbook" if complete else 'orderbook_depth_incomplete')
    return reasons
