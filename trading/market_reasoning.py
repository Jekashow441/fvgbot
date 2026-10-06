"""Observable market states and explanations; no claim to predict trader emotions."""


def market_state(df, adx, atr):
    last = df.iloc[-1]
    closes = df.close.tail(21)
    path = float(closes.diff().abs().sum())
    efficiency = abs(float(closes.iloc[-1]-closes.iloc[0]))/path if path > 0 else 0.0
    baseline = float(df.volume.iloc[-21:-1].mean())
    rvol = float(last.volume/baseline) if baseline > 0 else 0.0
    shock = atr > 0 and float(last.high-last.low)/atr >= 3
    regime = "SHOCK" if shock else "TREND" if (adx or 0) >= 25 and efficiency >= .25 else "RANGE" if (adx or 0) < 20 else "TRANSITION"
    prior = df.iloc[-21:-1]
    sweep = "SELL_SIDE" if last.low < prior.low.min() and last.close > prior.low.min() else "BUY_SIDE" if last.high > prior.high.max() and last.close < prior.high.max() else None
    return {"regime": regime, "efficiency": float(efficiency), "relative_volume": rvol,
            "liquidity_sweep": sweep, "version": "observable_rules_v1"}


def setup_reasoning(side, setup, state):
    explanations = {
        "fvg_retest": "Цена вернулась к дисбалансу и закрылась в сторону предполагаемого входа.",
        "impulse_continuation": "Свежий FVG, направленный тренд, повышенный объём и закрытие за предыдущими максимумами/минимумами.",
        "range_reclaim": "В боковом рынке цена сняла локальную ликвидность и вернулась за уровень; требуется подтверждённый FVG.",
    }
    return {**state, "setup": setup, "side": side, "explanation": explanations[setup],
            "invalidates": "Пробой границы зоны/стопа, потеря актуальности цены или новый риск-сигнал.",
            "win_probability": None}
