"""Non-trading journal notifications. Never creates signals or paper positions."""
import asyncio
import json
import time
from datetime import datetime,timezone
from core.database import _conn
from core.settings import cfg
from trading.setup_journal import history,latest_events

TERMINAL={'INVALIDATED','EXPIRED','MISSED','TP HIT','SL HIT'}
WAIT_REASONS={'no_recent_retest':'закрытый ретест FVG','no_bullish_rejection':'bullish close > open и close ≥ CE','no_bearish_rejection':'bearish close < open и close ≤ CE','entry_too_far':'возврат цены в допустимую ATR-дистанцию'}

def diagnostic(events,now=None):
    if not events:return None
    now=time.time() if now is None else now
    last=events[-1];p=last['payload'];a=p.get('assessment') or {};checks=a.get('checks') or {};z=p.get('fvg') or {}
    state=last['status'];expired=now>p.get('expires_at',float('inf'))
    if expired and state not in TERMINAL:state='EXPIRED'
    reasons=list(dict.fromkeys((a.get('rejections') or [])+(a.get('execution_rejections') or [])))
    missing=[r for r in reasons if r in WAIT_REASONS]
    complete={'FVG':checks.get('FVG') is True,'displacement':z.get('displacement_atr',0)>=cfg.displacement_atr,'structure':checks.get('Structure') is True,'liquidity':checks.get('Liquidity') is True,'HTF':checks.get('HTF bias') is True}
    recent=now-last['observed_at']<=600 and 0<=now-(a.get('analysis_bar',0)/1000+int(p.get('timeframe','5'))*60)<=600
    # Liquidity remains visible as a quality dimension, but an early notice
    # may be sent when the core FVG/HTF/structure checks pass. Confirmed
    # entries retain all production gates; this never creates a trade.
    core_complete=all(complete[k] for k in ('FVG','displacement','structure','HTF'))
    liquidity_ok=complete['liquidity']
    early_enabled=bool(getattr(cfg,'enable_early_setup_alerts',False))
    try:
        production_threshold=float((a.get('policy') or {}).get('min_score',cfg.min_signal_score))
        observation_threshold=float(getattr(cfg,'early_setup_min_score',production_threshold))
        score=float(a.get('score',p.get('fvg_quality',0)))
        score_ok=score >= (production_threshold if liquidity_ok else observation_threshold)
    except (TypeError,ValueError):
        score_ok=False
    qualifies=score_ok and core_complete and checks.get('Zone eligibility') is True and 1<=len(missing)<=2 and set(reasons)<=set(WAIT_REASONS) and state not in TERMINAL|{'CONFIRMED','RECOVERED CONFIRMATION'} and recent and (liquidity_ok or early_enabled)
    first=None
    for e in events:
        ea=e['payload'].get('assessment') or {};rs=(ea.get('rejections') or [])+(ea.get('execution_rejections') or [])
        if rs:
            first={'at':e['observed_at'],'reason':rs[0],'meaning':'Первый записанный отказ, может быть временным'};break
    terminal=next(({'at':e['observed_at'],'reason':e['reason']} for e in events if e['status'] in TERMINAL),None)
    return {'id':last['setup_id'],'asset':p.get('asset'),'direction':p.get('direction'),'timeframe':p.get('timeframe'),'state':state,'qualifies':qualifies,'early':bool(qualifies and not liquidity_ok),'completed':complete,'missing':missing,'all_reasons':reasons,'first_blocker':first,'irreversible_terminal':terminal,'score':a.get('score',p.get('fvg_quality')),'threshold':(a.get('policy') or {}).get('min_score','UNKNOWN'),'data_quality':'FRESH' if recent else 'STALE / UNKNOWN','updated':last['observed_at'],'fvg':z,'approaching':a.get('status')=='ENTRY APPROACHING','confirmed':checks.get('Entry') is True,'quality_groups':{'structure':sum(complete[k] for k in ('structure','liquidity','displacement')),'structure_max':3,'location':{'fvg':complete['FVG'],'htf':complete['HTF'],'premium_discount':'UNKNOWN'},'execution':p.get('execution_context') or 'UNKNOWN','entry':{'recorded_entry':checks.get('Entry'),'waiting':missing}}}

def render(d,event=None):
    if not d:return 'Setup ID не найден в журнале.'
    z=d['fvg'];first=d['first_blocker']
    # Keep the existing SIGNAL MAY FORM label for compatibility; the body
    # explicitly marks every such message as diagnostic-only.
    if event is None and d.get('qualifies'):
        event='MAY_FORM'
    lines=[('🟡 SIGNAL MAY FORM — '+event) if event and event!='INVALIDATED' else '⚪ SETUP CLOSED — INVALIDATED/EXPIRED' if event else 'WHY NOT SIGNAL?', 'НЕ ТОРГОВЫЙ СИГНАЛ. Paper-сделка не создаётся.',d['id'],f"{d['asset']} {d['direction']} | TF {d['timeframe']}m | {d['state']}",'COMPLETED: '+', '.join(k for k,v in d['completed'].items() if v),'WAITING FOR: '+('; '.join(WAIT_REASONS.get(r,r) for r in d['missing']) or '; '.join(d['all_reasons']) or 'полная проверка актуальных условий'),f"FVG: {z.get('bottom')} — {z.get('top')}; CE: {z.get('ce')}",f"Invalidation: полное заполнение зоны, запрещённое закрытие за CE или expiry.", 'Next trigger: '+('; '.join(WAIT_REASONS.get(r,r) for r in d['missing']) or 'проверка актуального состояния'),f"First recorded blocker: {first['reason'] if first else 'UNKNOWN'} (временный отказ не равен окончательной отмене)",f"Terminal: {d['irreversible_terminal'] or 'не зарегистрирован'}",'Secondary/current: '+('; '.join(d['all_reasons'][1:]) or 'не записаны'),f"Score: {d['score']} | Threshold: {d['threshold']}",f"Data: {d['data_quality']} | Updated: {datetime.fromtimestamp(d['updated'],timezone.utc).isoformat()}"]
    if d.get('early'):
        lines.append(f"Early observation threshold: {getattr(cfg,'early_setup_min_score',d['threshold'])} | production entry threshold: {d['threshold']}")
    return '\n'.join(lines)[:4000]

def schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS setup_notice_state(setup_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS setup_notices(id INTEGER PRIMARY KEY,setup_id TEXT NOT NULL,transition TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,state TEXT NOT NULL,attempted REAL,message_id INTEGER,error TEXT,next_try REAL DEFAULT 0, UNIQUE(setup_id,transition))')

def stage(d,now=None):
    now=time.time() if now is None else now
    with _conn() as con:
        schema(con);con.execute('BEGIN IMMEDIATE')
        old=con.execute('SELECT payload FROM setup_notice_state WHERE setup_id=?',(d['id'],)).fetchone()
        prev=json.loads(old[0]) if old else None
        if d['state'] in TERMINAL:
            if not prev:return
            transition='INVALIDATED'
            con.execute("UPDATE setup_notices SET state='SUPERSEDED' WHERE setup_id=? AND state='PENDING'",(d['id'],))
        elif not d['qualifies']:return
        elif prev is None:transition='NEW'
        elif d['approaching'] and not prev.get('approaching'):transition='TRIGGER APPROACHING'
        elif set(d['missing'])<set(prev['missing']):transition='IMPROVED'
        else:return
        # Unique transition imposes at most four notices per setup across restarts.
        con.execute("INSERT OR IGNORE INTO setup_notices(setup_id,transition,payload,created,state) VALUES(?,?,?,?,'PENDING')",(d['id'],transition,render(d,transition),now))
        con.execute('INSERT OR REPLACE INTO setup_notice_state VALUES(?,?)',(d['id'],json.dumps(d)))

async def send_one(bot,now=None):
    if not bot or not cfg.tg_chat_id or not cfg.is_running:return
    now=time.time() if now is None else now
    with _conn() as con:
        schema(con);con.execute('BEGIN IMMEDIATE')
        con.execute("UPDATE setup_notices SET state='UNCERTAIN',error='interrupted_send' WHERE state='SENDING' AND attempted<?",(now-120,))
        if con.execute("SELECT 1 FROM setup_notices WHERE attempted>? LIMIT 1",(now-30,)).fetchone():return
        row=con.execute("SELECT * FROM setup_notices WHERE state='PENDING' AND next_try<=? ORDER BY id LIMIT 1",(now,)).fetchone()
        if not row:return
        if row['transition']!='INVALIDATED' and now-row['created']>600:
            con.execute("UPDATE setup_notices SET state='EXPIRED' WHERE id=?",(row['id'],));return
        con.execute("UPDATE setup_notices SET state='SENDING',attempted=? WHERE id=?",(now,row['id']))
    try:
        result=await bot.send_message(cfg.tg_chat_id,row['payload'])
        with _conn() as con:con.execute("UPDATE setup_notices SET state='SENT',message_id=? WHERE id=?",(result.message_id,row['id']))
    except Exception as exc:
        retry=float(getattr(exc,'retry_after',0) or 0)
        with _conn() as con:con.execute('UPDATE setup_notices SET state=?,error=?,next_try=? WHERE id=?',('PENDING' if retry else 'UNCERTAIN',type(exc).__name__,now+max(30,retry),row['id']))

async def visibility_worker(bot):
    from trading.telemetry import heartbeat
    while True:
        try:
            if cfg.is_running:
                for last in latest_events(1000):
                    d=diagnostic(history(last['setup_id']));stage(d)
                await send_one(bot)
            heartbeat('setup_visibility')
        except Exception as exc:heartbeat('setup_visibility',type(exc).__name__)
        await asyncio.sleep(30)
