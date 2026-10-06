from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from core.settings import load_settings
from trading.symbols import ALL_SYMBOLS

def get_main_menu_kb() -> InlineKeyboardMarkup:
    cfg = load_settings()
    toggle_text = "🔴 Stop Bot" if cfg.is_running else "🟢 Start Bot"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="toggle_bot")],
        [InlineKeyboardButton(text="⚡ Быстрый сигнал", callback_data="quick_signal")],
        [InlineKeyboardButton(text="🌍 Обзор рынка", callback_data="market_brief")],
        [InlineKeyboardButton(text="🎯 Качество стратегии", callback_data="strategy_quality")],
        [InlineKeyboardButton(text="📋 Сетапы и журнал", callback_data="setup_journal")],
        [InlineKeyboardButton(text="⚙️ Strategy Settings", callback_data="settings_menu")],
        [InlineKeyboardButton(text="📊 Trading Pairs", callback_data="manage_pairs:0")],
        [
            InlineKeyboardButton(text="💰 Balance", callback_data="show_balance"),
            InlineKeyboardButton(text="📈 Stats", callback_data="show_stats")
        ],
        [InlineKeyboardButton(text="🚨 Send Test Signal", callback_data="test_signal")]
    ])

def get_settings_kb() -> InlineKeyboardMarkup:
    cfg = load_settings()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏱ Timeframe: {cfg.timeframe}m", callback_data="change_timeframe")],
        [InlineKeyboardButton(text=f"⚖️ Risk:Reward: 1:{cfg.risk_reward}", callback_data="change_rr")],
        [
            InlineKeyboardButton(text=f"🛡️ Dyn RR: {'ON' if cfg.dynamic_rr else 'OFF'}", callback_data="toggle_dynamic_rr"),
            InlineKeyboardButton(text=f"💼 BreakEv: {'ON' if cfg.enable_breakeven else 'OFF'}", callback_data="toggle_breakeven")
        ],
        [
            InlineKeyboardButton(text=f"⚡ Displacement: {int(cfg.fvg_min_body_pct)}%", callback_data="change_displacement"),
            InlineKeyboardButton(text=f"🎯 Strict CE (50%): {'ON' if cfg.fvg_strict_mitigation else 'OFF'}", callback_data="toggle_ce")
        ],
        [InlineKeyboardButton(text=f"🧠 Min Score: {cfg.min_signal_score}/100", callback_data="change_min_score")],
        [
            InlineKeyboardButton(text=f"📚 Learning: {'ON' if cfg.enable_factor_learning else 'OFF'}", callback_data="toggle_factor_learning"),
            InlineKeyboardButton(text=f"🧊 Cooldown: {cfg.loss_cooldown_minutes}m", callback_data="change_loss_cooldown")
        ],
        [InlineKeyboardButton(text="🔙 Back to Main", callback_data="main_menu")]
    ])

def get_pairs_kb(page: int = 0) -> InlineKeyboardMarkup:
    cfg = load_settings()
    kb = []
    row = []
    
    items_per_page = 10
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    page_symbols = ALL_SYMBOLS[start_idx:end_idx]
    
    for symbol in page_symbols:
        status = "✅" if symbol in cfg.symbols else "❌"
        row.append(InlineKeyboardButton(text=f"{status} {symbol}", callback_data=f"toggle_pair:{symbol}:{page}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
        
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Prev", callback_data=f"manage_pairs:{page-1}"))
    if end_idx < len(ALL_SYMBOLS):
        nav_row.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"manage_pairs:{page+1}"))
        
    if nav_row:
        kb.append(nav_row)
        
    kb.append([InlineKeyboardButton(text="🔙 Back to Main", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)
