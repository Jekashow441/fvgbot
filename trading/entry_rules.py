"""Explain execution-price rejection without weakening structural risk limits."""
import math


def entry_rejection(signal, price, settings):
    values = [price, signal['entry'], signal['sl'], signal['tp']]
    if not all(math.isfinite(value) and value > 0 for value in values):
        return 'invalid_execution_price'
    direction = 1 if signal['signal'] == 'LONG' else -1
    risk = direction*(price-signal['sl'])
    reward = direction*(signal['tp']-price)
    if risk <= 0 or reward <= 0:
        return 'price_outside_trade_levels'
    if reward/risk < settings.rr_min:
        return 'insufficient_execution_rr'
    if abs(price-signal['entry']) > abs(signal['entry']-signal['sl'])*.25:
        return 'execution_price_moved'
    costs = price*2*(settings.fee_bps+settings.slippage_bps)/10000
    if (reward-costs)/(risk+costs) < settings.min_net_rr:
        return 'insufficient_rr_after_costs'
    return None
