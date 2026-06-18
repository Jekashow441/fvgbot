from core.settings import cfg
from core.database import get_closed_today, current_loss_streak
from datetime import datetime
from trading.logger import log_info

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
        log_info(f"Risk Guard: Trading stopped. Hit max consecutive losses ({streak}).")
        return False
        
    # 2. Daily PnL check
    closed_today = get_closed_today(today_iso)
    daily_pnl = sum([t['pnl_pct'] for t in closed_today])
    
    if daily_pnl <= cfg.max_daily_loss_pct:
        log_info(f"Risk Guard: Trading stopped. Hit daily loss limit ({daily_pnl:.2f}% <= {cfg.max_daily_loss_pct}%).")
        return False
        
    if daily_pnl >= cfg.daily_profit_target:
        log_info(f"Risk Guard: Trading stopped. Hit daily profit target ({daily_pnl:.2f}% >= {cfg.daily_profit_target}%).")
        return False
        
    return True
