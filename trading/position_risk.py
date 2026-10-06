"""Unlevered paper allocation and realized account returns in consistent units."""
import json
import math


def stored_features(row):
    try:
        data = json.loads(row.get('ml_features') or '{}')
        return data if isinstance(data,dict) else {}
    except (TypeError,ValueError):
        return {}


def position_notional(entry, sl, balance, active, settings):
    if not all(math.isfinite(x) and x > 0 for x in (entry,sl,balance)) or entry == sl:
        return 0.0
    occupied = 0.0
    for row in active:
        size = stored_features(row).get('position_size_usdt')
        if size is None or not math.isfinite(float(size)) or float(size) < 0:
            return 0.0  # Unknown existing allocation must not create free capital.
        occupied += float(size)
    loss_fraction = abs(entry-sl)/entry+2*(settings.fee_bps+settings.slippage_bps)/10000
    risk_budget = balance*settings.paper_risk_per_trade_pct/100
    return max(0.0,min(risk_budget/loss_fraction,balance*settings.paper_trade_size_pct/100,balance-occupied))


def realized_day_return(rows, balance):
    pnl_usdt = 0.0
    for row in rows:
        size = stored_features(row).get('position_size_usdt')
        if size is None:
            return None
        try:
            pnl = float(size)*float(row['pnl_pct'])/100
        except (ValueError,TypeError,KeyError):
            return None
        if not math.isfinite(pnl) or float(size) < 0:
            return None
        pnl_usdt += pnl
    start_balance = balance-pnl_usdt
    return 100*pnl_usdt/start_balance if start_balance > 0 else None
