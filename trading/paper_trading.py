import time
import json
from typing import Dict, Any, Optional, Tuple

from core.settings import cfg
from core.database import log_signal, get_active_signals, get_stats, update_signal_levels
from trading.logger import log_info
from trading.position_risk import position_notional
from trading.paper_account import settle

def prepare_paper_entry(signal,price,microstructure,settings):
    from trading.entry_rules import entry_rejection
    reason=entry_rejection(signal,price,settings)
    if reason:
        return None,reason
    direction=1 if signal['signal']=='LONG' else -1
    return dict(signal,signal_entry=signal['entry'],entry=price,
                rr=direction*(signal['tp']-price)/(direction*(price-signal['sl'])),microstructure=microstructure),None


def open_paper_trade(symbol: str, signal_data: Dict[str, Any]) -> bool:
    active = get_active_signals()
    if len(active) >= cfg.max_active_positions:
        log_info(f"Paper trade skipped for {symbol}: max active positions reached ({cfg.max_active_positions}).")
        return False
    if any(t["symbol"] == symbol for t in active):
        log_info(f"Paper trade skipped for {symbol}: position already active.")
        return False

    entry = float(signal_data["entry"])
    sl = float(signal_data["sl"])
    tp = float(signal_data["tp"])
    if not (sl < entry < tp if signal_data["signal"] == "LONG" else tp < entry < sl):
        return False
    notional = position_notional(entry, sl, cfg.paper_balance, active, cfg)
    if notional <= 0:
        return False
    factors = signal_data.get("factors", []) or []
    sig_record = {
        "entry_ts": int(time.time() * 1000),
        "signal_id": signal_data.get('signal_id'),
        "max_active_limit": cfg.max_active_positions,
        "symbol": symbol,
        "side": signal_data["signal"],
        "score": float(signal_data.get("score", 50.0)),
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rsi": signal_data.get("rsi"),
        "trend": signal_data.get("trend"),
        "htf": signal_data.get("htf"),
        "adx": signal_data.get("adx"),
        "factors": factors,
        "signature": f"{signal_data['signal']}_{symbol}_{'-'.join(factors) if factors else 'basic'}",
        "ml_features": {
            "strategy_version": signal_data.get("strategy_version"),
            "journal_id": signal_data.get("journal_id"),
            "signal_id": signal_data.get("signal_id"),
            "signal_entry": signal_data.get('signal_entry',entry),
            "entry_quote_timestamp": signal_data.get('microstructure',{}).get('book_timestamp'),
            "entry_quote_host_observed_ms": signal_data.get('microstructure',{}).get('host_observed_at_ms'),
            "initial_entry": entry, "initial_sl": sl, "initial_tp": tp,
            "generated_at": signal_data.get('generated_at'),
            "evidence": signal_data.get("evidence"),
            "strategy_profile": signal_data.get("strategy_profile"),
            "execution_version": "cost_aware_v1",
            "settings_fingerprint": signal_data.get("settings_fingerprint"),
            "timeframe": signal_data.get("timeframe", cfg.timeframe),
            "decision_context": signal_data.get("memory", {}).get("context"),
            "memory_at_entry": signal_data.get("memory"),
            "exit_policy": {k:getattr(cfg,k) for k in ("enable_breakeven","breakeven_at_r","enable_trail","trail_at_r","trail_gap_r")},
            "setup": signal_data.get("setup"),
            "regime": signal_data.get("regime"),
            "reasoning": signal_data.get("reasoning"),
            "trendlines": signal_data.get("trendlines"),
            "intelligence": signal_data.get("intelligence"),
            "news": signal_data.get("news"),
            "qualification": signal_data.get("qualification"),
            "microstructure": signal_data.get("microstructure"),
            "position_size_usdt": notional,
            "risk_budget_pct": cfg.paper_risk_per_trade_pct,
            "fee_bps": cfg.fee_bps,
            "slippage_bps": cfg.slippage_bps,
            "structure": signal_data.get("structure"),
            "relative_volume": signal_data.get("relative_volume"),
            "market": signal_data.get("market"),
            "score": signal_data.get("score"),
            "rr": signal_data.get("rr"),
            "rsi": signal_data.get("rsi"),
            "adx": signal_data.get("adx"),
            "trend": signal_data.get("trend"),
            "htf": signal_data.get("htf"),
            "factors": factors,
        },
        "ml_prob": None,
        "initial_risk": abs(entry - sl),
        "rr": float(signal_data.get("rr", cfg.risk_reward)),
    }
    trade_id=log_signal(sig_record)
    signal_data['paper_trade_id']=trade_id
    return trade_id is not None


def _estimated_initial_risk(entry: float, sl: float, tp: float, side: str, stored_risk=None, stored_rr=None) -> float:
    """Estimate initial R for legacy rows that do not store original risk."""
    if stored_risk is not None:
        try:
            stored_risk = float(stored_risk)
            if stored_risk > 0:
                return stored_risk
        except (TypeError, ValueError):
            pass

    rr = stored_rr if stored_rr is not None else cfg.risk_reward
    try:
        rr = max(float(rr or 1.0), 0.1)
    except (TypeError, ValueError):
        rr = max(float(cfg.risk_reward or 1.0), 0.1)

    tp_distance = abs(tp - entry)
    if tp_distance > 0:
        return tp_distance / rr

    return abs(entry - sl)


def _manage_exit_levels(t: Dict[str, Any], current_high: float, current_low: float) -> Tuple[float, str]:
    entry = float(t["entry"])
    sl = float(t["sl"])
    tp = float(t["tp"])
    side = t["side"]
    risk = _estimated_initial_risk(entry, sl, tp, side, t.get("initial_risk"), t.get("rr"))
    if risk <= 0:
        return sl, ""

    new_sl = sl
    reason = ""
    try:
        features = json.loads(t.get("ml_features") or "{}")
        features = features if isinstance(features, dict) else {}
    except (TypeError, ValueError):
        features = {}
    policy = features.get("exit_policy") or {}
    enabled_be = policy.get("enable_breakeven", cfg.enable_breakeven)
    be_at = policy.get("breakeven_at_r", cfg.breakeven_at_r)
    enabled_trail = policy.get("enable_trail", cfg.enable_trail)
    trail_at = policy.get("trail_at_r", cfg.trail_at_r)
    trail_gap = policy.get("trail_gap_r", cfg.trail_gap_r)
    # Matches the paper ledger's round-trip cost approximation.
    cost_distance = entry*2*(features.get("fee_bps",cfg.fee_bps)+features.get("slippage_bps",cfg.slippage_bps))/10000
    # Existing trades retain their original exit convention.
    if features.get("execution_version") != "cost_aware_v1":
        cost_distance = 0.0

    if side == "LONG":
        if enabled_be and current_high >= entry + risk * be_at and current_high > entry+cost_distance:
            if new_sl < entry+cost_distance:
                new_sl = entry+cost_distance
                reason = "BE"
        if enabled_trail and current_high >= entry + risk * trail_at:
            candidate = current_high - risk * trail_gap
            if candidate > new_sl:
                new_sl = candidate
                reason = "TRAIL"
    else:
        if enabled_be and current_low <= entry - risk * be_at and current_low < entry-cost_distance:
            if new_sl > entry-cost_distance:
                new_sl = entry-cost_distance
                reason = "BE"
        if enabled_trail and current_low <= entry - risk * trail_at:
            candidate = current_low + risk * trail_gap
            if candidate < new_sl:
                new_sl = candidate
                reason = "TRAIL"

    if new_sl != sl:
        update_signal_levels(t["id"], sl=float(new_sl), reason=reason)
        log_info(f"Paper {reason}: {t['symbol']} {side} SL moved {sl:.6f} -> {new_sl:.6f}")
    return float(new_sl), reason


async def check_active_trades(bot: Optional[Any], symbol: str, current_high: float, current_low: float) -> None:
    for t in get_active_signals():
        if t["symbol"] != symbol:
            continue

        entry_price = float(t["entry"])
        tp = float(t["tp"])
        side = t["side"]
        sl = float(t["sl"])
        features = json.loads(t.get("ml_features") or "{}")
        fee_bps = features.get("fee_bps", cfg.fee_bps)
        slippage_bps = features.get("slippage_bps", cfg.slippage_bps)
        # A stop at or beyond entry was set by breakeven/trailing management.
        be_offset = entry_price*2*(fee_bps+slippage_bps)/10000 if features.get("execution_version") == "cost_aware_v1" else 0.0
        locked_offset = (sl - entry_price) if side == "LONG" else (entry_price - sl)
        managed_reason = "" if locked_offset < 0 else "BE" if locked_offset <= be_offset*(1+1e-9) else "TRAIL"

        is_closed = False
        pnl_pct = 0.0
        exit_reason = ""

        if side == "LONG":
            if current_low <= sl:
                if current_high == current_low:
                    sl = min(sl, current_low)
                exit_reason = managed_reason or "SL"
                is_closed, pnl_pct = True, ((sl - entry_price) / entry_price) * 100
            elif current_high >= tp:
                is_closed, pnl_pct, exit_reason = True, ((tp - entry_price) / entry_price) * 100, "TP"
        else:
            if current_high >= sl:
                if current_high == current_low:
                    sl = max(sl, current_high)
                exit_reason = managed_reason or "SL"
                is_closed, pnl_pct = True, ((entry_price - sl) / entry_price) * 100
            elif current_low <= tp:
                is_closed, pnl_pct, exit_reason = True, ((entry_price - tp) / entry_price) * 100, "TP"

        if not is_closed:
            _manage_exit_levels(t, current_high, current_low)
            continue

        costs = 2 * (fee_bps + slippage_bps) / 100
        pnl_pct -= costs
        if abs(pnl_pct) < 1e-10:
            pnl_pct = 0.0  # Floating-point dust is not a winning trade.
        outcome = "WIN" if pnl_pct > 0 else "LOSS"
        close_price = tp if exit_reason == "TP" else sl
        settled = settle(t['id'],outcome,close_price,pnl_pct,exit_reason,cfg.paper_balance)
        if settled is None:
            continue
        # The ledger is authoritative and reload_settings() reads it; rewriting the whole
        # config file here could revert a setting changed from Telegram meanwhile.
        pnl_usdt,cfg.paper_balance=settled
        from trading.setup_journal import record_trade_result
        record_trade_result(features.get('journal_id'),exit_reason,pnl_pct)

        msg = (
            f"🧾 <b>PAPER TRADE CLOSED</b>\n"
            f"<b>Pair:</b> {symbol} ({side})\n"
            f"<b>Result:</b> {'WIN 🟢' if outcome == 'WIN' else 'LOSS 🔴'} ({exit_reason})\n"
            f"<b>PnL:</b> {'+' if pnl_usdt >= 0 else ''}{pnl_usdt:.2f} USDT ({pnl_pct:.2f}%)\n"
            f"<b>New Balance:</b> {cfg.paper_balance:.2f} USDT"
        )
        if bot and cfg.tg_chat_id:
            try:
                await bot.send_message(cfg.tg_chat_id, msg, parse_mode="HTML")
            except Exception as e:
                log_info(f"Failed to send paper close notification: {e}")


def get_paper_stats() -> dict:
    stats = get_stats()
    return {
        "balance": cfg.paper_balance,
        "total_trades": stats.get("total", 0),
        "wins": stats.get("wins", 0),
        "losses": stats.get("losses", 0),
        "win_rate": stats.get("win_rate", 0.0),
        "total_pnl": stats.get("net_pnl", 0.0),
        "active_count": stats.get("open", 0),
        "profit_factor": stats.get("profit_factor", 0.0),
        "scope": "Mixed historical ledger; legacy outcome labels may disagree with PnL. Use /quality for version-separated results.",
    }
