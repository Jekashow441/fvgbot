"""Explain execution-price rejection without weakening structural risk limits."""
import math


# Targets are built from the expected fill (see validate_signal), so only residual noise
# beyond the spread estimate needs room here; drift stays capped at 0.25R and the
# after-cost check below still guards the economics.
EXECUTION_RR_TOLERANCE = 0.95


def execution_rr_floor(settings):
    return settings.rr_min * EXECUTION_RR_TOLERANCE


def entry_rejection(signal, price, settings):
    values = [price, signal['entry'], signal['sl'], signal['tp']]
    if not all(math.isfinite(value) and value > 0 for value in values):
        return 'invalid_execution_price'
    direction = 1 if signal['signal'] == 'LONG' else -1
    risk = direction*(price-signal['sl'])
    reward = direction*(signal['tp']-price)
    if risk <= 0 or reward <= 0:
        return 'price_outside_trade_levels'
    if reward/risk < execution_rr_floor(settings):
        return 'insufficient_execution_rr'
    if abs(price-signal['entry']) > abs(signal['entry']-signal['sl'])*.25:
        return 'execution_price_moved'
    costs = price*2*(settings.fee_bps+settings.slippage_bps)/10000
    if (reward-costs)/(risk+costs) < settings.min_net_rr:
        return 'insufficient_rr_after_costs'
    return None
