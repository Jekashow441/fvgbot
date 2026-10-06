"""Context-specific evidence from closed paper trades, never invented confidence."""
import json
import math
import statistics
import hashlib
from core.database import _conn


def settings_key(settings):
    from trading.research import fingerprint
    policy = {k:getattr(settings,k) for k in ('enable_breakeven','breakeven_at_r','enable_trail','trail_at_r','trail_gap_r')}
    policy['strategy'] = fingerprint(settings)
    return hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()[:20]


def context_key(signal):
    return '/'.join(str(signal.get(k) or 'unknown') for k in
                    ('strategy_version','strategy_profile','execution_version','settings_fingerprint','timeframe','setup','regime','signal'))


def assess_context(signal, rows=None):
    key = context_key(signal)
    if rows is None:
        with _conn() as con:
            rows = [dict(r) for r in con.execute("SELECT pnl_pct,closed_ts,ml_features FROM signals WHERE outcome IN ('WIN','LOSS') ORDER BY closed_ts DESC,id DESC")]
    returns, days = [], set()
    for row in rows:
        try:
            features = json.loads(row.get('ml_features') or '{}')
            if not isinstance(features,dict) or features.get('decision_context') != key:
                continue
            value = float(row['pnl_pct'])
            if not math.isfinite(value):
                continue
            returns.append(value)
            days.add(str(row.get('closed_ts') or '')[:10])
            if len(returns) == 100:
                break
        except (TypeError,ValueError,KeyError):
            continue
    n = len(returns)
    mean = statistics.mean(returns) if n else None
    margin = 1.96*statistics.stdev(returns)/math.sqrt(n) if n >= 2 else None
    upper = mean+margin if margin is not None else None
    enough = n >= 30 and len(days- {''}) >= 5
    status = 'NEGATIVE_EVIDENCE' if enough and upper < 0 else 'OBSERVING' if enough else 'INSUFFICIENT'
    return {'context':key,'status':status,'trades':n,'distinct_days':len(days-{''}),
            'avg_net_pct':mean,'mean_upper_95':upper,'score_bonus':0,
            'note':'Descriptive paper evidence; correlated trades and repeated checks limit confidence.'}
