import json
import os
from typing import List, Optional
from pydantic import BaseModel, Field

from trading.symbols import ALL_SYMBOLS

# Папка для всех данных
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "bot_config.json")

class BotSettings(BaseModel):
    # Telegram credentials
    tg_token: str = ""
    tg_chat_id: str = ""

    # Trading Configuration
    symbols: List[str] = ALL_SYMBOLS[:20]
    timeframe: str = "15"
    ema_period: int = 200
    risk_reward: float = 3.0
    
    # Strategy Parameters
    fvg_lookback: int = 20
    fvg_confirmations: int = 2
    fvg_timeframes: List[str] = ["5", "15", "60"]
    fvg_min_size_pct: float = 0.05
    fvg_min_body_pct: float = 60.0  # ICT Displacement: body must be >60% of candle
    fvg_require_volume: bool = True # ICT: FVG must form on higher than average volume
    fvg_strict_mitigation: bool = True # ICT: Invalidate if price closes beyond 50% (Consequent Encroachment)
    rsi_overbought: int = 70
    rsi_oversold: int = 30
    
    # Advanced Strategy (migrated from config.py)
    dynamic_rr: bool = True
    rr_min: float = 1.5
    rr_max: float = 2.6
    adx_strong: int = 30
    struct_sl: bool = True
    swing_lookback: int = 12
    sl_atr_buffer: float = 0.35
    sl_max_atr: float = 2.2
    
    # Risk Management
    paper_balance: float = 10000.0
    paper_trade_size_pct: float = 10.0
    enable_breakeven: bool = True
    breakeven_at_r: float = 0.8
    enable_trail: bool = True
    trail_at_r: float = 1.2
    trail_gap_r: float = 0.7
    enable_risk_guard: bool = True
    max_consecutive_losses: int = 4
    max_daily_loss_pct: float = -4.0
    daily_profit_target: float = 6.0
    
    # Bot Control
    is_running: bool = False

def load_settings() -> BotSettings:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return BotSettings(**data)
        except Exception as e:
            print(f"Error loading config: {e}")
            return BotSettings()
    return BotSettings()

def save_settings(settings: BotSettings):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(settings.model_dump_json(indent=4))

# Global instance
cfg = load_settings()
