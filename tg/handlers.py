import re
import time
from html import escape
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

def _chat_id(event):
    if isinstance(event, Message):
        return event.chat.id
    if isinstance(event, CallbackQuery) and event.message:
        return event.message.chat.id
    return None


def is_owner_event(event, owner: str) -> bool:
    """Only the configured chat may control the bot. With no owner yet, /start claims it."""
    chat = _chat_id(event)
    if chat is None:
        return False
    if owner:
        return str(chat) == str(owner)
    return isinstance(event, Message) and bool(re.match(r"^/start(@\w+)?(\s|$)", event.text or ""))


class OwnerOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not is_owner_event(event, load_settings().tg_chat_id):
            log_info(f"Telegram: ignored update from unauthorized chat {_chat_id(event)}")
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("Нет доступа", show_alert=True)
                except Exception:
                    pass
            return None
        return await handler(event, data)


router = Router()
router.message.middleware(OwnerOnlyMiddleware())
router.callback_query.middleware(OwnerOnlyMiddleware())
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
@router.message(Command("why_not_signal"))
async def cmd_why_not_signal(message: Message, command: CommandObject):
    settings=load_settings()
    if str(message.chat.id)!=str(settings.tg_chat_id):
        return
    from trading.setup_visibility import diagnostic,render
    from trading.setup_journal import history
    if not command.args:
        await message.answer('Использование: /why_not_signal <Setup ID>')
        return
    key=command.args.strip()
    if len(key)>180:
        await message.answer('Некорректный Setup ID.')
        return
    await message.answer(render(diagnostic(history(key))))

@router.message(Command("balance"))
async def cmd_balance(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Please provide the amount. Usage: /balance 1000")
        return
        
    try:
        new_balance = float(command.args)
        cfg = load_settings()
        from trading.paper_account import reset_balance
        reset_balance(new_balance)
        cfg.paper_balance = new_balance
        save_settings(cfg)
        await message.answer(f"✅ Virtual Balance updated to <b>{new_balance:.2f} USDT</b>", parse_mode="HTML")
    except ValueError:
        await message.answer("Invalid amount. Usage: /balance 1000")

@router.message(Command("start"))
async def cmd_start(message: Message):
    cfg = load_settings()
    if not cfg.tg_chat_id:
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

@router.callback_query(F.data == "change_min_score")
async def cb_change_min_score(call: CallbackQuery):
    cfg = load_settings()
    scores = [50, 60, 65, 70, 75, 80]
    try:
        idx = scores.index(cfg.min_signal_score)
        next_score = scores[(idx + 1) % len(scores)]
    except ValueError:
        next_score = 65
    cfg.min_signal_score = next_score
    save_settings(cfg)
    await call.answer(f"Min signal score: {next_score}/100")
    await _safe_edit_markup(call.message, reply_markup=kb.get_settings_kb())

@router.callback_query(F.data == "toggle_factor_learning")
async def cb_toggle_factor_learning(call: CallbackQuery):
    cfg = load_settings()
    cfg.enable_factor_learning = not cfg.enable_factor_learning
    save_settings(cfg)
    await call.answer(f"Factor learning {'enabled' if cfg.enable_factor_learning else 'disabled'}")
    await _safe_edit_markup(call.message, reply_markup=kb.get_settings_kb())

@router.callback_query(F.data == "change_loss_cooldown")
async def cb_change_loss_cooldown(call: CallbackQuery):
    cfg = load_settings()
    values = [0, 15, 30, 45, 60, 120]
    try:
        idx = values.index(cfg.loss_cooldown_minutes)
        next_value = values[(idx + 1) % len(values)]
    except ValueError:
        next_value = 45
    cfg.loss_cooldown_minutes = next_value
    save_settings(cfg)
    await call.answer(f"Loss cooldown: {next_value} minutes")
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
        f"📊 <b>Историческая статистика всех версий</b>\nСтарые WIN/LOSS содержат расхождения с PnL. Качество текущей версии: /quality.\n\n"
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
            "tp": 66000.00,
            "rr": cfg.risk_reward,
            "score": 85,
            "rsi": 52.0,
            "adx": 31.0,
            "trend": "UP",
            "htf": "UP",
            "factors": ["test_signal", "htf_uptrend", "healthy_rsi"]
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


async def _answer_quick_signal(message):
    import asyncio
    from core.settings import reload_settings
    from trading.engine import LATEST_DATA
    from trading.signal_delivery import quick_signal
    settings = reload_settings()
    if str(message.chat.id) != settings.tg_chat_id:
        await message.answer("Запрос доступен в настроенном чате бота.")
        return
    try:
        # A quick request now performs a bounded fresh scan when the cache has
        # no valid entry; allow enough time for exchange data and HTF checks.
        text = await asyncio.wait_for(quick_signal(LATEST_DATA), timeout=45)
    except asyncio.TimeoutError:
        text = "Свежий ручной анализ занял слишком долго. Попробуй ещё раз через несколько секунд."
    await message.answer(text, parse_mode="HTML", reply_markup=kb.get_main_menu_kb())


@router.callback_query(F.data == "quick_signal")
async def cb_quick_signal(call: CallbackQuery):
    await call.answer("Проверяю свежие результаты анализа…")
    if call.message:
        await _answer_quick_signal(call.message)


@router.message(Command("signal"))
async def cmd_quick_signal(message: Message):
    await _answer_quick_signal(message)


async def _answer_quality(message):
    from html import escape
    from trading.quality import quality_report
    settings = load_settings()
    if str(message.chat.id) != settings.tg_chat_id:
        return
    report = quality_report()
    lines = [f"🎯 <b>Цель winrate: {report['target_pct']:.0f}%</b>",
             f"Минимум {report['minimum_sample']} закрытых paper-сделок на сценарий; также проверяется результат после расходов."]
    from trading.version import STRATEGY_VERSION
    current = [(key, value) for key, value in report['cohorts'].items() if key.startswith(STRATEGY_VERSION+'/')]
    if not current:
        lines.append("У текущей версии пока нет закрытых сделок. Цель не подтверждена.")
    for key, item in sorted(current, key=lambda pair: -pair[1]['trades'])[:8]:
        interval = item['interval_95']
        lines.append(f"\n{escape(key)}\nСделок: {item['trades']} | Winrate: {item['win_rate']:.1f}%\nИнтервал 95%: {interval[0]:.1f}–{interval[1]:.1f}% | {item['target_status']}")
    lines.append("\nЭто статистика закрытых paper-позиций, не вероятность следующего сигнала. Коррелированные сделки снижают надёжность оценки.")
    await message.answer("\n".join(lines), parse_mode='HTML', reply_markup=kb.get_main_menu_kb())


@router.callback_query(F.data == 'strategy_quality')
async def cb_strategy_quality(call: CallbackQuery):
    await call.answer()
    if call.message:
        await _answer_quality(call.message)


@router.message(Command('quality'))
async def cmd_strategy_quality(message: Message):
    await _answer_quality(message)


async def _answer_setup_journal(message):
    from html import escape
    from datetime import datetime, timezone
    from trading.setup_journal import report
    settings=load_settings()
    if str(message.chat.id)!=settings.tg_chat_id:
        return
    rows=report()
    if not rows:
        await message.answer('Нет актуальных данных для проверки сигнала. Журнал пока пуст. INSUFFICIENT DATA',reply_markup=kb.get_main_menu_kb())
        return
    texts=['📋 Журнал наблюдений. Это история анализа, а не команда входить сейчас.']
    rows.sort(key=lambda r: (r['signal_may_form'],r['display_status'] in ('ENTRY APPROACHING','POTENTIAL','DEVELOPING'),r['payload'].get('fvg_quality') or 0),reverse=True)
    for row in rows[:3]:
        p=row['payload']
        def value(name):
            raw=p.get(name)
            return str(raw)[:200] if raw is not None else 'INSUFFICIENT DATA'
        date=datetime.fromtimestamp(row['observed_at'],timezone.utc).isoformat(timespec='seconds')
        texts.append(f"\n{row['category']} | {escape(row['display_status'])}\nID: {escape(row['setup_id'])}\nDate/Time: {date}\nAsset: {value('asset')} | TF: {value('timeframe')} | {value('direction')}\nHTF Bias: {value('htf_bias')}\nMarket Structure: {value('market_structure')}\nLiquidity: {value('liquidity')}\nFVG / Entry Zone: {value('entry_zone')}\nFVG Quality (setup score): {value('fvg_quality')}\nEntry: {value('entry')} | SL: {value('sl')}\nTP1: {value('tp1')} | TP2: {value('tp2')} | R:R: {value('rr')}\nConfirmation Required: {value('confirmation_required')}\nInvalidation: {value('invalidation')}\nReason: {escape(row['reason'])}\nConfidence: INSUFFICIENT DATA — probability not calibrated\nResult: {value('result')}")
        assessment=p.get('assessment',{})
        texts[-1]+='\nCurrent price: '+value('current_price')
        if row['signal_may_form']:
            texts[-1]+='\nSIGNAL MAY FORM — условия входа ещё не выполнены.'
        if assessment:
            texts[-1]+='\nChecks: '+str(assessment.get('checks'))+'\nMissing: '+', '.join(assessment.get('rejections',[]))+'\nRequired next conditions:\n'+'\n'.join(assessment.get('required_next_conditions',[]))
    for text in texts[1:]:
        await message.answer(texts[0]+'\n'+text,parse_mode=None,reply_markup=kb.get_main_menu_kb())


@router.callback_query(F.data=='setup_journal')
async def cb_setup_journal(call: CallbackQuery):
    await call.answer()
    if call.message:
        await _answer_setup_journal(call.message)


@router.message(Command('setups'))
async def cmd_setups(message: Message):
    await _answer_setup_journal(message)


async def _answer_market(message):
    from trading.engine import LATEST_DATA, MARKET_DATA, BENCHMARKS
    from trading.market_watch import market_brief, format_brief, feed
    text = format_brief(market_brief(LATEST_DATA, MARKET_DATA, BENCHMARKS))
    recent = feed(8)
    if recent:
        text += "\n\n<b>Последние наблюдения</b>\n" + "\n".join(f"• {escape(e['symbol'])}: {escape(e['text'])}" for e in recent)
    await message.answer(text[:4000], parse_mode="HTML")


@router.message(Command("market"))
async def cmd_market(message: Message):
    await _answer_market(message)


@router.callback_query(F.data == "market_brief")
async def cb_market_brief(call: CallbackQuery):
    await call.answer()
    await _answer_market(call.message)


@router.message(Command("coin"))
async def cmd_coin(message: Message, command: CommandObject):
    from trading.engine import LATEST_DATA
    from trading.market_watch import commentary
    from trading.news import news_context
    symbol = (command.args or "").strip().upper()
    if symbol and not symbol.endswith("USDT"):
        symbol += "USDT"
    if not symbol or symbol not in LATEST_DATA:
        await message.answer("Использование: /coin SOL (монета должна быть в сканируемом списке)")
        return
    c = commentary(symbol, LATEST_DATA[symbol], news_context(symbol))
    text = f"<b>{escape(symbol)}</b>\n" + "\n".join(escape(x) for x in c["lines"]) + f"\n\n<b>Вывод:</b> {escape(c['verdict'])}"
    await message.answer(text[:4000], parse_mode="HTML")


@router.message(Command("learning"))
async def cmd_learning(message: Message):
    from trading.learning import learning_summary
    data = learning_summary()
    shadow = data["shadow"]
    lines = ["<b>Самообучение</b>", f"Теневые сделки: открыто {shadow['open']}, закрыто {shadow['closed']}."]
    lines += [escape(x) for x in data["insights"]]
    useful = [b for b in data["blockers"] if b["blocker"] != "__passed__" and b["trades"] >= 10][:6]
    if useful:
        lines.append("\n<b>Фильтры</b>")
        lines += [f"• {escape(b['blocker'])}: {b['trades']} шт., {b['avg_r']:+.2f}R — {escape(b['verdict'])}" for b in useful]
    await message.answer("\n".join(lines)[:4000], parse_mode="HTML")
