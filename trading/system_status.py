"""Durable, clearly non-trading diagnostics when the signal stream is quiet."""
import asyncio
import json
import time
from collections import Counter,defaultdict
from core.database import _conn
from core.settings import cfg
from trading.telemetry import system_health,event,heartbeat

def snapshot(since,now=None):
    now=time.time() if now is None else now
    with _conn() as con:
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        events=[dict(r) for r in con.execute('SELECT * FROM setup_events WHERE observed_at>=? ORDER BY id',(since,))] if 'setup_events' in tables else []
        outbox=[dict(r) for r in con.execute('SELECT state FROM signal_outbox WHERE created>=?',(since,))] if 'signal_outbox' in tables else []
    seen=defaultdict(set);reasons=defaultdict(set);latest={};highest=None
    for e in events:
        p=json.loads(e['payload']);e['payload']=p;seen[e['status']].add(e['setup_id']);latest[e['setup_id']]=e
        score=p.get('fvg_quality')
        if score is not None:highest=score if highest is None else max(highest,score)
        for reason in e['reason'].split('; '):reasons[reason].add(e['setup_id'])
    candidates=[r for r in latest.values() if r['status'] in ('POTENTIAL','DEVELOPING','ENTRY APPROACHING','WATCHLIST') and r['observed_at']>=now-600 and r['payload'].get('expires_at',0)>now and r['payload'].get('assessment',{}).get('checks',{}).get('Zone eligibility',True)]
    candidates.sort(key=lambda r:(r['status']=='ENTRY APPROACHING',r['status']=='POTENTIAL',r['status']=='DEVELOPING',r['payload'].get('fvg_quality') or 0),reverse=True)
    return {'since':since,'as_of':now,'health':system_health(now),'detected':len(latest),'stages':{k:len(v) for k,v in seen.items()},'highest_score':highest,'queued':len(outbox),'sent':sum(r['state']=='SENT' for r in outbox),'main_blockers':sorted(((k,len(v)) for k,v in reasons.items()),key=lambda x:-x[1])[:5],'top_candidates':candidates[:3]}

def format_status(data):
    h=data['health'];scan=h.get('scan',{});counts=scan.get('counts',{})
    lines=['SYSTEM / MARKET STATUS — НЕ ТОРГОВЫЙ СИГНАЛ',h['status'],f"Окно: последние {(data['as_of']-data['since'])/3600:.1f} ч; Score threshold сейчас {cfg.min_signal_score}.",f"Последний завершённый проход: выбрано {scan.get('selected','UNKNOWN')}; свежие {counts.get('fresh','UNKNOWN')}; stale {counts.get('stale','UNKNOWN')}; failed {counts.get('failed','UNKNOWN')}; skipped {counts.get('skipped','UNKNOWN')}.",f"За окно: {data['detected']} сетапов; Potential {data['stages'].get('POTENTIAL',0)}; Developing {data['stages'].get('DEVELOPING',0)}; Entry Approaching {data['stages'].get('ENTRY APPROACHING',0)}; Confirmed {data['stages'].get('CONFIRMED',0)}.",f"Queued {data['queued']}; SENT/API accepted {data['sent']}; highest observed Score {data['highest_score']}.",f"Основные причины: {data['main_blockers']}"]
    for e in data['top_candidates']:
        p=e['payload'];a=p.get('assessment',{});z=p['fvg']
        lines += [f"{p['asset']} {p['direction']} — Score {p.get('fvg_quality')}, {e['status']}",f"Already satisfied: {[k for k,v in a.get('checks',{}).items() if v]}",f"Missing: {e['reason']}",f"Confirmation: закрытый ретест FVG [{z['bottom']:.8g}, {z['top']:.8g}], направленное закрытие относительно CE {z['ce']:.8g} и все обязательные фильтры.",f"Invalidation: полное заполнение, запрещённое закрытие за CE или истечение срока."]
    lines.insert(4,f"HTF: {scan.get('htf',{})}; 4h: {scan.get('higher_4h',{})}; system reasons: {h.get('reasons',[])}")
    return '\n'.join(lines)[:4000]

async def maybe_send(bot,now=None):
    if not bot or not cfg.is_running or not cfg.enable_system_summary or not cfg.tg_chat_id:return
    now=time.time() if now is None else now
    with _conn() as con:
        con.execute('CREATE TABLE IF NOT EXISTS system_reports(id INTEGER PRIMARY KEY,created REAL,payload TEXT,state TEXT,next_try REAL DEFAULT 0,attempts INTEGER DEFAULT 0,message_id INTEGER,sent_at REAL,error TEXT)')
        last=con.execute('SELECT * FROM system_reports ORDER BY id DESC LIMIT 1').fetchone()
    if last and last['state']=='PENDING':
        if last['next_try']>now:return
        report_id=last['id'];text=last['payload']
    else:
        if last and now-last['created']<cfg.system_summary_seconds:return
        data=snapshot(now-cfg.system_summary_seconds,now)
        if not data['health'].get('scan') and data['health']['status']!='CRITICAL':return
        if data['stages'].get('CONFIRMED',0)>0 and data['health']['status']!='CRITICAL':return
        text=format_status(data)
        with _conn() as con:
            report_id=con.execute("INSERT INTO system_reports(created,payload,state) VALUES(?,?,'PENDING')",(now,text)).lastrowid
    try:
        result=await bot.send_message(cfg.tg_chat_id,text)
        with _conn() as con:
            con.execute("UPDATE system_reports SET state='SENT',message_id=?,sent_at=?,attempts=attempts+1,error=NULL WHERE id=?",(result.message_id,time.time(),report_id))
        event('SYSTEM_STATUS_SENT',report_id=report_id,message_id=result.message_id)
    except Exception as exc:
        with _conn() as con:
            con.execute('UPDATE system_reports SET next_try=?,attempts=attempts+1,error=? WHERE id=?',(now+max(60,float(getattr(exc,'retry_after',0))),type(exc).__name__,report_id))
        event('SYSTEM_STATUS_DELIVERY_FAILED',report_id=report_id,error=type(exc).__name__)

async def status_worker(bot):
    while True:
        try:
            heartbeat('system_status');await maybe_send(bot)
        except Exception as exc:
            heartbeat('system_status',type(exc).__name__);event('SYSTEM_STATUS_WORKER_FAILED',error=type(exc).__name__)
        await asyncio.sleep(60)
