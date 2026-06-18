import time
from typing import Dict, Any
from core.settings import cfg
from core.database import init_db, log_signal, get_active_signals, close_signal, get_stats

# Ensure database is initialized
init_db()

def open_paper_trade(symbol: str, signal_data: Dict[str, Any]):
    """
    Opens a paper trade by logging a signal to SQLite database.
    """
    active = get_active_signals()
    
    # Check max positions limit
    if len(active) >= cfg.max_active_positions:
        return
        
    # Check if already active
    for t in active:
        if t["symbol"] == symbol:
            return
            
    # Prepare signal record
    sig_record = {
        "entry_ts": int(time.time() * 1000),
        "symbol": symbol,
        "side": signal_data["signal"],
        "score": 100.0, # Dummy score
        "entry": signal_data["entry"],
        "sl": signal_data["sl"],
        "tp": signal_data["tp"],
        "rsi": None,
        "trend": None,
        "htf": None,
        "adx": None,
        "factors": [],
        "signature": f"{signal_data['signal']}_{symbol}_{int(time.time())}",
        "ml_features": None,
        "ml_prob": None
    }
    
    log_signal(sig_record)

async def check_active_trades(bot, symbol: str, current_high: float, current_low: float):
    """
    Checks if active trades for a given symbol hit SL or TP.
    """
    active_trades = get_active_signals()
    
    for t in active_trades:
        if t["symbol"] != symbol:
            continue
            
        is_closed = False
        pnl_pct = 0.0
        result = ""
        exit_reason = ""
        
        # We need original Risk Amount to calculate USD pnl correctly
        # But we can do % pnl simpler
        entry_price = t["entry"]
        sl = t["sl"]
        tp = t["tp"]
        
        # Calculate Breakeven and Trailing stop logic here if needed
        # For simplicity, we just check original SL and TP
        
        if t["side"] == "LONG":
            if current_low <= sl:
                is_closed = True
                result = "LOSS 🔴"
                pnl_pct = ((sl - entry_price) / entry_price) * 100
                exit_reason = "SL"
            elif current_high >= tp:
                is_closed = True
                result = "WIN 🟢"
                pnl_pct = ((tp - entry_price) / entry_price) * 100
                exit_reason = "TP"
        else: # SHORT
            if current_high >= sl:
                is_closed = True
                result = "LOSS 🔴"
                pnl_pct = ((entry_price - sl) / entry_price) * 100
                exit_reason = "SL"
            elif current_low <= tp:
                is_closed = True
                result = "WIN 🟢"
                pnl_pct = ((entry_price - tp) / entry_price) * 100
                exit_reason = "TP"
                
        if is_closed:
            # Calculate USD PnL
            position_size_usdt = cfg.paper_balance * (cfg.paper_trade_size_pct / 100.0)
            pnl_usdt = position_size_usdt * (pnl_pct / 100.0)
            
            # Update balance
            cfg.paper_balance += pnl_usdt
            from core.settings import save_settings
            save_settings(cfg)
            
            # Close in DB
            close_price = sl if "LOSS" in result else tp
            close_signal(t["id"], "WIN" if pnl_pct > 0 else "LOSS", close_price, pnl_pct, exit_reason)
            
            # Notify User
            msg = (
                f"🧾 <b>PAPER TRADE CLOSED</b>\n"
                f"<b>Pair:</b> {t['symbol']} ({t['side']})\n"
                f"<b>Result:</b> {result} ({exit_reason})\n"
                f"<b>PnL:</b> {'+' if pnl_usdt >= 0 else ''}{pnl_usdt:.2f} USDT ({pnl_pct:.2f}%)\n"
                f"<b>New Balance:</b> {cfg.paper_balance:.2f} USDT"
            )
            await bot.send_message(cfg.tg_chat_id, msg, parse_mode="HTML")

def get_paper_stats() -> dict:
    stats = get_stats()
    
    return {
        "balance": cfg.paper_balance,
        "total_trades": stats.get("total", 0),
        "wins": stats.get("wins", 0),
        "losses": stats.get("losses", 0),
        "win_rate": stats.get("win_rate", 0.0),
        "total_pnl": stats.get("net_pnl", 0.0),
        "active_count": stats.get("open", 0)
    }
