import json
import math
import statistics
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple

from core.settings import cfg
from trading.version import STRATEGY_VERSION
from core.database import get_closed_signals, last_loss_time, set_setting_text


def _parse_factors(raw) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def factor_performance(min_trades: int | None = None) -> Dict[str, Dict[str, Any]]:
    """Compute historical performance per factor from closed real signals."""
    min_trades = cfg.factor_learning_min_trades if min_trades is None else min_trades
    rows = get_closed_signals()
    from trading.decision_memory import settings_key
    current_key = settings_key(cfg)
    stats: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        try:
            features = json.loads(row.get("ml_features") or "{}") or {}
        except (TypeError, ValueError):
            continue
        if not isinstance(features, dict) or features.get("strategy_version") != STRATEGY_VERSION or features.get("strategy_profile") != cfg.strategy_profile:
            continue
        if features.get("execution_version") != "cost_aware_v1" or features.get("settings_fingerprint") != current_key:
            continue
        outcome = row.get("outcome")
        pnl = float(row.get("pnl_pct") or 0.0)
        for factor in set(_parse_factors(row.get("factors"))):
            if factor.startswith("learn_"):
                continue
            item = stats.setdefault(factor, {"factor": factor, "trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "pnls": []})
            item["trades"] += 1
            item["wins"] += 1 if outcome == "WIN" else 0
            item["losses"] += 1 if outcome == "LOSS" else 0
            item["net_pnl"] += pnl
            item["pnls"].append(pnl)

    for item in stats.values():
        trades = item["trades"]
        item["win_rate"] = round(item["wins"] / trades * 100, 1) if trades else 0.0
        item["avg_pnl"] = round(item["net_pnl"] / trades, 4) if trades else 0.0
        item["net_pnl"] = round(item["net_pnl"], 4)
        item["sample_ok"] = trades >= max(30, min_trades)
        pnls = item.pop("pnls")
        margin = 1.96*statistics.stdev(pnls)/math.sqrt(trades) if trades > 1 else float("inf")
        mean = sum(pnls)/trades
        item["expectancy_lower"] = mean-margin if math.isfinite(margin) else None
        item["expectancy_upper"] = mean+margin if math.isfinite(margin) else None

    return dict(sorted(stats.items(), key=lambda kv: (kv[1]["sample_ok"], kv[1]["avg_pnl"], kv[1]["trades"]), reverse=True))


def learning_adjustment(factors: List[str]) -> Tuple[int, List[str]]:
    """Return score adjustment based on factor history."""
    if not cfg.enable_factor_learning or not factors:
        return 0, []

    perf = factor_performance()
    votes = []
    notes: List[str] = []
    for factor in set(factors):
        item = perf.get(factor)
        if not item or not item.get("sample_ok"):
            continue
        if item.get("expectancy_lower") is not None and item["expectancy_lower"] > 0:
            votes.append(4)
            notes.append(f"learn_bonus:{factor}")
        elif item.get("expectancy_upper") is not None and item["expectancy_upper"] < 0:
            votes.append(-5)
            notes.append(f"learn_penalty:{factor}")
    # Correlated factor labels do not multiply a single piece of evidence.
    cap = max(0, int(cfg.factor_score_adjustment_cap))
    adjustment = max(-cap, min(cap, int(statistics.median(votes)))) if votes else 0
    return adjustment, notes


def apply_learning_to_signal(signal: Dict[str, Any]) -> Dict[str, Any] | None:
    """Adjust signal score using historical factor performance.

    Returns None if adjusted score falls below min_signal_score.
    """
    if not signal:
        return None

    factors = list(signal.get("factors") or [])
    adjustment, notes = learning_adjustment(factors)
    if adjustment == 0 and not notes:
        signal["learning_adjustment"] = 0
        return signal

    new_signal = dict(signal)
    new_factors = factors + notes
    new_score = int(new_signal.get("score", 0)) + adjustment
    new_score = max(0, min(100, new_score))
    new_signal["score"] = new_score
    new_signal["learning_adjustment"] = adjustment
    new_signal["factors"] = new_factors

    if new_score < cfg.min_signal_score:
        return None
    return new_signal


def is_symbol_in_cooldown(symbol: str) -> Tuple[bool, str | None]:
    """Cooldown a symbol after a recent loss to avoid revenge entries in chop."""
    minutes = int(cfg.loss_cooldown_minutes or 0)
    if minutes <= 0:
        return False, None

    ts = last_loss_time(symbol)
    if not ts:
        return False, None

    try:
        loss_dt = datetime.fromisoformat(ts)
    except Exception:
        return False, None

    until = loss_dt + timedelta(minutes=minutes)
    if datetime.now() < until:
        return True, until.isoformat(timespec="seconds")
    return False, None


def learning_summary() -> Dict[str, Any]:
    perf = factor_performance(min_trades=1)
    factors = list(perf.values())
    strong = [x for x in factors if x["sample_ok"] and x["expectancy_lower"] is not None and x["expectancy_lower"] > 0]
    weak = [x for x in factors if x["sample_ok"] and x["expectancy_upper"] is not None and x["expectancy_upper"] < 0]
    summary = {
        "enabled": cfg.enable_factor_learning,
        "min_trades": max(30, cfg.factor_learning_min_trades),
        "score_cap": cfg.factor_score_adjustment_cap,
        "loss_cooldown_minutes": cfg.loss_cooldown_minutes,
        "factors": factors,
        "strong_factors": strong[:10],
        "weak_factors": weak[:10],
    }
    try:
        set_setting_text("learning_summary", json.dumps(summary, ensure_ascii=False))
    except Exception:
        pass
    return summary
