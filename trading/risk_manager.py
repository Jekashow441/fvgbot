from core.settings import cfg
from core.database import get_closed_today, current_loss_streak, last_any_loss_time
from datetime import datetime, timedelta
from trading.logger import log_info
from trading.position_risk import realized_day_return

def is_trading_allowed() -> bool:
    """
    Checks circuit breakers: daily loss limit and consecutive losses.
    """
    if not cfg.enable_risk_guard:
        return True
        
    today_iso = datetime.now().isoformat()[:10]
    
    # 1. Consecutive losses check
    streak = current_loss_streak()
    if streak >= cfg.max_consecutive_losses:
        timestamp = last_any_loss_time()
        try:
            last_loss = datetime.fromisoformat(timestamp) if timestamp else datetime.now()
            now = datetime.now(last_loss.tzinfo)
            if now < last_loss + timedelta(minutes=cfg.global_loss_pause_minutes):
                log_info(f"Risk Guard: temporary pause after {streak} losses.")
                return False
        except (TypeError, ValueError):
            return False
        
    # 2. Daily PnL check
    closed_today = get_closed_today(today_iso)
    daily_pnl = realized_day_return(closed_today, cfg.paper_balance)
    if daily_pnl is None:
        log_info("Risk Guard: cannot reconstruct daily account return from stored position sizes.")
        return False
    
    if daily_pnl <= cfg.max_daily_loss_pct:
        log_info(f"Risk Guard: Trading stopped. Hit daily loss limit ({daily_pnl:.2f}% <= {cfg.max_daily_loss_pct}%).")
        return False
        
    if daily_pnl >= cfg.daily_profit_target:
        log_info(f"Risk Guard: Trading stopped. Hit daily profit target ({daily_pnl:.2f}% >= {cfg.daily_profit_target}%).")
        return False
        
    return True
