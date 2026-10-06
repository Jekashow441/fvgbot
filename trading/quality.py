"""Version-separated realized paper performance. Not a signal probability model."""
import json
import math
from collections import defaultdict
from core.database import _conn
from core.settings import cfg


def summarize_outcomes(pnls, target=70, minimum=100):
    n = len(pnls)
    wins = sum(p > 0 for p in pnls)
    if not n:
        return {"trades": 0, "win_rate": None, "interval_95": None, "target_status": "INSUFFICIENT", "avg_pnl_pct": None}
    p = wins/n
    z = 1.96
    denominator = 1+z*z/n
    center = (p+z*z/(2*n))/denominator
    margin = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denominator
    low, high = max(0, 100*(center-margin)), min(100, 100*(center+margin))
    avg = sum(pnls)/n
    status = "INSUFFICIENT" if n < minimum else "OBSERVED_TARGET" if low >= target and avg > 0 else "NOT_REACHED"
    return {"trades": n, "wins": wins, "win_rate": 100*p, "interval_95": [low, high],
            "avg_pnl_pct": avg, "target_status": status}


def quality_report():
    groups = defaultdict(list)
    with _conn() as con:
        rows = con.execute("SELECT pnl_pct, ml_features FROM signals WHERE outcome IN ('WIN','LOSS') AND pnl_pct IS NOT NULL").fetchall()
    for row in rows:
        try:
            features = json.loads(row["ml_features"] or "{}") or {}
        except (TypeError, ValueError):
            features = {}
        if not isinstance(features, dict):
            features = {}
        key = "/".join(str(features.get(k) or "unknown") for k in ("strategy_version", "strategy_profile", "execution_version", "settings_fingerprint", "timeframe", "setup", "regime"))
        groups[key].append(float(row["pnl_pct"]))
    return {"target_pct": cfg.target_winrate_pct, "minimum_sample": cfg.target_min_trades,
            "cohorts": {k: summarize_outcomes(v, cfg.target_winrate_pct, cfg.target_min_trades) for k,v in groups.items()},
            "scope": "Closed paper positions only; not every delivered notification. Intervals assume independent trades; correlated samples reduce reliability."}
