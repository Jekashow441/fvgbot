import time
from aiogram import Router, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ErrorEvent
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from core.settings import load_settings, save_settings
import tg.keyboards as kb
from trading.paper_trading import get_paper_stats
from trading.logger import log_info

# --- Anti-Spam Middleware ---
class AntiSpamMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 1.0):
        self.limit = limit
        self.users = {}

    async def __call__(self, handler, event, data):
        user_id = None
        if isinstance(event, Message):
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id

        if user_id:
            now = time.time()
            last_time = self.users.get(user_id, 0)
            if now - last_time < self.limit:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⚠️ Too fast! Please wait a second.", show_alert=True)
                    except Exception:
                        pass
                return # Block the request
            self.users[user_id] = now

        return await handler(event, data)

router = Router()
router.message.middleware(AntiSpamMiddleware())
router.callback_query.middleware(AntiSpamMiddleware())

# --- Global Error Handler ---
@router.errors()
async def global_error_handler(event: ErrorEvent):
    log_info(f"Global Telegram Error: {event.exception}")
    
    # We can try to notify the user if possible
    if event.update.message:
        try:
            await event.update.message.answer("⚠️ An internal error occurred. Please try again later.")
        except Exception:
            pass
    elif event.update.callback_query:
        try:
            await event.update.callback_query.answer("⚠️ An error occurred.", show_alert=True)
        except Exception:
            pass
            
    return True # Handle the error and prevent crash

# --- Handlers ---
@router.message(Command("balance"))
async def cmd_balance(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Please provide the amount. Usage: /balance 1000")
        return
        
    try:
        new_balance = float(command.args)
        cfg = load_settings()
        cfg.paper_balance = new_balance
        save_settings(cfg)
        await message.answer(f"✅ Virtual Balance updated to <b>{new_balance:.2f} USDT</b>", parse_mode="HTML")
    except ValueError:
        await message.answer("Invalid amount. Usage: /balance 1000")

@router.message(Command("start"))
async def cmd_start(message: Message):
    cfg = load_settings()
    if cfg.tg_chat_id != str(message.chat.id):
        cfg.tg_chat_id = str(message.chat.id)
        save_settings(cfg)
        
    await message.answer(
        "👋 <b>Welcome to FVG Bot!</b>\n\n"
        "Configure your strategy and manage the bot using the menu below.",
        reply_markup=kb.get_main_menu_kb(),
        parse_mode="HTML"
    )

async def _safe_edit_text(message: Message, text: str, reply_markup=None):
    """Safely edits text to avoid 'Message is not modified' exception."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            log_info(f"TelegramBadRequest: {e}")
    except Exception as e:
        log_info(f"Edit text failed: {e}")

async def _safe_edit_markup(message: Message, reply_markup):
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            log_info(f"TelegramBadRequest: {e}")
    except Exception as e:
        log_info(f"Edit markup failed: {e}")

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    await _safe_edit_text(call.message, "<b>Main Menu</b>", reply_markup=kb.get_main_menu_kb())
    await call.answer()

@router.callback_query(F.data == "toggle_bot")
async def cb_toggle_bot(call: CallbackQuery):
    cfg = load_settings()
    cfg.is_running = not cfg.is_running
    save_settings(cfg)
    
    status = "running" if cfg.is_running else "stopped"
    await call.answer(f"Bot is now {status}!")
    await _safe_edit_markup(call.message, reply_markup=kb.get_main_menu_kb())

@router.callback_query(F.data == "settings_menu")
async def cb_settings_menu(call: CallbackQuery):
    await _safe_edit_text(call.message, "<b>Settings Menu</b>\nConfigure your FVG strategy parameters:", reply_markup=kb.get_settings_kb())
    await call.answer()

@router.callback_query(F.data.startswith("manage_pairs:"))
async def cb_manage_pairs(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    await _safe_edit_text(call.message, "<b>Manage Trading Pairs</b>\nSelect pairs to monitor:", reply_markup=kb.get_pairs_kb(page))
    await call.answer()

@router.callback_query(F.data.startswith("toggle_pair:"))
async def cb_toggle_pair(call: CallbackQuery):
    _, pair, page = call.data.split(":")
    page = int(page)
    cfg = load_settings()
    
    if pair in cfg.symbols:
        cfg.symbols.remove(pair)
    else:
        cfg.symbols.append(pair)
        
    save_settings(cfg)
    await call.answer(f"{pair} toggled")
    await _safe_edit_markup(call.message, reply_markup=kb.get_pairs_kb(page))

@router.callback_query(F.data == "change_timeframe")
async def cb_change_timeframe(call: CallbackQuery):
    cfg = load_settings()
    tfs = ["5", "15", "60", "240"]
    try:
        idx = tfs.index(cfg.timeframe)
        next_tf = tfs[(idx + 1) % len(tfs)]
    except ValueError:
        next_tf = "15"
        
    cfg.timeframe = next_tf
    save_settings(cfg)
    await call.answer("Timeframe updated!")
    await _safe_edit_markup(call.message, reply_markup=kb.get_settings_kb())

@router.callback_query(F.data == "change_rr")
async def cb_change_rr(call: CallbackQuery):
    cfg = load_settings()
    rrs = [1.0, 1.5, 2.0, 2.5, 3.0]
    try:
        idx = rrs.index(cfg.risk_reward)
        next_rr = rrs[(idx + 1) % len(rrs)]
    except ValueError:
        next_rr = 2.0
        
    cfg.risk_reward = next_rr
    save_settings(cfg)
    await call.answer("Risk:Reward updated!")
    await _safe_edit_markup(call.message, reply_markup=kb.get_settings_kb())

@router.callback_query(F.data == "toggle_dynamic_rr")
async def cb_toggle_dynamic_rr(call: CallbackQuery):
    cfg = load_settings()
    cfg.dynamic_rr = not cfg.dynamic_rr
    save_settings(cfg)
    await call.answer("Dynamic RR toggled!")
    await _safe_edit_markup(call.message, reply_markup=kb.get_settings_kb())

@router.callback_query(F.data == "toggle_breakeven")
async def cb_toggle_breakeven(call: CallbackQuery):
    cfg = load_settings()
    cfg.enable_breakeven = not cfg.enable_breakeven
    save_settings(cfg)
    await call.answer("Breakeven toggled!")
    await _safe_edit_markup(call.message, reply_markup=kb.get_settings_kb())

@router.callback_query(F.data == "change_displacement")
async def cb_change_displacement(call: CallbackQuery):
    cfg = load_settings()
    pcts = [50.0, 60.0, 70.0, 80.0]
    try:
        idx = pcts.index(cfg.fvg_min_body_pct)
        next_pct = pcts[(idx + 1) % len(pcts)]
    except ValueError:
        next_pct = 60.0
        
    cfg.fvg_min_body_pct = next_pct
    save_settings(cfg)
    await call.answer(f"Displacement required: {int(next_pct)}%")
    await _safe_edit_markup(call.message, reply_markup=kb.get_settings_kb())

@router.callback_query(F.data == "toggle_ce")
async def cb_toggle_ce(call: CallbackQuery):
    cfg = load_settings()
    cfg.fvg_strict_mitigation = not cfg.fvg_strict_mitigation
    save_settings(cfg)
    await call.answer(f"Strict CE {'Enabled' if cfg.fvg_strict_mitigation else 'Disabled'}!")
    await _safe_edit_markup(call.message, reply_markup=kb.get_settings_kb())

@router.callback_query(F.data == "show_balance")
async def cb_show_balance(call: CallbackQuery):
    cfg = load_settings()
    await call.answer()
    await _safe_edit_text(
        call.message,
        f"💰 <b>Virtual Balance:</b> {cfg.paper_balance:.2f} USDT\n<i>Use /balance &lt;amount&gt; to change it.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]])
    )

@router.callback_query(F.data == "show_stats")
async def cb_show_stats(call: CallbackQuery):
    stats = get_paper_stats()
    await call.answer()
    msg = (
        f"📊 <b>Trading Statistics</b>\n\n"
        f"<b>Balance:</b> {stats['balance']:.2f} USDT\n"
        f"<b>Total Trades:</b> {stats['total_trades']}\n"
        f"<b>Wins:</b> {stats['wins']} | <b>Losses:</b> {stats['losses']}\n"
        f"<b>Win Rate:</b> {stats['win_rate']}%\n"
        f"<b>Net PnL:</b> {stats['total_pnl']:.2f}%\n"
        f"<b>Active Trades:</b> {stats['active_count']}\n"
    )
    await _safe_edit_text(
        call.message,
        msg,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]])
    )

@router.callback_query(F.data == "test_signal")
async def cb_test_signal(call: CallbackQuery):
    cfg = load_settings()
    symbol = cfg.symbols[0] if cfg.symbols else "BTCUSDT"
    
    import time
    from datetime import datetime, timezone
    
    fvg_time = datetime.now(timezone.utc).strftime('%H:%M:%S (UTC)')
    
    msg = (
        f"🚨 <b>TEST FVG LONG SIGNAL</b> 🚨\n"
        f"<b>Pair:</b> {symbol}\n"
        f"<b>Timeframe:</b> {cfg.timeframe}m\n"
        f"<b>Time:</b> {fvg_time}\n\n"
        f"<b>Entry:</b> 65000.00\n"
        f"<b>Stop Loss:</b> 64500.00\n"
        f"<b>Take Profit:</b> 66000.00\n"
        f"<b>R:R:</b> 1:{cfg.risk_reward}\n\n"
        f"<i>(This is a generated test signal)</i>"
    )
    
    kb_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Preview Chart", callback_data=f"preview:{symbol}")]
    ])
    
    await call.message.answer(msg, parse_mode="HTML", reply_markup=kb_markup)
    
    from trading.engine import LATEST_DATA
    
    if symbol not in LATEST_DATA:
        LATEST_DATA[symbol] = {}
        
    LATEST_DATA[symbol] = {
        "fvg": {
            "type": "BULLISH",
            "top": 64800.00,
            "bottom": 64000.00,
            "gap_size": 1.25,
            "candle_time": int(time.time() * 1000)
        },
        "signal": {
            "signal": "LONG",
            "entry": 65000.00,
            "sl": 64500.00,
            "tp": 66000.00
        },
        "current_price": 64950.00
    }
    
    await call.answer("Test signal sent and dashboard updated!")

@router.callback_query(F.data.startswith("preview:"))
async def cb_preview_chart(call: CallbackQuery):
    await call.answer("Generating chart, please wait...")
    
    symbol = call.data.split(":")[1]
    cfg = load_settings()
    
    from trading.engine import LATEST_DATA
    if symbol not in LATEST_DATA or "fvg" not in LATEST_DATA[symbol]:
        await call.message.answer("No recent FVG data available for preview.")
        return
        
    fvg = LATEST_DATA[symbol]["fvg"]
    signal = LATEST_DATA[symbol]["signal"]
    
    from core.bybit import get_klines_async
    from trading.charting import generate_fvg_chart
    from aiogram.types import BufferedInputFile
    
    df = await get_klines_async(symbol, cfg.timeframe, limit=100)
    if df.empty:
        await call.message.answer("Failed to fetch recent chart data.")
        return
        
    buf = generate_fvg_chart(symbol, df, fvg, signal)
    
    photo = BufferedInputFile(buf.read(), filename=f"{symbol}_fvg.png")
    await call.message.answer_photo(photo, caption=f"📊 <b>{symbol}</b> {fvg['type']} FVG Setup", parse_mode="HTML")
