import json
import os
from typing import List
from pydantic import BaseModel, Field, ConfigDict

from trading.symbols import ALL_SYMBOLS

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "bot_config.json")


class BotSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tg_token: str = ""
    tg_chat_id: str = ""

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000

    symbols: List[str] = Field(default_factory=lambda: ALL_SYMBOLS[:20])
    timeframe: str = "15"
    ema_period: int = 200
    risk_reward: float = 3.0

    fvg_lookback: int = 20
    fvg_confirmations: int = 2
    fvg_timeframes: List[str] = Field(default_factory=lambda: ["5", "15", "60"])
    fvg_min_size_pct: float = 0.05
    fvg_max_size_atr: float = Field(default=3.0, gt=0)
    fvg_max_touch_bars: int = Field(default=2, ge=1)
    fvg_min_body_pct: float = 60.0
    fvg_require_volume: bool = True
    fvg_strict_mitigation: bool = True
    rsi_overbought: int = 70
    rsi_oversold: int = 30

    dynamic_rr: bool = True
    rr_min: float = 1.5
    rr_max: float = 2.6
    adx_strong: int = 30
    min_signal_score: int = 65
    enable_factor_learning: bool = True
    factor_learning_min_trades: int = 8
    factor_score_adjustment_cap: int = 15
    loss_cooldown_minutes: int = 45
    struct_sl: bool = True
    swing_lookback: int = 12
    sl_atr_buffer: float = 0.35
    sl_max_atr: float = 2.2

    paper_balance: float = 10000.0
    paper_trade_size_pct: float = 10.0
    paper_risk_per_trade_pct: float = Field(default=0.5, gt=0, le=5)
    max_active_positions: int = 5
    enable_breakeven: bool = True
    breakeven_at_r: float = 0.8
    enable_trail: bool = True
    trail_at_r: float = 1.2
    trail_gap_r: float = 0.7
    enable_risk_guard: bool = True
    max_consecutive_losses: int = 4
    max_daily_loss_pct: float = -4.0
    daily_profit_target: float = 6.0

    bybit_cache_ttl: float = 10.0
    bybit_request_timeout: float = 8.0
    # Keep the 5m universe scan inside a candle without changing strategy
    # gates; the public API budget supports this modest concurrency.
    bybit_max_concurrency: int = 10

    is_running: bool = False
    scan_all_symbols: bool = True
    min_turnover_24h: float = Field(default=5_000_000, ge=0)
    max_spread_bps: float = Field(default=15, gt=0)
    universe_refresh_seconds: int = Field(default=300, ge=30)
    structure_pivot: int = Field(default=3, ge=1, le=20)
    require_structure: bool = True
    min_relative_volume: float = Field(default=0.8, ge=0)
    displacement_atr: float = Field(default=0.8, ge=0)
    max_entry_distance_atr: float = Field(default=1.0, gt=0)
    fee_bps: float = Field(default=5.5, ge=0)
    slippage_bps: float = Field(default=2.0, ge=0)
    enable_coin_research: bool = True
    research_days: int = Field(default=90, ge=7, le=365)
    research_refresh_hours: int = Field(default=24, ge=1)
    research_min_trades: int = Field(default=30, ge=5)
    research_min_test_trades: int = Field(default=10, ge=3)
    research_min_profit_factor: float = Field(default=1.15, ge=1)
    research_max_drawdown_r: float = Field(default=12, gt=0)
    require_backtest_pass: bool = True
    enable_news: bool = True
    news_refresh_seconds: int = Field(default=300, ge=60)
    news_max_age_hours: int = Field(default=24, ge=1, le=168)
    news_blackout_minutes: int = Field(default=60, ge=5)
    news_require_coverage: bool = True
    enable_coin_news: bool = True
    coin_news_interval_seconds: float = Field(default=4, ge=1)
    coin_news_priority_ttl_minutes: int = Field(default=15, ge=5)
    coin_news_ttl_minutes: int = Field(default=120, ge=15)
    coin_news_max_age_hours: int = Field(default=48, ge=6, le=168)
    coin_news_score_weight: int = Field(default=6, ge=0, le=15)
    news_feeds: List[str] = Field(default_factory=lambda: [
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ])
    benchmark_correlation_min: float = Field(default=0.6, ge=0, le=1)
    max_funding_rate: float = Field(default=0.001, gt=0)
    min_book_depth_usdt: float = Field(default=25000, ge=0)
    book_depth_bps: float = Field(default=20, gt=0)
    # Required same-side depth scales with the planned position, capped by
    # min_book_depth_usdt and never below the floor.
    book_depth_position_multiple: float = Field(default=10, gt=0)
    min_book_depth_floor_usdt: float = Field(default=5000, ge=0)
    max_same_direction_positions: int = Field(default=3, ge=1)
    strategy_profile: str = Field(default="balanced", pattern="^(legacy|balanced|contextual)$")
    research_gate_mode: str = Field(default="paper", pattern="^(strict|paper)$")
    retest_window_bars: int = Field(default=3, ge=1, le=5)
    trendline_tolerance_atr: float = Field(default=0.25, gt=0, le=1)
    global_loss_pause_minutes: int = Field(default=120, ge=5)
    min_net_rr: float = Field(default=0.8, gt=0)
    context_timeframe: str = Field(default="60", pattern="^(15|30|60|120|240)$")
    signal_max_age_seconds: int = Field(default=300, ge=30, le=1800)
    continuation_min_rvol: float = Field(default=1.3, ge=1)
    target_winrate_pct: float = Field(default=70, ge=50, le=95)
    target_min_trades: int = Field(default=100, ge=30)
    enable_system_summary: bool = True
    system_summary_seconds: int = Field(default=14400,ge=300)
    # Diagnostic-only Telegram notices for high-quality setups one condition
    # short of a confirmed entry. These never enter the signal outbox.
    enable_early_setup_alerts: bool = True
    # Early notices may use the lower observation threshold while production
    # entries continue to require min_signal_score (currently 80).
    early_setup_min_score: int = Field(default=65, ge=50, le=100)


def _apply_env_overrides(settings: BotSettings) -> BotSettings:
    mapping = {
        "TG_TOKEN": ("tg_token", str),
        "TG_CHAT_ID": ("tg_chat_id", str),
        "DASHBOARD_HOST": ("dashboard_host", str),
        "DASHBOARD_PORT": ("dashboard_port", int),
        "BYBIT_CACHE_TTL": ("bybit_cache_ttl", float),
        "BYBIT_REQUEST_TIMEOUT": ("bybit_request_timeout", float),
        "BYBIT_MAX_CONCURRENCY": ("bybit_max_concurrency", int),
    }
    data = settings.model_dump()
    for env_name, (field_name, caster) in mapping.items():
        raw = os.getenv(env_name)
        if raw is None or raw == "":
            continue
        try:
            data[field_name] = caster(raw)
        except ValueError:
            print(f"[settings] invalid {env_name}={raw!r}, ignored")
    return BotSettings(**data)


def load_settings() -> BotSettings:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _apply_env_overrides(BotSettings(**data))
        except Exception as e:
            print(f"Error loading config: {e}")
    return _apply_env_overrides(BotSettings())


def _mutate_global(settings: BotSettings) -> BotSettings:
    global cfg
    try:
        target = cfg
    except NameError:
        cfg = settings
        return cfg
    for field_name in BotSettings.model_fields:
        setattr(target, field_name, getattr(settings, field_name))
    return target


def reload_settings() -> BotSettings:
    settings=load_settings()
    from trading.paper_account import balance
    stored=balance()
    if stored is not None:
        settings.paper_balance=stored
    return _mutate_global(settings)


def save_settings(settings: BotSettings) -> None:
    clean = BotSettings(**settings.model_dump())
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=DATA_DIR,suffix='.tmp',delete=False) as f:
        f.write(clean.model_dump_json(indent=4))
        temporary=f.name
    os.replace(temporary,CONFIG_FILE)
    _mutate_global(clean)


cfg = load_settings()
