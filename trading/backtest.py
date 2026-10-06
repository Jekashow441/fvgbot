"""Chronological single-symbol strategy evaluation, isolated from live DB/learning.

Uses next-bar open entries and fixed SL/TP; trailing and market-wide ticker filters
are intentionally not simulated without historical snapshots/tick data.
"""
import math
import numpy as np
from core.settings import cfg as default_cfg
from trading.intelligence import coin_context, technical_blockers
from trading.strategy import detect_fvgs, validate_signal, market_context, _calc_atr
from trading.context import closed_candles
from trading.profiles import strategy_settings
from trading.exit_rules import candle_exit
from trading.data_quality import validate_ohlcv


def metrics(trades):
    returns = [t["net_r"] for t in trades]
    n = len(returns)
    wins = sum(r > 0 for r in returns)
    p = wins/n if n else 0
    z = 1.96
    denom = 1 + z*z/n if n else 1
    center = (p+z*z/(2*n))/denom if n else 0
    margin = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denom if n else 0
    gains = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    equity = peak = drawdown = 0.0
    for r in returns:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    return {"trades": n, "win_rate_pct": 100*p if n else None,
            "win_rate_95pct_interval": [100*(center-margin), 100*(center+margin)] if n else None,
            "expectancy_r": equity/n if n else None, "net_r": equity,
            "profit_factor": gains/losses if losses else None, "max_drawdown_r": drawdown}


def run_backtest(df, htf_df=None, htf="60", settings=None, benchmark_df=None, enhanced=False, start_index=None, max_hold_bars=0):
    if not isinstance(max_hold_bars, int) or max_hold_bars < 0:
        raise ValueError("max_hold_bars must be a nonnegative integer")
    cfg = strategy_settings(settings or default_cfg)
    validate_ohlcv(df, cfg.timeframe)
    if htf_df is not None:
        validate_ohlcv(htf_df, htf)
    if benchmark_df is not None:
        validate_ohlcv(benchmark_df, cfg.timeframe)
    df = df.sort_values("timestamp").reset_index(drop=True)
    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    if (df.timestamp.duplicated().any() or not np.isfinite(df[columns].to_numpy(dtype=float)).all()
            or (df[["open", "high", "low", "close"]] <= 0).any().any()
            or (df.volume < 0).any()
            or (df.high < df[["open", "low", "close"]].max(axis=1)).any()
            or (df.low > df[["open", "high", "close"]].min(axis=1)).any()):
        raise ValueError("Invalid OHLCV or duplicate timestamps")
    df["vol_sma"] = df["volume"].shift(1).rolling(20).mean()
    df["atr_previous"] = _calc_atr(df).shift(1)
    trades, active, pending, used = [], None, None, set()
    htf_cache = {}
    htf_ends = htf_df.timestamp.to_numpy()+int(htf)*60000 if htf_df is not None and str(htf).isdigit() else None
    rejected = {}
    warmup = max(cfg.ema_period + 60, 100, start_index or 0)
    for i in range(warmup, len(df)):
        bar = df.iloc[i]
        if pending and active is None:
            side = 1 if pending["signal"] == "LONG" else -1
            entry = float(bar.open)
            risk = side*(entry-pending["sl"])
            reward = side*(pending["tp"]-entry)
            costs = entry*2*(cfg.fee_bps+cfg.slippage_bps)/10000
            if risk > 0 and reward/risk >= cfg.rr_min and (reward-costs)/(risk+costs) >= cfg.min_net_rr:
                active = dict(pending, entry=entry, risk=risk, direction=side, entry_timestamp=int(bar.timestamp), entry_index=i)
            else:
                rejected["entry_cost_or_gap"] = rejected.get("entry_cost_or_gap", 0)+1
            pending = None
        if active:
            a = active
            held_bars = i-a["entry_index"]+1
            exit_result = candle_exit(a["direction"], a["sl"], a["tp"], bar, held_bars, max_hold_bars)
            if exit_result:
                exit_price, exit_reason = exit_result
                costs = 2*(cfg.fee_bps+cfg.slippage_bps)/10000*a["entry"]
                trades.append({"entry_timestamp": a["entry_timestamp"], "side": a["signal"], "setup": a.get("setup", "fvg_retest"), "regime": a.get("regime", "UNKNOWN"), "timestamp": int(bar.timestamp), "held_bars": held_bars, "exit_reason": exit_reason, "net_r": (a["direction"]*(exit_price-a["entry"])-costs)/a["risk"]})
                active = None
            continue
        history = df.iloc[max(0, i-max(cfg.ema_period+60, 350)+1):i+1]
        zones = detect_fvgs(history, cfg, prepared=True)
        if not zones:
            continue
        context = None
        if htf_df is not None:
            close_time = int(bar.timestamp)+int(cfg.timeframe)*60000
            available = htf_df.iloc[:int(np.searchsorted(htf_ends, close_time, side="right"))] if htf_ends is not None else closed_candles(htf_df, htf, close_time)
            if len(available) < cfg.ema_period:
                continue
            if not 0 <= close_time-(int(available.iloc[-1].timestamp)+int(htf)*60000) < int(htf)*60000:
                rejected['stale_htf'] = rejected.get('stale_htf',0)+1
                continue
            last_htf = int(available.iloc[-1].timestamp)
            if last_htf not in htf_cache:
                htf_cache[last_htf] = market_context(available.tail(350), cfg)
            context = htf_cache[last_htf]
        candidates = [(zone, validate_signal(history, zone, context, settings=cfg, diagnostics=rejected))
                      for zone in zones if (zone['candle_time'],zone['type']) not in used]
        candidates = sorted(((z,s) for z,s in candidates if s),
                            key=lambda pair:(-pair[1].get('score',0),pair[0].get('touches',0),pair[0].get('age',0)))
        for zone, signal in candidates:
            key = (zone["candle_time"], zone["type"])
            if key in used:
                continue
            if signal and enhanced:
                btc = benchmark_df[benchmark_df.timestamp <= bar.timestamp].tail(350) if benchmark_df is not None else None
                coin = coin_context(history, btc, cfg)
                btc_trend = market_context(btc, cfg)["trend"] if btc is not None and len(btc) >= cfg.ema_period else "UNKNOWN"
                blockers = technical_blockers(signal["signal"], coin, btc_trend, cfg)
                for reason in blockers:
                    rejected[reason] = rejected.get(reason, 0)+1
                if blockers:
                    continue
            if signal:
                pending = signal
                used.add(key)
                break
    split = int(df.iloc[len(df)*2//3].timestamp) if len(df) else 0
    return {"model": "fixed SL/TP, next-open execution, stop-first ambiguity", "max_hold_bars": max_hold_bars,
            "limitations": ["No historical market-wide turnover/spread filter", "No live learning, cooldown, portfolio limits or trailing", "Single-symbol results are not live winrate"],
            "all": metrics(trades), "last_third": metrics([t for t in trades if t["entry_timestamp"] >= split]),
            "rejected": rejected, "trade_log": trades,
            "open_positions": int(active is not None), "pending_signals": int(pending is not None)}
