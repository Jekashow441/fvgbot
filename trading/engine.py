import asyncio
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from core.settings import cfg, reload_settings
from core.bybit import get_klines_async, get_market_tickers, get_coin_microstructure
from core.database import get_active_signals, get_setting_text, set_setting_text
from trading.context import closed_candles, ticker_context
from trading.strategy import detect_fvg, detect_fvgs, validate_signal, market_context
from trading.paper_trading import open_paper_trade, check_active_trades
from trading.risk_manager import is_trading_allowed
from trading.learning import apply_learning_to_signal, is_symbol_in_cooldown
from trading.logger import log_info
from trading.intelligence import coin_context, microstructure_context, live_blockers, required_book_depth
from trading.position_risk import position_notional
from trading.news import refresh_news, news_context, NEWS_STATE
from trading.research import request_research, research_worker, load_report, qualify, research_gate
from trading.profiles import strategy_settings
from trading.signal_delivery import enqueue, delivery_worker, original_signal, price_valid
from trading.entry_rules import entry_rejection
from trading.data_quality import decision_candles, observed_price
from trading.version import STRATEGY_VERSION
from trading.zone_lifecycle import invalidation_reason
from trading.setup_journal import observe, setup_id
from trading.setup_assessment import assess_zones
from trading.decision_memory import assess_context, settings_key
from trading.telemetry import event,record_cycle,heartbeat
from trading.market_watch import observe as market_observe

LATEST_DATA = {}
SENT_SIGNALS = {}
MARKET_DATA = {}
MARKET_UPDATED = 0.0
BENCHMARKS = {}
SCAN_STATUS = {'selected':0,'processed':0,'cycle_seconds':None,'last_completed_at':None}
SCAN_RESULTS = {}

# Serialize manual scans so repeated button presses cannot interleave writes
# to LATEST_DATA or flood the exchange API while the background loop runs.
_QUICK_SCAN_LOCK = None


def apply_news_tone(signal: dict, coin_news: Optional[dict]) -> None:
    """Headline tone nudges the score; it never creates a setup on its own."""
    tone = (coin_news or {}).get("sentiment")
    weight = int(cfg.coin_news_score_weight)
    if tone is None or not weight:
        return
    aligned = tone if signal["signal"] == "LONG" else -tone
    if aligned >= 0.25:
        signal["score"] = min(100, signal["score"] + weight)
        signal["factors"] = list(signal.get("factors") or []) + ["news_tailwind"]
    elif aligned <= -0.25:
        signal["score"] = max(0, signal["score"] - weight)
        signal["factors"] = list(signal.get("factors") or []) + ["news_headwind"]


def _signal_key(symbol: str, fvg: dict, signal: dict) -> str:
    return f"{symbol}_{signal.get('timeframe', cfg.timeframe)}_{fvg.get('candle_time')}_{signal.get('signal')}"


def _pick_htf() -> Optional[str]:
    if str(cfg.timeframe).isdigit() and int(cfg.context_timeframe) > int(cfg.timeframe):
        return cfg.context_timeframe
    timeframes = [tf for tf in cfg.fvg_timeframes if str(tf) != str(cfg.timeframe)]
    if not timeframes:
        return None
    try:
        current = int(cfg.timeframe)
        higher = sorted([int(tf) for tf in timeframes if int(tf) > current])
        return str(higher[0]) if higher else None
    except Exception:
        return str(timeframes[-1])


async def _get_htf_context(symbol: str) -> tuple[Optional[str], Optional[dict]]:
    htf = _pick_htf()
    if not htf:
        return None, None
    df_htf = await get_klines_async(symbol, htf, limit=max(cfg.ema_period + 61, 351))
    if df_htf.empty:
        return htf, None
    try:
        closed = decision_candles(df_htf, htf, int(time.time()*1000), cfg.ema_period)
        return htf, market_context(closed)
    except ValueError as exc:
        return htf, {"trend":"UNKNOWN", "error":str(exc)}


async def process_symbol(bot: Optional[Bot], symbol: str, *, emit_signal: bool = True,
                         force_reanalysis: bool = False, run_research: bool = True) -> None:
    SCAN_RESULTS[symbol]={'outcome':'failed','reason':'incomplete_scan','started_at':time.time()}
    try:
        limit = max(cfg.ema_period + 61, cfg.fvg_lookback + 81, 351)
        df = await get_klines_async(symbol, cfg.timeframe, limit=limit)
        if df.empty:
            SCAN_RESULTS[symbol]['reason']='empty_candles'
            LATEST_DATA[symbol]={'signal':None,'blockers':['empty_candles'],'updated_at':datetime.now(timezone.utc).isoformat()}
            return

        current_price = observed_price(df, cfg.timeframe, time.time()*1000)
        if current_price is None:
            SCAN_RESULTS[symbol].update(outcome='stale',reason='stale_price')
            LATEST_DATA[symbol] = {"signal":None,"blockers":["stale_price"]}
            return
        observed_frame = df.copy()
        # Point observations cannot contain extrema that occurred before entry.
        await check_active_trades(bot, symbol, current_price, current_price)
        try:
            df = decision_candles(df, cfg.timeframe, int(time.time()*1000), cfg.ema_period)
        except ValueError as exc:
            SCAN_RESULTS[symbol].update(outcome='stale' if 'stale' in str(exc) else 'failed',reason=str(exc))
            LATEST_DATA[symbol] = {"signal":None,"blockers":[str(exc)],"status":"INVALID_CANDLES"}
            return
        if df.empty:
            return
        if str(cfg.timeframe).isdigit() and time.time()*1000 - int(df.iloc[-1]["timestamp"]) > int(cfg.timeframe)*60000*2:
            LATEST_DATA[symbol] = {"signal": None, "current_price": current_price, "status": "STALE_CANDLES"}
            return
        candle_key = str(int(df.iloc[-1]["timestamp"]))
        SCAN_RESULTS[symbol].update(outcome='fresh',reason=None,closed_bar=int(candle_key),htf='not_required')
        coin = coin_context(df, BENCHMARKS.get("BTCUSDT", {}).get("candles"))
        news = news_context(symbol) if cfg.enable_news else {"coverage": "DISABLED", "blockers": [], "events": []}
        qualification, research_reasons = research_gate(load_report(symbol))
        effective = strategy_settings()
        if cfg.enable_coin_research and run_research:
            request_research(symbol)
        analysis_key = f"{settings_key(cfg)}:{candle_key}:{qualification['status']}:{NEWS_STATE.get('updated_at')}:{MARKET_UPDATED}"
        cached=LATEST_DATA.get(symbol,{})
        # Retry changing execution gates on each scan when a closed-bar candidate exists.
        # Pure technical rejections remain cached until their input changes.
        retry_execution=any(a.get('checks',{}).get('Entry') for a in cached.get('assessments',{}).values())
        if cached.get("analysis_key") == analysis_key and not retry_execution and not force_reanalysis:
            LATEST_DATA[symbol]["current_price"] = current_price
            LATEST_DATA[symbol]['updated_at']=datetime.now(timezone.utc).isoformat()
            SCAN_RESULTS[symbol]['htf']='unavailable' if (cached.get('htf_context') or {}).get('error') else ('fresh_cached' if cached.get('htf_context') else 'not_required')
            previous=LATEST_DATA[symbol].get('signal')
            if previous and previous.get('fvg'):
                why=invalidation_reason(previous['fvg'],observed_frame,cfg.timeframe,time.time()*1000,previous.get('strict_ce',True))
                if why:
                    LATEST_DATA[symbol]['blockers']=list(set(LATEST_DATA[symbol].get('blockers',[])+[why]))
            observe(symbol,cfg.timeframe,LATEST_DATA[symbol],observed_frame,cfg)
            return

        zones = detect_fvgs(df, effective)
        fvg = zones[0] if zones else None
        cooldown, cooldown_until = is_symbol_in_cooldown(symbol) if fvg else (False, None)
        if cooldown:
            SCAN_RESULTS[symbol].update(outcome='skipped',reason='cooldown')
            LATEST_DATA.setdefault(symbol, {})
            LATEST_DATA[symbol].update({
                "fvg": fvg,
                "signal": None,
                "current_price": current_price,
                "cooldown_until": cooldown_until,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            return

        htf, htf_ctx = await _get_htf_context(symbol) if fvg else (None, None)
        SCAN_RESULTS[symbol]['htf']='unavailable' if htf and (not htf_ctx or htf_ctx.get('error')) else 'fresh' if htf else 'not_required'
        signal = None
        blockers = []
        micro = None
        setup_diagnostics = {}
        blockers += research_reasons
        blockers += news["blockers"]
        if not BENCHMARKS or time.monotonic()-MARKET_UPDATED > cfg.universe_refresh_seconds*2:
            blockers.append("benchmark_context_unavailable")
        allowed = is_trading_allowed()
        if not allowed:
            blockers.append("risk_guard")
        if fvg and htf and (not htf_ctx or htf_ctx.get("trend") == "UNKNOWN"):
            blockers.append("htf_unavailable_or_stale")
        base_blockers = list(blockers)
        assessments=assess_zones(df,zones,htf_ctx,MARKET_DATA.get(symbol),effective) if zones else {}
        if fvg and MARKET_DATA.get(symbol, {}).get("eligible") and allowed and (not htf or (htf_ctx and htf_ctx.get("trend") != "UNKNOWN")):
            candidates = [(zone,assessments[str(zone['candle_time'])+zone['type']]['candidate']) for zone in zones]
            for assessment in assessments.values():
                for reason in assessment['rejections']:
                    setup_diagnostics[reason]=setup_diagnostics.get(reason,0)+1
            candidates = sorted(((z,c) for z,c in candidates if c), key=lambda pair:(-pair[1]["score"], pair[0]["touches"], pair[0]["age"]))
            for zone, candidate in candidates:
                zone_invalid = invalidation_reason(zone,observed_frame,cfg.timeframe,time.time()*1000,cfg.fvg_strict_mitigation)
                if zone_invalid:
                    blockers.append(zone_invalid)
                    continue
                if candidate:
                    # Detailed feeds are fetched only for actual strategy candidates.
                    if micro is None:
                        raw,higher=await asyncio.gather(get_coin_microstructure(symbol),get_klines_async(symbol,'240',limit=260),return_exceptions=True)
                        try:
                            if isinstance(raw,Exception):raise raw
                            micro = microstructure_context(raw)
                        except Exception as exc:
                            event('ORDERBOOK_UNAVAILABLE',symbol=symbol,error=type(exc).__name__)
                            micro = {"status": "UNAVAILABLE"}
                        try:
                            if isinstance(higher,Exception):raise higher
                            coin["higher_4h"] = market_context(decision_candles(higher, "240", int(time.time()*1000), cfg.ema_period))
                            SCAN_RESULTS[symbol]['higher_4h']='fresh'
                        except Exception as exc:
                            event('HTF_DATA_UNAVAILABLE',symbol=symbol,timeframe='240',error=type(exc).__name__,reason=str(exc) if isinstance(exc,ValueError) else 'request_failed')
                            coin['higher_4h']={'trend':'UNKNOWN','error':type(exc).__name__}
                            SCAN_RESULTS[symbol]['higher_4h']='unavailable'
                    active_trades = get_active_signals()
                    notional = position_notional(candidate["entry"], candidate["sl"], cfg.paper_balance, active_trades, cfg)
                    reasons = live_blockers(candidate["signal"], coin, BENCHMARKS.get("BTCUSDT", {}).get("trend"), MARKET_DATA.get(symbol, {}), micro, notional=notional)
                    coin_headlines = (news.get("coin") or {})
                    if candidate["signal"] == "LONG" and coin_headlines.get("critical"):
                        reasons.append("critical_negative_news")
                    if coin.get("higher_4h", {}).get("trend", "UNKNOWN") == "UNKNOWN":
                        reasons.append("higher_4h_unavailable_or_stale")
                    if coin.get("higher_4h", {}).get("trend") == ("DOWN" if candidate["signal"] == "LONG" else "UP"):
                        reasons.append("higher_4h_opposition")
                    if sum(t["side"] == candidate["signal"] for t in active_trades) >= cfg.max_same_direction_positions:
                        reasons.append("same_direction_exposure_limit")
                    event('ENTRY_CONTEXT_CHECK',setup_id(symbol,cfg.timeframe,zone),symbol=symbol,score=candidate['score'],rejections=list(dict.fromkeys(reasons+base_blockers)),microstructure=micro,higher_4h=coin.get('higher_4h'),distance_vwap_atr=coin.get('distance_vwap_atr'),min_book_depth_usdt=required_book_depth(notional),max_spread_bps=cfg.max_spread_bps)
                    if reasons or base_blockers:
                        assessments[str(zone['candle_time'])+zone['type']]['execution_rejections']=list(dict.fromkeys(reasons+base_blockers))
                        blockers = list(dict.fromkeys(blockers + reasons))
                        continue
                    exec_price = micro["ask"] if candidate["signal"] == "LONG" else micro["bid"]
                    # Reprice at the observed executable-side estimate; keep structural levels.
                    side = candidate["signal"]
                    risk = exec_price-candidate["sl"] if side == "LONG" else candidate["sl"]-exec_price
                    reward = candidate["tp"]-exec_price if side == "LONG" else exec_price-candidate["tp"]
                    execution_reason = entry_rejection(candidate, exec_price, cfg)
                    if execution_reason:
                        event('ENTRY_PRICE_REJECTED',setup_id(symbol,cfg.timeframe,zone),symbol=symbol,reason=execution_reason,observed_price=exec_price,signal_entry=candidate['entry'],sl=candidate['sl'],tp=candidate['tp'])
                        assessments[str(zone['candle_time'])+zone['type']]['execution_rejections']=[execution_reason]
                        blockers.append(execution_reason)
                        continue
                    current_price = exec_price
                    candidate.update(entry=current_price, rr=reward/risk, timeframe=cfg.timeframe, strategy_version=STRATEGY_VERSION, strategy_profile=cfg.strategy_profile,
                                     execution_version="cost_aware_v1", settings_fingerprint=settings_key(cfg),
                                     evidence="historical_qualified" if qualification["status"] == "PASS" else "unverified_paper",
                                     intelligence=coin, news=news, qualification=qualification, microstructure=micro)
                    candidate.update(fvg=dict(zone), strict_ce=cfg.fvg_strict_mitigation, journal_id=setup_id(symbol,cfg.timeframe,zone), analyzed_at=time.time())
                    candidate["memory"] = assess_context(candidate)
                    apply_news_tone(candidate, news.get("coin"))
                    fvg, signal = zone, candidate
                    break
        if signal is not None and signal["score"] < cfg.min_signal_score:
            blockers.append("news_score_below_threshold")
            signal = None
        had_candidate = signal is not None
        signal = apply_learning_to_signal(signal) if signal else None
        if had_candidate and signal is None:
            blockers.append("learning_score_below_threshold")
        signal_created = None

        if fvg and signal:
            blockers = []
            signal_id = _signal_key(symbol, fvg, signal)
            signal.update(signal_id=signal_id,generated_at=time.time())
            snapshot = original_signal(signal_id, signal)
            signal, signal_created = snapshot['signal'], snapshot['created']
            legacy_key = f"{symbol}_{fvg.get('candle_time')}_{signal.get('signal')}"
            if get_setting_text('signal:'+legacy_key, '') == 'opened':
                blockers.append('previously_used_zone')
            if not 0 <= time.time()-signal_created <= cfg.signal_max_age_seconds or not price_valid(signal, current_price):
                blockers.append("original_signal_expired_or_price_moved")
            SENT_SIGNALS.setdefault(symbol, set())
            if (emit_signal and not blockers and signal_id not in SENT_SIGNALS[symbol]
                    and get_setting_text("signal:" + signal_id, "") != "opened"):
                # Notification eligibility does not depend on paper portfolio capacity.
                if cfg.tg_chat_id:
                    enqueue(signal_id, symbol, signal, cfg.tg_chat_id, created=signal_created)
                else:
                    event('SIGNAL_DELIVERY_FAILED',signal_id,error='chat_not_configured')
                from trading.paper_trading import prepare_paper_entry
                trade_signal,fill_reason=prepare_paper_entry(signal,current_price,micro,cfg)
                if fill_reason is None:
                    signal['paper_opened']=open_paper_trade(symbol,trade_signal)
                    signal['paper_trade_id']=trade_signal.get('paper_trade_id')
                else:
                    signal['paper_opened']=False
                    signal['paper_skip_reason']=fill_reason
                set_setting_text("signal:" + signal_id, "opened")
                SENT_SIGNALS[symbol] = {signal_id}

        before = dict(LATEST_DATA.get(symbol) or {})
        LATEST_DATA.setdefault(symbol, {})
        LATEST_DATA[symbol].update({
            "fvg": fvg,
            "zones": zones,
            "assessments": {key:{k:v for k,v in a.items() if k!='candidate'} for key,a in assessments.items()},
            "signal": signal,
            "signal_created": signal_created,
            "current_price": current_price,
            "htf_timeframe": htf,
            "htf_context": htf_ctx,
            "cooldown_until": None,
            "processed_candle": candle_key,
            "analysis_key": analysis_key,
            "intelligence": coin,
            "news": news,
            "qualification": qualification,
            "microstructure": micro,
            "blockers": list(dict.fromkeys(blockers)),
            "base_blockers":base_blockers,
            "research_mode": cfg.research_gate_mode,
            "setup_diagnostics": setup_diagnostics,
            "strict_ce": cfg.fvg_strict_mitigation,
            "market": MARKET_DATA.get(symbol),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        observe(symbol,cfg.timeframe,LATEST_DATA[symbol],observed_frame,cfg)
        if before.get("analysis_key"):
            market_observe(symbol, before, LATEST_DATA[symbol])
    except Exception as e:
        SCAN_RESULTS[symbol].update(outcome='failed',reason=type(e).__name__)
        LATEST_DATA[symbol]={'signal':None,'blockers':['scan_error:'+type(e).__name__],'updated_at':datetime.now(timezone.utc).isoformat()}
        log_info(f"Error processing {symbol}: {type(e).__name__}: {e}")
        event('SCAN_SYMBOL_FAILED',symbol=symbol,error=type(e).__name__)


async def _ensure_market_context() -> bool:
    """Refresh the shared universe/benchmark snapshot when a manual scan needs it."""
    global MARKET_UPDATED
    fresh = bool(MARKET_DATA and BENCHMARKS and
                 time.monotonic() - MARKET_UPDATED <= cfg.universe_refresh_seconds)
    if fresh:
        return True
    try:
        snapshot = ticker_context(await get_market_tickers(), cfg.min_turnover_24h, cfg.max_spread_bps)
        if not snapshot:
            raise RuntimeError("empty_market_snapshot")
        leaders = await asyncio.gather(*(
            get_klines_async(leader, cfg.timeframe, limit=300)
            for leader in ("BTCUSDT", "ETHUSDT")
        ), return_exceptions=True)
        benchmarks = {}
        for leader, candles in zip(("BTCUSDT", "ETHUSDT"), leaders):
            if isinstance(candles, Exception):
                raise candles
            closed = decision_candles(candles, cfg.timeframe, int(time.time()*1000), cfg.ema_period)
            if len(closed) < cfg.ema_period:
                raise RuntimeError(f"missing_benchmark_{leader}")
            benchmarks[leader] = {"candles": closed, **market_context(closed),
                                  "updated_at": int(time.time()*1000)}
        MARKET_DATA.clear()
        MARKET_DATA.update(snapshot)
        BENCHMARKS.clear()
        BENCHMARKS.update(benchmarks)
        MARKET_UPDATED = time.monotonic()
        return True
    except Exception as exc:
        log_info(f"Quick market refresh failed: {type(exc).__name__}: {exc}")
        event("QUICK_MARKET_REFRESH_FAILED", error=type(exc).__name__)
        return False


async def quick_scan(max_symbols: int = 80) -> int:
    """Run a bounded, read-only fresh scan for the Telegram quick-signal button.

    The regular loop owns delivery and paper positions.  This path only updates
    the analysis cache, so pressing the button cannot create a duplicate trade.
    """
    global _QUICK_SCAN_LOCK
    if _QUICK_SCAN_LOCK is None:
        _QUICK_SCAN_LOCK = asyncio.Lock()
    async with _QUICK_SCAN_LOCK:
        reload_settings()
        if not cfg.is_running or not await _ensure_market_context():
            return 0
        if cfg.scan_all_symbols:
            symbols = [symbol for symbol, row in sorted(
                MARKET_DATA.items(), key=lambda item: item[1].get("volume_rank", 10**9)
            ) if row.get("eligible")]
        else:
            symbols = [symbol for symbol in cfg.symbols if MARKET_DATA.get(symbol, {}).get("eligible")]
        symbols = list(dict.fromkeys(symbols))[:max(1, int(max_symbols))]
        if not symbols:
            return 0
        semaphore = asyncio.Semaphore(max(1, min(int(cfg.bybit_max_concurrency), 8)))

        async def _limited(symbol: str) -> None:
            async with semaphore:
                await process_symbol(None, symbol, emit_signal=False,
                                     force_reanalysis=True, run_research=False)

        event("QUICK_SCAN_STARTED", symbols=len(symbols))
        await asyncio.gather(*(_limited(symbol) for symbol in symbols))
        event("QUICK_SCAN_FINISHED", symbols=len(symbols))
        return len(symbols)


async def _scan_loop(bot: Optional[Bot]) -> None:
    global MARKET_UPDATED
    log_info("Trading loop started.")
    heartbeat('scanner')
    while True:
        reload_settings()
        from trading.pipeline_recovery import record_policy
        record_policy(cfg)
        if not cfg.is_running:
            await asyncio.sleep(5)
            continue

        if time.monotonic() - MARKET_UPDATED > cfg.universe_refresh_seconds or not MARKET_DATA:
            try:
                snapshot = ticker_context(await get_market_tickers(), cfg.min_turnover_24h, cfg.max_spread_bps)
                if not snapshot:
                    raise RuntimeError("Empty market snapshot")
                for leader in ("BTCUSDT", "ETHUSDT"):
                    candles = decision_candles(await get_klines_async(leader, cfg.timeframe, limit=300), cfg.timeframe, int(time.time()*1000), cfg.ema_period)
                    if len(candles) < cfg.ema_period:
                        raise RuntimeError(f"Missing benchmark {leader}")
                    BENCHMARKS[leader] = {"candles": candles, **market_context(candles), "updated_at": int(time.time()*1000)}
                MARKET_DATA.clear()
                MARKET_DATA.update(snapshot)
                MARKET_UPDATED = time.monotonic()
            except Exception as exc:
                log_info(f"Market volume refresh failed: {exc}")
                # Manage open positions, but do not issue signals with stale market context.
                for trade in get_active_signals():
                    quotes = await get_klines_async(trade["symbol"], cfg.timeframe, limit=3)
                    price = observed_price(quotes, cfg.timeframe, time.time()*1000)
                    if price is not None:
                        await check_active_trades(bot, trade["symbol"], price, price)
                await asyncio.sleep(15)
                continue
        selected = [s for s, row in sorted(MARKET_DATA.items(), key=lambda x: x[1]["volume_rank"]) if row["eligible"]] if cfg.scan_all_symbols else cfg.symbols
        if cfg.enable_coin_research:
            for symbol in selected:
                request_research(symbol)
        symbols = list(dict.fromkeys(list(selected) + [t["symbol"] for t in get_active_signals()]))
        for stale_symbol in set(LATEST_DATA)-set(symbols):
            LATEST_DATA.pop(stale_symbol,None)
            SCAN_RESULTS.pop(stale_symbol,None)
        if not symbols:
            await asyncio.sleep(10)
            continue

        semaphore = asyncio.Semaphore(max(1, int(cfg.bybit_max_concurrency)))
        cycle_started=time.monotonic()
        cycle_wall_started=time.time()
        SCAN_STATUS.update(selected=len(symbols),processed=0)

        async def _limited(symbol: str) -> None:
            async with semaphore:
                await process_symbol(bot, symbol)
                SCAN_STATUS['processed']+=1

        await asyncio.gather(*[_limited(symbol) for symbol in symbols])
        coverage=record_cycle(cycle_wall_started,SCAN_RESULTS,symbols)
        SCAN_STATUS.update(coverage=coverage['counts'],htf=coverage['htf'])
        SCAN_STATUS.update(cycle_seconds=round(time.monotonic()-cycle_started,2),last_completed_at=datetime.now(timezone.utc).isoformat())
        await asyncio.sleep(15)


async def _news_loop():
    while True:
        if cfg.is_running and cfg.enable_news:
            try:
                await refresh_news()
            except Exception as exc:
                log_info(f"News refresh: {type(exc).__name__}: {exc}")
        await asyncio.sleep(cfg.news_refresh_seconds if cfg.is_running else 5)


async def _position_loop(bot):
    # Long research/scanning cycles must not postpone management of open positions.
    while True:
        try:
            heartbeat('positions')
            if cfg.is_running:
                for trade in get_active_signals():
                    df = await get_klines_async(trade["symbol"], cfg.timeframe, limit=3)
                    price = observed_price(df, cfg.timeframe, time.time()*1000)
                    if price is not None:
                        await check_active_trades(bot, trade["symbol"], price, price)
        except Exception as exc:
            heartbeat('positions',type(exc).__name__)
            event('POSITION_WORKER_FAILED',error=type(exc).__name__)
        await asyncio.sleep(5)


async def _recovery_loop():
    from trading.pipeline_recovery import reconcile
    while True:
        try:
            reconcile()
        except Exception as exc:
            log_info(f'Pipeline recovery: {type(exc).__name__}: {exc}')
        await asyncio.sleep(30)


def news_priority_symbols() -> list:
    """Coins a trader would watch most closely: open positions, live signals, ripe setups."""
    symbols = [t["symbol"] for t in get_active_signals()]
    symbols += [s for s, info in LATEST_DATA.items() if (info or {}).get("signal")]
    symbols += [s for s, info in LATEST_DATA.items()
                if any(a.get("status") in ("ENTRY APPROACHING", "POTENTIAL") for a in ((info or {}).get("assessments") or {}).values())]
    return list(dict.fromkeys(symbols))


def news_background_symbols() -> list:
    ranked = sorted(MARKET_DATA.items(), key=lambda x: x[1].get("volume_rank", 10**9))
    return [s for s, row in ranked if row.get("eligible")]


async def trading_loop(bot: Optional[Bot]) -> None:
    from trading.system_status import status_worker
    from trading.coin_news import coin_news_worker
    tasks = [asyncio.create_task(coin_news_worker(news_priority_symbols, news_background_symbols)),
             asyncio.create_task(status_worker(bot)),asyncio.create_task(_recovery_loop()),asyncio.create_task(delivery_worker(bot)), asyncio.create_task(_news_loop()), asyncio.create_task(research_worker()), asyncio.create_task(_position_loop(bot))]
    try:
        await _scan_loop(bot)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
