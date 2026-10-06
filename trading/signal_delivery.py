"""Durable Telegram outbox and read-only access to fresh analyzed signals."""
import asyncio
import json
import math
import time
from collections import Counter
from html import escape
from core.database import _conn
from core.settings import cfg
from core.bybit import get_klines_async
from trading.data_quality import observed_price
from trading.zone_lifecycle import invalidation_reason
from trading.telemetry import event,heartbeat


def signal_zone_reason(signal, frame, now_ms=None):
    now_ms=time.time()*1000 if now_ms is None else now_ms
    zone=signal.get('fvg')
    if not zone:
        return 'zone_metadata_unavailable' if signal.get('strategy_version') else None
    from trading.data_quality import validate_ohlcv
    timeframe=signal.get('timeframe',cfg.timeframe)
    try:
        validate_ohlcv(frame,timeframe)
        start=signal.get('analyzed_at')
        if start is None or frame.timestamp.iloc[0] > (int(start*1000)//(int(timeframe)*60000))*(int(timeframe)*60000):
            return 'invalidation_history_incomplete'
        return invalidation_reason(zone,frame,timeframe,now_ms,signal.get('strict_ce',True))
    except (KeyError,ValueError,TypeError):
        return 'invalidation_data_unavailable'


def validation_limit(signal):
    # Keep enough history to re-check every candle after the FVG formed.  A
    # short age-only window could miss a fill that happened before delivery.
    lifecycle = int(cfg.fvg_lookback) + int(cfg.fvg_confirmations) + 5
    age_window = math.ceil(cfg.signal_max_age_seconds/(int(signal.get('timeframe',cfg.timeframe))*60)) + 3
    return max(3, lifecycle, age_window)


def _schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS signal_outbox (
        signal_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, payload TEXT NOT NULL,
        created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
        attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, error TEXT)""")
    columns={r[1] for r in con.execute('PRAGMA table_info(signal_outbox)')}
    for name,kind in [('sent_at','REAL'),('telegram_message_id','INTEGER')]:
        if name not in columns:
            con.execute(f'ALTER TABLE signal_outbox ADD COLUMN {name} {kind}')
    con.execute('CREATE TABLE IF NOT EXISTS delivery_attempts(id INTEGER PRIMARY KEY,signal_id TEXT NOT NULL,started_at REAL NOT NULL,finished_at REAL,result TEXT NOT NULL,message_id INTEGER,error TEXT)')


def begin_attempt(key):
    with _conn() as con:
        _schema(con)
        return con.execute("INSERT INTO delivery_attempts(signal_id,started_at,result) VALUES(?,?,'STARTED')",(key,time.time())).lastrowid


def finish_attempt(attempt,key,result,message_id=None,error=None):
    with _conn() as con:
        con.execute('UPDATE delivery_attempts SET finished_at=?,result=?,message_id=?,error=? WHERE id=?',(time.time(),result,message_id,error,attempt))
        if result=='API_ACCEPTED':
            con.execute("UPDATE signal_outbox SET state='SENT',sent_at=?,telegram_message_id=?,error=NULL,attempts=attempts+1 WHERE signal_id=?",(time.time(),message_id,key))
    event('SIGNAL_SENT' if result=='API_ACCEPTED' else 'SIGNAL_DELIVERY_FAILED',key,message_id=message_id,error=error,result=result)


def enqueue(signal_id, symbol, signal, chat_id, created=None):
    now = time.time() if created is None else float(created)
    payload = {"symbol": symbol, "signal": signal, "created": now}
    with _conn() as con:
        _schema(con)
        inserted=con.execute("INSERT OR IGNORE INTO signal_outbox(signal_id,chat_id,payload,created,expires) VALUES(?,?,?,?,?)",
                    (signal_id, str(chat_id), json.dumps(payload, allow_nan=False), now, now+cfg.signal_max_age_seconds))
        is_new=inserted.rowcount>0
    if is_new:event('SIGNAL_QUEUED',signal_id,symbol=symbol,created=now)


def original_signal(signal_id, signal):
    """Persist immutable analysis and original age independently of delivery state."""
    with _conn() as con:
        key = 'snapshot:'+signal_id
        value = json.dumps({'signal':signal,'created':time.time()},allow_nan=False)
        inserted=con.execute('INSERT OR IGNORE INTO settings_text(key,value) VALUES(?,?)',(key,value))
        is_new=inserted.rowcount>0
        result=json.loads(con.execute('SELECT value FROM settings_text WHERE key=?',(key,)).fetchone()[0])
    if is_new:event('SIGNAL_GENERATED',signal_id,score=signal.get('score'),generated_at=result['created'])
    return result


def outbox_rows(now=None):
    now = time.time() if now is None else now
    with _conn() as con:
        _schema(con)
        con.execute("UPDATE signal_outbox SET state='EXPIRED' WHERE state='PENDING' AND expires<=?", (now,))
        return [dict(row) for row in con.execute("SELECT * FROM signal_outbox WHERE state='PENDING' AND next_try<=? ORDER BY created LIMIT 5", (now,))]


def mark(signal_id, state, error=None, delay=0):
    with _conn() as con:
        con.execute("UPDATE signal_outbox SET state=?, error=?, attempts=attempts+1, next_try=? WHERE signal_id=?",
                    (state, error, time.time()+delay, signal_id))
    if error:event('SIGNAL_DELIVERY_FAILED',signal_id,state=state,error=error,retry_delay=delay)


def format_signal(symbol, signal, manual=False):
    label = "⚡ Быстрый просмотр сигнала" if manual else "🚨 Новый FVG-сигнал"
    return (f"<b>{label}: {escape(symbol)} {escape(signal['signal'])}</b>\n"
            f"Режим: paper | TF: {escape(str(signal.get('timeframe', cfg.timeframe)))}m\n"
            f"Вход: <b>{signal['entry']:.8g}</b>\nSL: {signal['sl']:.8g}\nTP: {signal['tp']:.8g}\n"
            f"Качество: {signal.get('score', 0)}/100 (не вероятность выигрыша)\n"
            f"Память сценария: {escape(signal.get('memory', {}).get('status', 'INSUFFICIENT'))}, закрытых сделок: {signal.get('memory', {}).get('trades', 0)}\n"
            f"R:R: 1:{signal['rr']:.2f}\n"
            f"Сценарий: {escape(signal.get('setup', 'fvg_retest'))} | Режим рынка: {escape(signal.get('regime', 'UNKNOWN'))}\n"
            f"Основание: {escape(signal.get('reasoning', {}).get('explanation', 'Подтверждённый FVG по правилам стратегии.'))}\n"
            f"Проверка: {escape(signal.get('evidence', 'unverified_paper'))}\n"
            f"Новости: {escape(signal.get('news', {}).get('coverage', 'UNKNOWN'))}\n"
            + ("Повторный просмотр анализа; новая paper-сделка не создаётся." if manual else "Исследовательский сигнал; прибыльность не гарантирована."))


def price_valid(signal, price):
    if not math.isfinite(price) or price <= 0:
        return False
    entry, sl, tp = (float(signal[k]) for k in ("entry", "sl", "tp"))
    risk = abs(entry-sl)
    inside = sl < price < tp if signal["signal"] == "LONG" else tp < price < sl
    return inside and risk > 0 and abs(price-entry) <= .25*risk


def cached_candidates(latest, now=None):
    now = time.time() if now is None else now
    result = []
    for symbol, row in latest.items():
        signal = row.get("signal")
        created = row.get("signal_created") or 0
        if signal and not row.get("blockers") and 0 <= now-created <= cfg.signal_max_age_seconds:
            result.append((symbol, signal))
    return sorted(result, key=lambda item: item[1].get("score", 0), reverse=True)


_WAIT_LABELS = {
    "no_recent_retest": "ожидается закрытый ретест FVG",
    "no_bullish_rejection": "нужна бычья свеча выше CE",
    "no_bearish_rejection": "нужна медвежья свеча ниже CE",
    "entry_too_far": "цена должна вернуться ближе к зоне",
}


def cached_observations(latest, now=None):
    """Return fresh, high-quality early setups when no confirmed entry exists."""
    now = time.time() if now is None else now
    result = []
    for symbol, row in latest.items():
        try:
            updated = row.get("updated_at")
            if updated:
                from datetime import datetime
                age = now - datetime.fromisoformat(str(updated)).timestamp()
                if age < 0 or age > 900:
                    continue
        except (TypeError, ValueError, OverflowError):
            continue
        zones = {str(z.get("candle_time")) + str(z.get("type")): z
                 for z in (row.get("zones") or [])}
        for key, assessment in (row.get("assessments") or {}).items():
            checks = assessment.get("checks") or {}
            if checks.get("Entry") is True or checks.get("Zone eligibility") is not True:
                continue
            if not all(checks.get(name) is True for name in ("FVG", "HTF bias", "Structure")):
                continue
            reasons = list(dict.fromkeys((assessment.get("rejections") or []) +
                                         (assessment.get("execution_rejections") or [])))
            waiting = [reason for reason in reasons if reason in _WAIT_LABELS]
            if not 1 <= len(waiting) <= 2 or set(reasons) - set(_WAIT_LABELS):
                continue
            liquidity = checks.get("Liquidity") is True
            threshold = float(cfg.min_signal_score if liquidity else
                              getattr(cfg, "early_setup_min_score", cfg.min_signal_score))
            score = float(assessment.get("score") or 0)
            if score < threshold:
                continue
            zone = zones.get(key)
            if not zone:
                continue
            price = float(row.get("current_price") or 0)
            distance = max(float(zone.get("bottom", 0)) - price,
                           price - float(zone.get("top", 0)), 0)
            result.append({"symbol": symbol, "row": row, "assessment": assessment,
                           "zone": zone, "waiting": waiting, "score": score,
                           "threshold": threshold, "liquidity": liquidity,
                           "distance": distance})
    return sorted(result, key=lambda item: (-item["score"], item["distance"]))


def format_observation(item, scanned=0):
    """Explain a quick directional observation without fabricating entry levels."""
    symbol, row, assessment, zone = (item["symbol"], item["row"],
                                     item["assessment"], item["zone"])
    direction = "LONG" if zone.get("type") == "BULLISH" else "SHORT"
    intelligence = row.get("intelligence") or {}
    waiting = "; ".join(_WAIT_LABELS.get(reason, reason) for reason in item["waiting"])
    liquidity = "подтверждена" if item["liquidity"] else "ещё не подтверждена"
    return (f"<b>⚡ Быстрый прогноз: {escape(symbol)} {direction}</b>\n"
            f"Статус: раннее наблюдение по свежему анализу ({scanned} монет проверено)\n"
            f"Качество модели: {item['score']:.0f}/100 | порог наблюдения: {item['threshold']:.0f}\n"
            f"FVG: {float(zone.get('bottom', 0)):.8g} — {float(zone.get('top', 0)):.8g}; "
            f"CE: {float(zone.get('ce', 0)):.8g}\n"
            f"Текущая цена: {float(row.get('current_price') or 0):.8g}\n"
            f"Тренд: {escape(str(intelligence.get('trend', 'UNKNOWN')))} | "
            f"режим: {escape(str(intelligence.get('regime', 'UNKNOWN')))} | "
            f"объём: {float(intelligence.get('relative_volume') or 0):.2f}x\n"
            f"Ликвидность: {liquidity}\n"
            f"Ждём: {escape(waiting)}\n"
            "Это модельный предсигнал по закрытым данным: уровни входа/SL/TP появятся "
            "только после полного подтверждения. Новая paper-сделка не создаётся.")


def scan_summary(latest):
    labels = {
        "no_recent_retest": "ожидается возврат в FVG",
        "low_relative_volume": "недостаточный относительный объём",
        "no_bullish_rejection": "нет подтверждения покупки",
        "no_bearish_rejection": "нет подтверждения продажи",
        "entry_too_far": "цена далеко от зоны",
        "zone_exhausted": "зона многократно протестирована",
        "execution_price_moved": "цена ушла от рассчитанного входа",
        "insufficient_execution_rr": "цель слишком близко к текущей цене",
        "insufficient_rr_after_costs": "недостаточная цель после комиссии",
        "price_outside_trade_levels": "цена уже вышла за уровни сделки",
    }
    reasons = Counter(reason for row in latest.values() for reason in row.get("blockers", []))
    reasons.update({"нет подтверждённого FVG": sum(not row.get("fvg") for row in latest.values())})
    for row in latest.values():
        reasons.update(row.get("setup_diagnostics", {}))
    text = "; ".join(f"{escape(labels.get(k,k))}: {v}" for k, v in reasons.most_common(4) if v)
    return f"Проверено пар: {len(latest)}. " + (text or "Анализ ещё выполняется.")


async def quick_signal(latest):
    from trading.news import news_context
    from trading.risk_manager import is_trading_allowed
    if not cfg.is_running:
        return "Сканер выключен. Включи его кнопкой Start Bot."
    if not is_trading_allowed():
        return "Входы приостановлены риск-лимитом. " + scan_summary(latest)

    async def _find_confirmed():
        for symbol, signal in cached_candidates(latest)[:8]:
            if cfg.enable_news and news_context(symbol)["blockers"]:
                continue
            frame = await get_klines_async(symbol, signal.get("timeframe", cfg.timeframe),
                                           limit=validation_limit(signal))
            if signal_zone_reason(signal, frame):
                continue
            price = observed_price(frame, signal.get("timeframe", cfg.timeframe), time.time()*1000)
            if price is not None and price_valid(signal, price):
                return format_signal(symbol, signal, manual=True) + f"\nПроверенная сейчас цена: {price:.8g}"
        return None

    found = await _find_confirmed()
    if found:
        return found

    # Previously the button only inspected the cache.  Run a bounded fresh
    # scan now, then inspect the newly written cache for a real candidate.
    scanned = 0
    try:
        from trading.engine import quick_scan
        scanned = await quick_scan(max_symbols=80)
    except Exception as exc:
        event("QUICK_SCAN_FAILED", error=type(exc).__name__)

    found = await _find_confirmed()
    if found:
        return found + f"\nСвежий ручной анализ: проверено монет {scanned}."
    observations = cached_observations(latest)
    if observations:
        return format_observation(observations[0], scanned=scanned)
    return ("Сейчас нет свежего подтверждённого входа и нет раннего сетапа, "
            "достаточно близкого к срабатыванию.\n" + scan_summary(latest) +
            f"\nРучной анализ проверил монет: {scanned}.")


async def deliver_once(bot):
    if not bot or not cfg.is_running:
        return
    from trading.news import news_context
    from trading.risk_manager import is_trading_allowed
    for row in outbox_rows():
        if str(row["chat_id"]) != str(cfg.tg_chat_id):
            mark(row["signal_id"], "EXPIRED", "recipient_changed")
            continue
        payload = json.loads(row["payload"])
        signal, symbol = payload["signal"], payload["symbol"]
        attempt=None
        try:
            if not is_trading_allowed() or (cfg.enable_news and news_context(symbol)["blockers"]):
                continue
            frame = await get_klines_async(symbol, signal.get("timeframe", cfg.timeframe), limit=validation_limit(signal))
            zone_reason=signal_zone_reason(signal,frame)
            if zone_reason:
                if zone_reason in ('invalidation_data_unavailable','invalidation_history_incomplete'):
                    mark(row['signal_id'],'PENDING',zone_reason,15)
                else:
                    mark(row['signal_id'],'EXPIRED',zone_reason)
                continue
            price = observed_price(frame, signal.get("timeframe", cfg.timeframe), time.time()*1000)
            if price is None:
                mark(row["signal_id"], "PENDING", "price_unavailable", 15)
                continue
            if not price_valid(signal, price):
                mark(row["signal_id"], "EXPIRED", "price_moved")
                continue
            from tg.keyboards import get_main_menu_kb
            attempt=begin_attempt(row['signal_id'])
            receipt=await bot.send_message(row["chat_id"], format_signal(symbol, signal), parse_mode="HTML", reply_markup=get_main_menu_kb())
            message_id=getattr(receipt,'message_id',None)
            finish_attempt(attempt,row['signal_id'],'API_ACCEPTED',message_id if isinstance(message_id,int) else None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt is not None:
                finish_attempt(attempt,row['signal_id'],'ERROR_OR_UNKNOWN',error=type(exc).__name__)
            # Telegram has no idempotency keys: an ambiguous timeout may cause a duplicate.
            delay = max(float(getattr(exc, "retry_after", 0)), min(120, 5*2**min(row["attempts"], 4)))
            mark(row["signal_id"], "PENDING", type(exc).__name__, delay)


async def delivery_worker(bot):
    while True:
        try:
            heartbeat('delivery')
            await deliver_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A DB or network failure must not terminate the delivery worker.
            heartbeat('delivery',type(exc).__name__)
            event('DELIVERY_WORKER_FAILED',error=type(exc).__name__)
        await asyncio.sleep(5)
