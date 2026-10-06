"""Virtual follow-through of every technically valid setup, traded or not.

Outcomes are measured in R with fixed SL/TP from point-price observations, so they are
a conservative learning sample, not exchange fills. Nothing here places a trade.
"""
import json
import math
import time

from core.database import _conn
from core.settings import cfg
from trading.version import STRATEGY_VERSION


_READY = set()
TIMEFRAME_MINUTES = {"D": 1440, "W": 10080, "M": 43200}


def timeframe_minutes(timeframe):
    tf = str(timeframe)
    return int(tf) if tf.isdigit() else TIMEFRAME_MINUTES.get(tf, 60)


def _schema(con):
    from core import database
    if database.DB_PATH in _READY:
        return
    _READY.add(database.DB_PATH)
    # Same table the legacy ML path created; columns are reused, R lives in ml_features.
    con.execute("""CREATE TABLE IF NOT EXISTS shadow_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, entry_ts INTEGER, symbol TEXT NOT NULL,
        side TEXT NOT NULL, score REAL, entry REAL NOT NULL, sl REAL NOT NULL, tp REAL NOT NULL, rsi REAL,
        trend TEXT, htf TEXT, adx REAL, factors TEXT, signature TEXT, ml_features TEXT, ml_prob REAL,
        outcome TEXT DEFAULT 'OPEN', exit_price REAL, pnl_pct REAL, closed_ts TEXT, exit_reason TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS shadow_open ON shadow_signals(symbol, outcome)")
    con.execute("CREATE INDEX IF NOT EXISTS shadow_signature ON shadow_signals(signature)")


def cost_r(entry, sl, settings=None):
    settings = settings or cfg
    risk = abs(entry-sl)
    return entry*2*(settings.fee_bps+settings.slippage_bps)/10000/risk if risk > 0 else 0.0


def track(symbol, key, candidate, blockers, context=None, now=None):
    """Start following a setup once; later scans of the same zone are ignored."""
    now = time.time() if now is None else now
    entry, sl, tp = float(candidate["entry"]), float(candidate["sl"]), float(candidate["tp"])
    side = candidate["signal"]
    direction = 1 if side == "LONG" else -1
    if not all(math.isfinite(x) and x > 0 for x in (entry, sl, tp)) or direction*(entry-sl) <= 0 or direction*(tp-entry) <= 0:
        return None
    features = {
        "strategy_version": STRATEGY_VERSION, "timeframe": cfg.timeframe, "strategy_profile": cfg.strategy_profile, "setup": candidate.get("setup"),
        "regime": candidate.get("regime"), "htf": candidate.get("htf"), "side": side,
        "score": candidate.get("score"), "min_score": cfg.min_signal_score,
        "blockers": sorted(set(blockers or [])), "traded": not blockers and candidate.get("score", 0) >= cfg.min_signal_score,
        "rr": abs(tp-entry)/abs(entry-sl), "cost_r": cost_r(entry, sl), "opened_at": now,
        "max_hold_seconds": cfg.shadow_max_hold_bars*timeframe_minutes(cfg.timeframe)*60, **(context or {}),
    }
    with _conn() as con:
        _schema(con)
        if con.execute("SELECT 1 FROM shadow_signals WHERE signature=? LIMIT 1", (key,)).fetchone():
            return None
        return con.execute(
            """INSERT INTO shadow_signals (ts, entry_ts, symbol, side, score, entry, sl, tp, rsi, trend, htf, adx, factors, signature, ml_features)
               VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(now*1000), symbol, side, candidate.get("score"), entry, sl, tp, candidate.get("rsi"), candidate.get("trend"),
             candidate.get("htf"), candidate.get("adx"), json.dumps(candidate.get("factors") or []), key,
             json.dumps(features, allow_nan=False))).lastrowid


def update(symbol, price, now=None):
    """Resolve open shadows with an observed price: stop first, then target, then timeout."""
    now = time.time() if now is None else now
    if price is None or not math.isfinite(price) or price <= 0:
        return []
    closed = []
    with _conn() as con:
        _schema(con)
        rows = con.execute("SELECT id, side, entry, sl, tp, ml_features FROM shadow_signals WHERE symbol=? AND outcome='OPEN'", (symbol,)).fetchall()
        for r in rows:
            f = json.loads(r["ml_features"] or "{}")
            direction = 1 if r["side"] == "LONG" else -1
            risk = abs(r["entry"]-r["sl"])
            if direction*(price-r["sl"]) <= 0:
                # A gap through the stop fills at the worse observed price.
                exit_price, reason = (min(price, r["sl"]) if direction == 1 else max(price, r["sl"])), "SL"
            elif direction*(price-r["tp"]) >= 0:
                exit_price, reason = r["tp"], "TP"
            elif now-f.get("opened_at", now) >= f.get("max_hold_seconds", float("inf")):
                exit_price, reason = price, "TIMEOUT"
            else:
                continue
            r_multiple = direction*(exit_price-r["entry"])/risk - f.get("cost_r", 0)
            f.update(r=r_multiple, closed_at=now)
            outcome = "WIN" if r_multiple > 0 else "LOSS"
            con.execute("UPDATE shadow_signals SET outcome=?, exit_price=?, pnl_pct=?, closed_ts=datetime('now'), exit_reason=?, ml_features=? WHERE id=? AND outcome='OPEN'",
                        (outcome, exit_price, 100*direction*(exit_price-r["entry"])/r["entry"], reason, json.dumps(f, allow_nan=False), r["id"]))
            closed.append({"id": r["id"], "outcome": outcome, "r": r_multiple, "reason": reason})
    return closed


def abandon_unpriced(now=None):
    """Shadows that could not be priced well past their hold limit are voided, not scored."""
    now = time.time() if now is None else now
    with _conn() as con:
        _schema(con)
        rows = con.execute("SELECT id, ml_features FROM shadow_signals WHERE outcome='OPEN'").fetchall()
        for r in rows:
            f = json.loads(r["ml_features"] or "{}")
            if now-f.get("opened_at", now) > 3*f.get("max_hold_seconds", float("inf")):
                con.execute("UPDATE shadow_signals SET outcome='VOID', exit_reason='unpriced', closed_ts=datetime('now') WHERE id=? AND outcome='OPEN'", (r["id"],))


def open_symbols():
    with _conn() as con:
        _schema(con)
        return [r[0] for r in con.execute("SELECT DISTINCT symbol FROM shadow_signals WHERE outcome='OPEN'")]


def outcomes(version=None, limit=5000):
    """Closed shadow outcomes for the current strategy version, newest first."""
    version = version or STRATEGY_VERSION
    with _conn() as con:
        _schema(con)
        rows = con.execute("SELECT symbol, side, factors, ml_features, exit_reason FROM shadow_signals "
                           "WHERE outcome IN ('WIN','LOSS') ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for r in rows:
        try:
            f = json.loads(r["ml_features"] or "{}")
            factors = json.loads(r["factors"] or "[]")
        except (TypeError, ValueError):
            continue
        # Outcomes only transfer between identical entry timeframe and profile.
        if (f.get("strategy_version") != version or f.get("r") is None or str(f.get("timeframe")) != str(cfg.timeframe)
                or f.get("strategy_profile", cfg.strategy_profile) != cfg.strategy_profile):
            continue
        result.append({"symbol": r["symbol"], "side": r["side"], "factors": factors, "r": float(f["r"]),
                       "setup": f.get("setup"), "regime": f.get("regime"), "htf": f.get("htf"),
                       "blockers": f.get("blockers") or [], "traded": f.get("traded"), "score": f.get("score"),
                       "exit_reason": r["exit_reason"], "source": "shadow"})
    return result


def counts():
    with _conn() as con:
        _schema(con)
        rows = dict(con.execute("SELECT outcome, COUNT(*) FROM shadow_signals GROUP BY outcome").fetchall())
    return {"open": rows.get("OPEN", 0), "closed": rows.get("WIN", 0)+rows.get("LOSS", 0)}
