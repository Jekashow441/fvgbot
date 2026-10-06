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

REASONS_RU = {
    'scan_not_yet_completed': 'Первый проход сканера ещё не завершён',
    'scanner_not_fresh': 'Сканер не завершал проход больше 3 минут',
    'some_symbols_failed_or_stale': 'Часть монет не загрузилась (ошибки API или устаревшие данные)',
    'some_required_htf_data_unavailable': 'Не удалось получить данные старшего ТФ по части монет',
    'delivery_pending_over_120s': 'Сигнал ждёт отправки в Telegram больше 2 минут',
    'delivery_worker_unhealthy': 'Отправка в Telegram не работает',
}
# A handful of illiquid or fresh symbols failing is normal market noise, not a system fault.
WARN_FRACTION = 0.05


def system_health(now=None):
    now=time.time() if now is None else now
    with _conn() as con:
        schema(con)
        row=con.execute('SELECT completed_at,payload FROM scan_cycles ORDER BY id DESC LIMIT 1').fetchone()
        outbox_exists=con.execute("SELECT 1 FROM sqlite_master WHERE name='signal_outbox'").fetchone()
        pending=con.execute("SELECT count(*) FROM signal_outbox WHERE state='PENDING' AND created<?",(now-120,)).fetchone()[0] if outbox_exists else 0
    if not row:
        started=WORKERS.get('scanner',{}).get('observed_at',now)
        status='CRITICAL' if now-started>180 else 'WARNING'
        return {'status':status,'reason':'scan_not_yet_completed','reasons':['scan_not_yet_completed'],
                'details':[REASONS_RU['scan_not_yet_completed']],'workers':WORKERS.copy()}
    data=json.loads(row['payload']);counts=data['counts'];bad=counts.get('stale',0)+counts.get('failed',0)
    selected=max(1,data.get('selected') or sum(counts.values()))
    reasons=[];details=[];status='HEALTHY'
    def warn(code,text):
        reasons.append(code);details.append(text)
    if now-row['completed_at']>180 or counts.get('fresh',0)==0:
        status='CRITICAL';warn('scanner_not_fresh',REASONS_RU['scanner_not_fresh'])
    elif bad/selected>WARN_FRACTION:
        status='WARNING';warn('some_symbols_failed_or_stale',f"{REASONS_RU['some_symbols_failed_or_stale']}: {bad} из {selected}")
    htf_bad=data.get('htf',{}).get('unavailable',0)+data.get('higher_4h',{}).get('unavailable',0)
    if htf_bad/selected>WARN_FRACTION:
        if status=='HEALTHY':status='WARNING'
        warn('some_required_htf_data_unavailable',f"{REASONS_RU['some_required_htf_data_unavailable']}: {htf_bad}")
    if pending:status='CRITICAL';warn('delivery_pending_over_120s',REASONS_RU['delivery_pending_over_120s'])
    delivery=WORKERS.get('delivery')
    if delivery and (now-delivery['observed_at']>60 or delivery.get('error')):status='CRITICAL';warn('delivery_worker_unhealthy',REASONS_RU['delivery_worker_unhealthy'])
    notes=[]
    if bad:notes.append(f"не загрузились: {bad}")
    skipped=counts.get('skipped',0)
    if skipped:notes.append(f"пропущены (новые листинги/кулдаун): {skipped}")
    return {'status':status,'reasons':reasons,'details':details,'notes':notes,'last_completed_at':row['completed_at'],'scan':data,'pending_deliveries':pending,'workers':WORKERS.copy()}


def prune(settings, now=None):
    """Bounded history: diagnostics age out; open trades and recent setups are untouched."""
    now = time.time() if now is None else now
    removed = {}
    with _conn() as con:
        schema(con)
        removed["scan_cycles"] = con.execute("DELETE FROM scan_cycles WHERE completed_at<?", (now-settings.scan_cycle_retention_days*86400,)).rowcount
        removed["pipeline_events"] = con.execute("DELETE FROM pipeline_events WHERE observed_at<?", (now-settings.telemetry_retention_days*86400,)).rowcount
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='setup_events'").fetchone():
            cutoff = now-settings.journal_retention_days*86400
            stale = "SELECT setup_id FROM setup_events GROUP BY setup_id HAVING MAX(observed_at)<?"
            open_ids = set()
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='signals'").fetchone():
                open_ids = {r[0] for r in con.execute("SELECT json_extract(ml_features,'$.journal_id') FROM signals WHERE outcome='OPEN'") if r[0]}
            ids = [r[0] for r in con.execute(stale, (cutoff,)) if r[0] not in open_ids]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i+500]
                marks = ",".join("?"*len(chunk))
                con.execute(f"DELETE FROM setup_events WHERE setup_id IN ({marks})", chunk)
                if con.execute("SELECT 1 FROM sqlite_master WHERE name='setup_heartbeat'").fetchone():
                    con.execute(f"DELETE FROM setup_heartbeat WHERE setup_id IN ({marks})", chunk)
            removed["setups"] = len(ids)
    return removed
