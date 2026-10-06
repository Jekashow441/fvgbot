"""Persistent operational evidence, independent from strategy eligibility."""
import json
import time
import sqlite3
from collections import Counter
from core.database import _conn
from trading.logger import log_info,log_error

WORKERS={}

def schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS pipeline_events(id INTEGER PRIMARY KEY,observed_at REAL,kind TEXT,signal_id TEXT,payload TEXT)')
    con.execute('CREATE INDEX IF NOT EXISTS pipeline_events_time ON pipeline_events(observed_at)')
    con.execute('CREATE TABLE IF NOT EXISTS scan_cycles(id INTEGER PRIMARY KEY,started_at REAL,completed_at REAL,payload TEXT)')

def event(kind,signal_id=None,**facts):
    now=time.time();payload=json.dumps(facts,ensure_ascii=False,allow_nan=False)
    log_info(f'{kind} timestamp={now:.3f} SignalID={signal_id or "-"} {payload}')
    try:
        with _conn() as con:
            schema(con)
            con.execute('INSERT INTO pipeline_events(observed_at,kind,signal_id,payload) VALUES(?,?,?,?)',(now,kind,signal_id,payload))
    except sqlite3.Error as exc:
        log_error(f'TELEMETRY_WRITE_FAILED kind={kind} error={type(exc).__name__}')

def heartbeat(name,error=None):
    WORKERS[name]={'observed_at':time.time(),'error':error}

def record_cycle(started,results,selected):
    groups={name:[] for name in ('fresh','stale','failed','skipped')}
    tf=Counter();higher=Counter()
    for symbol in selected:
        r=results.get(symbol,{})
        outcome=r.get('outcome','failed');groups[outcome if outcome in groups else 'failed'].append(symbol)
        if r.get('htf'):tf[r['htf']]+=1
        higher[r.get('higher_4h','not_required')]+=1
    data={'selected':len(selected),'processed':len(selected),'counts':{k:len(v) for k,v in groups.items()},'symbols':groups,'htf':dict(tf),'higher_4h':dict(higher)}
    with _conn() as con:
        schema(con)
        con.execute('INSERT INTO scan_cycles(started_at,completed_at,payload) VALUES(?,?,?)',(started,time.time(),json.dumps(data)))
    heartbeat('scanner');return data

def system_health(now=None):
    now=time.time() if now is None else now
    with _conn() as con:
        schema(con)
        row=con.execute('SELECT completed_at,payload FROM scan_cycles ORDER BY id DESC LIMIT 1').fetchone()
        outbox_exists=con.execute("SELECT 1 FROM sqlite_master WHERE name='signal_outbox'").fetchone()
        pending=con.execute("SELECT count(*) FROM signal_outbox WHERE state='PENDING' AND created<?",(now-120,)).fetchone()[0] if outbox_exists else 0
    if not row:
        started=WORKERS.get('scanner',{}).get('observed_at',now)
        return {'status':'CRITICAL' if now-started>180 else 'WARNING','reason':'scan_not_yet_completed','workers':WORKERS.copy()}
    data=json.loads(row['payload']);counts=data['counts'];bad=counts['stale']+counts['failed']
    reasons=[];status='HEALTHY'
    if now-row['completed_at']>180 or counts['fresh']==0:status='CRITICAL';reasons.append('scanner_not_fresh')
    elif bad:status='WARNING';reasons.append('some_symbols_failed_or_stale')
    if data.get('htf',{}).get('unavailable',0) or data.get('higher_4h',{}).get('unavailable',0):
        if status=='HEALTHY':status='WARNING'
        reasons.append('some_required_htf_data_unavailable')
    if pending:status='CRITICAL';reasons.append('delivery_pending_over_120s')
    delivery=WORKERS.get('delivery')
    if delivery and (now-delivery['observed_at']>60 or delivery.get('error')):status='CRITICAL';reasons.append('delivery_worker_unhealthy')
    return {'status':status,'reasons':reasons,'last_completed_at':row['completed_at'],'scan':data,'pending_deliveries':pending,'workers':WORKERS.copy()}
