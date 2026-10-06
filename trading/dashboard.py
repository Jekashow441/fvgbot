"""Compact, JSON-safe dashboard view over the live engine state."""
import json
import math
import time
from collections import Counter

from core.database import _conn, get_active_signals, get_history_signals, get_stats
from core.settings import cfg
from trading.version import STRATEGY_VERSION

STAGE_RANK = {"ENTRY APPROACHING": 3, "POTENTIAL": 2, "DEVELOPING": 1, "WATCHLIST": 0}
SYSTEM_BLOCKERS = {"benchmark_context_unavailable", "risk_guard", "htf_unavailable_or_stale"}


def _num(value, digits=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits) if digits is not None else value


def _features(row):
    try:
        data = json.loads(row.get("ml_features") or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def active_positions(latest):
    rows = []
    for t in get_active_signals():
        entry, sl, tp = float(t["entry"]), float(t["sl"]), float(t["tp"])
        direction = 1 if t["side"] == "LONG" else -1
        features = _features(t)
        initial_sl = _num(features.get("initial_sl")) or sl
        risk = abs(entry - initial_sl) or None
        price = _num((latest.get(t["symbol"]) or {}).get("current_price"))
        size = _num(features.get("position_size_usdt"))
        move = direction * (price - entry) if price else None
        span = abs(tp - sl)
        rows.append({
            "id": t["id"], "symbol": t["symbol"], "side": t["side"], "entry": entry, "sl": sl, "tp": tp,
            "initial_sl": initial_sl, "price": price, "score": _num(t.get("score")), "opened": t.get("ts"),
            "size_usdt": size,
            "pnl_pct": _num(100 * move / entry, 3) if move is not None else None,
            "pnl_usdt": _num(size * move / entry, 2) if move is not None and size else None,
            "r": _num(move / risk, 2) if move is not None and risk else None,
            # 0 = at stop, 1 = at target.
            "progress": _num(min(1, max(0, direction * (price - sl) / span)), 3) if price and span else None,
            "stop_locked": direction * (sl - entry) >= 0,
            "setup": features.get("setup"),
        })
    return rows


def live_signals(latest, limit=20):
    signals = []
    for symbol, info in latest.items():
        sig = (info or {}).get("signal")
        if not sig or info.get("blockers"):
            continue
        signals.append({
            "symbol": symbol, "side": sig.get("signal"), "score": sig.get("score"),
            "entry": _num(sig.get("entry")), "sl": _num(sig.get("sl")), "tp": _num(sig.get("tp")), "rr": _num(sig.get("rr"), 2),
            "setup": sig.get("setup"), "regime": sig.get("regime"),
            "factors": [f for f in sig.get("factors") or [] if isinstance(f, str)],
            "generated_at": sig.get("generated_at"), "paper_opened": sig.get("paper_opened"),
            "paper_skip_reason": sig.get("paper_skip_reason"),
        })
    signals.sort(key=lambda s: -(s.get("generated_at") or 0))
    return signals[:limit]


def watchlist(latest, limit=60):
    rows = []
    for symbol, info in latest.items():
        info = info or {}
        zones = {str(z.get("candle_time")) + z.get("type", ""): z for z in info.get("zones") or []}
        price = _num(info.get("current_price"))
        atr_pct = _num((info.get("intelligence") or {}).get("atr_pct"))
        for key, a in (info.get("assessments") or {}).items():
            zone = zones.get(key)
            if not zone:
                continue
            checks = a.get("checks") or {}
            if checks.get("Zone eligibility") is False:
                continue
            ref = price or _num(a.get("current_price"))
            distance = max(zone["bottom"] - ref, ref - zone["top"], 0) if ref else None
            reasons = list(dict.fromkeys((a.get("rejections") or []) + (a.get("execution_rejections") or [])))
            rows.append({
                "symbol": symbol, "side": "LONG" if zone["type"] == "BULLISH" else "SHORT",
                "stage": "CONFIRMED" if checks.get("Entry") and not reasons else a.get("status"),
                "score": a.get("score"), "top": zone["top"], "bottom": zone["bottom"], "ce": zone.get("ce"),
                "price": ref, "age": zone.get("age"), "touches": zone.get("touches"),
                "distance_pct": _num(100 * distance / ref, 3) if distance is not None and ref else None,
                "distance_atr": _num(100 * distance / ref / atr_pct, 2) if distance is not None and ref and atr_pct else None,
                "checks": {k: bool(v) for k, v in checks.items()}, "missing": reasons,
                "may_form": bool(a.get("signal_may_form")),
            })
    rows.sort(key=lambda r: (r["stage"] == "CONFIRMED", STAGE_RANK.get(r["stage"], -1), r["score"] or 0), reverse=True)
    return rows[:limit]


def blocker_summary(latest, limit=12):
    """Count symbols per reason, so one symbol cannot dominate the chart."""
    counts = Counter()
    for info in latest.values():
        info = info or {}
        reasons = set(info.get("blockers") or [])
        for a in (info.get("assessments") or {}).values():
            reasons.update(a.get("rejections") or [])
            reasons.update(a.get("execution_rejections") or [])
        counts.update(reasons)
    return [{"reason": k, "symbols": v, "system": k in SYSTEM_BLOCKERS} for k, v in counts.most_common(limit)]


def equity_history(limit=500):
    with _conn() as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "paper_cashflows" not in tables:
            return []
        rows = con.execute("SELECT observed_at, amount, balance_after, kind, trade_id FROM paper_cashflows "
                           "ORDER BY observed_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [{"t": int(r["observed_at"] * 1000), "balance": _num(r["balance_after"], 2), "amount": _num(r["amount"], 2),
             "kind": r["kind"], "trade_id": r["trade_id"]} for r in reversed(rows)]


def trade_history(limit=100):
    keys = ("id", "ts", "closed_ts", "symbol", "side", "score", "entry", "sl", "tp", "exit_price", "pnl_pct", "outcome", "exit_reason")
    return [{k: t.get(k) for k in keys} for t in get_history_signals(limit=limit)]


def stats_summary():
    stats = get_stats()
    with _conn() as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        net = con.execute("SELECT COALESCE(SUM(amount),0) FROM paper_cashflows WHERE kind='SETTLEMENT'").fetchone()[0] \
            if "paper_cashflows" in tables else None
        scratch = con.execute("SELECT COUNT(*) FROM signals WHERE outcome IN ('WIN','LOSS') AND pnl_pct=0").fetchone()[0]
    decided = stats.get("wins", 0) + stats.get("losses", 0) - scratch
    from trading.paper_account import balance as account_balance
    stored = account_balance()
    return {"balance": _num(cfg.paper_balance if stored is None else stored, 2), "trades": stats.get("wins", 0) + stats.get("losses", 0),
            "wins": stats.get("wins", 0), "losses": stats.get("losses", 0) - scratch, "breakeven": scratch,
            "win_rate": _num(100 * stats.get("wins", 0) / decided, 1) if decided > 0 else None,
            "profit_factor": stats.get("profit_factor"), "net_pnl_usdt": _num(net, 2), "net_pnl_pct_sum": stats.get("net_pnl"),
            "active": stats.get("open", 0)}


def clean(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    if hasattr(value, "item"):
        return clean(value.item())
    return value


def overview(latest, scan_status, market, benchmarks, health=None):
    return clean(_overview(latest, scan_status, market, benchmarks, health))


def _overview(latest, scan_status, market, benchmarks, health):
    eligible = sum(1 for row in market.values() if row.get("eligible"))
    breadth = next((row.get("breadth") for row in market.values() if row.get("breadth") is not None), None)
    return {
        "generated_at": time.time(),
        "is_running": cfg.is_running,
        "health": {k: v for k, v in (health or {}).items() if k in ("status", "reasons", "reason", "last_completed_at", "pending_deliveries")},
        "scan": {k: scan_status.get(k) for k in ("selected", "processed", "cycle_seconds", "last_completed_at", "coverage", "htf")},
        "policy": {"timeframe": cfg.timeframe, "context_timeframe": cfg.context_timeframe, "min_score": cfg.min_signal_score,
                   "profile": cfg.strategy_profile, "version": STRATEGY_VERSION, "scan_all": cfg.scan_all_symbols,
                   "max_active": cfg.max_active_positions},
        "market": {"contracts": len(market), "eligible": eligible, "breadth": _num(breadth, 3),
                   "btc": (benchmarks.get("BTCUSDT") or {}).get("trend"), "eth": (benchmarks.get("ETHUSDT") or {}).get("trend")},
        "stats": stats_summary(),
        "active": active_positions(latest),
        "signals": live_signals(latest),
        "watchlist": watchlist(latest),
        "blockers": blocker_summary(latest),
    }
