"""Self-learning from shadow outcomes of every valid setup, in R units.

Every technically valid setup is followed virtually (trading/shadow.py), so the sample grows
far faster than real paper trades. Small samples are shrunk toward the overall average, so a
few lucky outcomes cannot swing the score. Filters are reported, never silently disabled.
"""
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple

from core.settings import cfg
from core.database import last_loss_time, set_setting_text

# Shrinkage strength: a factor needs about this many outcomes before its own average
# counts as much as the overall average.
PRIOR_WEIGHT = 20
# Learned R effect -> score points (0.2R better than average ~ +5 points).
POINTS_PER_R = 25
_CACHE = {"at": 0.0, "rows": None}


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


def evidence(max_age_seconds=60):
    """Closed shadow outcomes for the current strategy version (cached briefly)."""
    if _CACHE["rows"] is None or time.time()-_CACHE["at"] > max_age_seconds:
        from trading.shadow import outcomes
        _CACHE.update(rows=outcomes(), at=time.time())
    return _CACHE["rows"]


def _stats(rs, baseline_r=0.0, baseline_wr=0.5):
    n = len(rs)
    wins = sum(r > 0 for r in rs)
    mean = sum(rs)/n if n else 0.0
    return {"trades": n, "wins": wins, "win_rate": round(100*wins/n, 1) if n else None,
            "avg_r": round(mean, 3) if n else None,
            # Shrunk toward the overall baseline: honest on small samples.
            "effect_r": round(n*(mean-baseline_r)/(n+PRIOR_WEIGHT), 3) if n else 0.0,
            "win_rate_est": round(100*(wins+PRIOR_WEIGHT*baseline_wr)/(n+PRIOR_WEIGHT), 1)}


def baseline(rows=None):
    rows = evidence() if rows is None else rows
    rs = [r["r"] for r in rows]
    n = len(rs)
    return {"trades": n, "avg_r": sum(rs)/n if n else 0.0, "win_rate": sum(r > 0 for r in rs)/n if n else 0.5}


def factor_performance(min_trades: int | None = None, rows=None) -> Dict[str, Dict[str, Any]]:
    """Per-factor outcome statistics with shrunk effect versus the overall average."""
    min_trades = max(10, cfg.factor_learning_min_trades) if min_trades is None else min_trades
    rows = evidence() if rows is None else rows
    base = baseline(rows)
    groups: Dict[str, List[float]] = {}
    for row in rows:
        for factor in set(row["factors"]):
            if not factor.startswith(("learn_", "news_")):
                groups.setdefault(factor, []).append(row["r"])
    total = max(1, len(rows))
    result = {}
    for factor, rs in groups.items():
        item = {"factor": factor, **_stats(rs, base["avg_r"], base["win_rate"])}
        # A factor present on nearly every setup carries no information.
        item["coverage"] = round(len(rs)/total, 3)
        item["sample_ok"] = len(rs) >= min_trades and item["coverage"] <= 0.9
        result[factor] = item
    return dict(sorted(result.items(), key=lambda kv: (kv[1]["sample_ok"], kv[1]["effect_r"]), reverse=True))


def learning_adjustment(factors: List[str], perf=None) -> Tuple[int, List[str]]:
    """Average (not sum) of learned factor effects, so correlated labels cannot stack."""
    if not cfg.enable_factor_learning or not factors:
        return 0, []
    perf = factor_performance() if perf is None else perf
    effects, notes = [], []
    for factor in set(factors):
        item = perf.get(factor)
        if not item or not item.get("sample_ok"):
            continue
        effects.append(item["effect_r"])
        if item["effect_r"] >= 0.1:
            notes.append(f"learn_bonus:{factor}")
        elif item["effect_r"] <= -0.1:
            notes.append(f"learn_penalty:{factor}")
    if not effects:
        return 0, []
    cap = max(0, int(cfg.factor_score_adjustment_cap))
    adjustment = int(round(sum(effects)/len(effects)*POINTS_PER_R))
    return max(-cap, min(cap, adjustment)), notes


def context_estimate(signal: Dict[str, Any], rows=None) -> Dict[str, Any]:
    """Historical hit rate of similar setups, from the most specific bucket with enough data."""
    rows = evidence() if rows is None else rows
    base = baseline(rows)
    side, setup, regime = signal.get("signal") or signal.get("side"), signal.get("setup"), signal.get("regime")
    levels = [
        ("context", lambda r: r["side"] == side and r["setup"] == setup and r["regime"] == regime),
        ("setup", lambda r: r["side"] == side and r["setup"] == setup),
        ("side", lambda r: r["side"] == side),
    ]
    for name, match in levels:
        rs = [r["r"] for r in rows if match(r)]
        if len(rs) >= 10:
            stats = _stats(rs, base["avg_r"], base["win_rate"])
            return {"level": name, "trades": stats["trades"], "win_rate_est": stats["win_rate_est"],
                    "avg_r": stats["avg_r"], "note": "Shadow outcomes of similar setups; fixed SL/TP, not a guarantee."}
    return {"level": "insufficient", "trades": len(rows), "win_rate_est": None, "avg_r": None,
            "note": "Too few closed shadow outcomes yet."}


def blocker_report(rows=None, min_trades=10) -> List[Dict[str, Any]]:
    """What each filter did: did the setups it blocked lose (useful) or win (costly)?"""
    rows = evidence() if rows is None else rows
    groups: Dict[str, List[float]] = {}
    for row in rows:
        for blocker in set(row["blockers"]) or {"__passed__"}:
            groups.setdefault(blocker, []).append(row["r"])
    report = []
    for blocker, rs in groups.items():
        stats = _stats(rs)
        if blocker == "__passed__":
            verdict = "прошли все фильтры"
        elif len(rs) < min_trades:
            verdict = "мало данных"
        elif stats["avg_r"] <= -0.05:
            verdict = "полезен: отсекает убыточные сетапы"
        elif stats["avg_r"] >= 0.1:
            verdict = "режет прибыльные сетапы — стоит пересмотреть"
        else:
            verdict = "нейтрален"
        report.append({"blocker": blocker, **stats, "verdict": verdict})
    return sorted(report, key=lambda x: -x["trades"])


def apply_learning_to_signal(signal: Dict[str, Any]) -> Dict[str, Any] | None:
    """Adjust the score from learned factor effects and attach the historical estimate.

    Returns None if the adjusted score falls below min_signal_score.
    """
    if not signal:
        return None
    factors = list(signal.get("factors") or [])
    adjustment, notes = learning_adjustment(factors)
    new_signal = dict(signal)
    new_signal["learning_adjustment"] = adjustment
    new_signal["factors"] = factors + notes
    new_signal["score"] = max(0, min(100, int(new_signal.get("score", 0)) + adjustment))
    new_signal["history_estimate"] = context_estimate(new_signal)
    if new_signal["score"] < cfg.min_signal_score:
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


BLOCKER_RU = {
    "thin_orderbook": "тонкий стакан", "extended_from_vwap": "далеко от VWAP", "higher_4h_opposition": "4h против",
    "score_below_threshold": "score ниже порога", "htf_opposition": "старший ТФ против", "correlated_btc_opposition": "BTC против",
    "expensive_crowded_funding": "дорогой funding", "wide_spread": "широкий спред", "critical_negative_news": "критичная новость",
    "same_direction_exposure_limit": "лимит позиций в сторону", "risk_guard": "risk guard", "lower_ranked_zone": "выбрана зона получше",
    "insufficient_rr_after_costs": "мало R после комиссий", "execution_price_moved": "цена ушла", "volatility_shock": "шок волатильности",
}


def insights(perf, blockers, base) -> List[str]:
    """Plain-language takeaways, the way a trader would summarise their journal."""
    lines = []
    if base["trades"] < 20:
        return [f"Пока собрано {base['trades']} теневых исходов — выводы появятся после ~20."]
    lines.append(f"По {base['trades']} теневым сетапам: win rate {base['win_rate']*100:.0f}%, в среднем {base['avg_r']:+.2f}R на сетап.")
    good = [p for p in perf.values() if p["sample_ok"] and p["effect_r"] >= 0.1][:3]
    bad = [p for p in perf.values() if p["sample_ok"] and p["effect_r"] <= -0.1][-3:]
    if good:
        lines.append("Лучше среднего работают: " + ", ".join(f"{p['factor']} ({p['effect_r']:+.2f}R)" for p in good) + ".")
    if bad:
        lines.append("Хуже среднего: " + ", ".join(f"{p['factor']} ({p['effect_r']:+.2f}R)" for p in bad) + ".")
    for b in blockers:
        if b["verdict"].startswith("режет"):
            lines.append(f"Фильтр «{BLOCKER_RU.get(b['blocker'], b['blocker'])}» отсёк {b['trades']} сетапов со средним {b['avg_r']:+.2f}R — возможно, он лишний.")
        elif b["verdict"].startswith("полезен") and b["trades"] >= 20:
            lines.append(f"Фильтр «{BLOCKER_RU.get(b['blocker'], b['blocker'])}» сэкономил: отсечённые сетапы в среднем {b['avg_r']:+.2f}R.")
    return lines


def learning_summary() -> Dict[str, Any]:
    from trading.shadow import counts
    rows = evidence(0)
    base = baseline(rows)
    perf = factor_performance(rows=rows)
    blockers = blocker_report(rows)
    summary = {
        "enabled": cfg.enable_factor_learning,
        "method": "shadow outcomes of every valid setup, R units, shrinkage toward baseline",
        "min_trades": max(10, cfg.factor_learning_min_trades),
        "score_cap": cfg.factor_score_adjustment_cap,
        "loss_cooldown_minutes": cfg.loss_cooldown_minutes,
        "shadow": counts(),
        "baseline": {"trades": base["trades"], "avg_r": round(base["avg_r"], 3), "win_rate": round(base["win_rate"]*100, 1)},
        "factors": list(perf.values()),
        "strong_factors": [p for p in perf.values() if p["sample_ok"] and p["effect_r"] >= 0.1][:10],
        "weak_factors": [p for p in perf.values() if p["sample_ok"] and p["effect_r"] <= -0.1][:10],
        "blockers": blockers,
        "insights": insights(perf, blockers, base),
    }
    try:
        set_setting_text("learning_summary", json.dumps(summary, ensure_ascii=False))
    except Exception:
        pass
    return summary
