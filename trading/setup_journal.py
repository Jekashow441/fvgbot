"""Append-only setup observations. Original proposals are never rewritten."""
import json
import time
from core.database import _conn
from trading.zone_lifecycle import invalidation_reason


def setup_id(symbol, timeframe, zone):
    return f"{symbol}_{timeframe}_{zone['candle_time']}_{zone['type']}"


def _schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS setup_events (id INTEGER PRIMARY KEY, setup_id TEXT NOT NULL, observed_at REAL NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL)')
    con.execute('CREATE INDEX IF NOT EXISTS setup_events_key ON setup_events(setup_id,id)')
    # One row per setup: the latest re-observation that did not change the substance.
    con.execute('CREATE TABLE IF NOT EXISTS setup_heartbeat (setup_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, analysis_bar INTEGER, current_price REAL)')


# Fields that change on every scan without changing what the setup is.
VOLATILE = ('current_price', 'analysis_bar')


def _substance(assessment):
    return {k: v for k, v in (assessment or {}).items() if k not in VOLATILE}


def _overlay(con, rows):
    """Latest event of each setup carries the freshest re-observation time and price."""
    out = []
    for r in rows:
        row = {**dict(r), 'payload': json.loads(r['payload'])}
        hb = con.execute('SELECT observed_at,analysis_bar,current_price FROM setup_heartbeat WHERE setup_id=?', (row['setup_id'],)).fetchone()
        if hb and hb['observed_at'] > row['observed_at']:
            row['observed_at'] = hb['observed_at']
            assessment = row['payload'].get('assessment')
            if isinstance(assessment, dict):
                assessment.update({k: hb[k] for k in VOLATILE if hb[k] is not None})
            if hb['current_price'] is not None:
                row['payload']['current_price'] = hb['current_price']
        out.append(row)
    return out


def record(key,status,reason,payload,now=None):
    now=time.time() if now is None else now
    with _conn() as con:
        _schema(con)
        last=con.execute('SELECT status,reason,payload FROM setup_events WHERE setup_id=? ORDER BY id DESC LIMIT 1',(key,)).fetchone()
        if last and last['status'] in ('INVALIDATED','EXPIRED','MISSED','TP HIT','SL HIT'):
            return
        if last and last['status']=='CONFIRMED' and status in ('WATCHLIST','POTENTIAL','DEVELOPING','ENTRY APPROACHING'):
            return
        assessment = payload.get('assessment') or {}
        con.execute('INSERT INTO setup_heartbeat(setup_id,observed_at,analysis_bar,current_price) VALUES(?,?,?,?) '
                    'ON CONFLICT(setup_id) DO UPDATE SET observed_at=excluded.observed_at,analysis_bar=excluded.analysis_bar,current_price=excluded.current_price',
                    (key, now, assessment.get('analysis_bar'), assessment.get('current_price', payload.get('current_price'))))
        if last and last['status']==status and last['reason']==reason and _substance(json.loads(last['payload']).get('assessment'))==_substance(assessment):
            return
        con.execute('INSERT INTO setup_events(setup_id,observed_at,status,reason,payload) VALUES(?,?,?,?,?)',
                    (key,now,status,reason,json.dumps(payload,allow_nan=False)))


def latest_events(limit=100, prefix=None):
    with _conn() as con:
        _schema(con)
        if prefix is None:
            rows=con.execute('SELECT * FROM setup_events WHERE id IN (SELECT MAX(id) FROM setup_events GROUP BY setup_id) ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        else:
            rows=con.execute('SELECT * FROM setup_events WHERE id IN (SELECT MAX(id) FROM setup_events WHERE setup_id>=? AND setup_id<? GROUP BY setup_id) ORDER BY id DESC LIMIT ?', (prefix,prefix+'\uffff',limit)).fetchall()
        return _overlay(con, rows)


def snapshot(symbol,timeframe,row):
    zone=row.get('fvg')
    signal=row.get('signal') or {}
    assessment=row.get('assessments',{}).get(str(zone['candle_time'])+zone['type'],{})
    return {'asset':symbol,'timeframe':timeframe,'direction':'LONG' if zone['type']=='BULLISH' else 'SHORT',
            'htf_bias':(row.get('htf_context') or {}).get('trend','UNKNOWN'),
            'market_structure':signal.get('structure',assessment.get('structure')), 'liquidity':signal.get('structure',assessment.get('structure',{})).get('sweep'),
            'fvg':zone,'entry_zone':[zone['bottom'],zone['top']],
            'fvg_quality':signal.get('score',assessment.get('score')),'confidence':None,
            'assessment':assessment,'current_price':row.get('current_price'),
            'market_at_observation':row.get('market'),
            'execution_context':{'microstructure':row.get('microstructure'),'higher_4h':(row.get('intelligence') or {}).get('higher_4h'),'distance_vwap_atr':(row.get('intelligence') or {}).get('distance_vwap_atr'),'benchmark':(row.get('intelligence') or {}).get('benchmark'),'market':row.get('market')} if assessment.get('checks',{}).get('Entry') else None,
            'evidence':(row.get('qualification') or {}).get('status','UNKNOWN'),
            'confidence_note':'INSUFFICIENT DATA: probability is not calibrated; score is heuristic.',
            'entry':signal.get('entry'),'sl':signal.get('sl'),'tp1':signal.get('tp'),'tp2':None,'rr':signal.get('rr'),
            'confirmation_required':'Closed-candle retest plus all strategy, context, price and risk gates.',
            'invalidation':'Full wick fill, prohibited closed CE breach, or expiry.',
            'strict_ce':row.get('strict_ce',True),'signal_created':row.get('signal_created'),
            'paper_opened':signal.get('paper_opened',False),'result':None}


def observe(symbol,timeframe,row,frame,settings,now=None):
    now=time.time() if now is None else now
    # Only entries for this symbol; old events stay accessible through history().
    for old in latest_events(1000, prefix=f'{symbol}_{timeframe}_'):
        payload=old['payload']
        if payload['asset']!=symbol or payload['timeframe']!=timeframe or old['status'] in ('INVALIDATED','EXPIRED','MISSED','TP HIT','SL HIT'):
            continue
        why=invalidation_reason(payload['fvg'],frame,timeframe,now*1000,payload['strict_ce'])
        lifetime=settings.signal_max_age_seconds if old['status']=='CONFIRMED' else settings.fvg_lookback*int(timeframe)*60
        start=payload.get('signal_created') or old['observed_at']
        if why:
            record(old['setup_id'],'INVALIDATED',why,payload,now)
        elif now > payload.get('expires_at',start+lifetime):
            record(old['setup_id'],'EXPIRED','observation_expired',payload,now)
        elif old['status']=='CONFIRMED' and not payload.get('paper_opened') and not paper_exists(old['setup_id']) and payload.get('entry') and payload.get('sl'):
            price=row.get('current_price')
            if price and abs(price-payload['entry'])>.25*abs(payload['entry']-payload['sl']):
                record(old['setup_id'],'MISSED','paper_entry_window_passed',payload,now)
    for zone in row.get('zones') or ([row['fvg']] if row.get('fvg') else []):
        candidate=dict(row,fvg=zone)
        if zone != row.get('fvg'):
            candidate.update(signal=None,signal_created=None)
        _observe_candidate(symbol,timeframe,candidate,frame,settings,now)


def _observe_candidate(symbol,timeframe,row,frame,settings,now):
    zone=row.get('fvg')
    if not zone:
        return
    payload=snapshot(symbol,timeframe,row)
    payload['expires_at']=(row['signal_created']+settings.signal_max_age_seconds) if row.get('signal') and row.get('signal_created') else zone['formed_at']/1000+(settings.fvg_lookback+1)*int(timeframe)*60
    key=setup_id(symbol,timeframe,zone)
    why=invalidation_reason(zone,frame,timeframe,now*1000,payload['strict_ce'])
    if why:
        record(key,'INVALIDATED',why,payload,now)
        return
    assessment=payload.get('assessment',{})
    blockers=list(dict.fromkeys(row.get('base_blockers',row.get('blockers') or [])+assessment.get('execution_rejections',[])))
    sig=row.get('signal')
    fresh=sig and 0<=now-(row.get('signal_created') or 0)<=settings.signal_max_age_seconds
    if fresh and not blockers:
        record(key,'CONFIRMED','all_entry_rules_passed',payload,now)
    elif payload['htf_bias'] == ('DOWN' if payload['direction']=='LONG' else 'UP'):
        record(key,'WATCHLIST','htf_opposition — ignore entry',payload,now)
    elif blockers:
        record(key,'WATCHLIST','; '.join(blockers),payload,now)
    elif payload.get('assessment'):
        assessment=payload['assessment']
        record(key,assessment['status'],'; '.join(assessment['rejections']) or 'Awaiting live entry gates',payload,now)
    elif payload['htf_bias'] == ('UP' if payload['direction']=='LONG' else 'DOWN'):
        record(key,'POTENTIAL','FVG and HTF align; entry confirmation not established',payload,now)
    else:
        record(key,'WATCHLIST','Context/entry confirmation incomplete',payload,now)


def history(key):
    with _conn() as con:
        _schema(con)
        rows=con.execute('SELECT * FROM setup_events WHERE setup_id=? ORDER BY id',(key,)).fetchall()
        events=[{**dict(r),'payload':json.loads(r['payload'])} for r in rows]
        if rows:
            events[-1]=_overlay(con,rows[-1:])[0]
    return events


def report(now=None):
    now=time.time() if now is None else now
    rows=latest_events()
    for row in rows:
        if row['status'] in ('CONFIRMED','POTENTIAL','WATCHLIST','DEVELOPING','ENTRY APPROACHING') and now>row['payload'].get('expires_at',row['observed_at']+300):
            row['display_status']='EXPIRED — historical observation'
        else:
            row['display_status']=row['status']
        row['category']='🔴 INVALID / IGNORE' if row['status'] in ('INVALIDATED','EXPIRED','MISSED','SL HIT') else '⚪ WATCHLIST' if row['status']=='WATCHLIST' else '🟡 POTENTIAL SETUP'
        if row['display_status'].startswith('EXPIRED'):
            row['category']='🔴 INVALID / IGNORE'
        if 'htf_opposition' in row['reason'] or row['status'] in ('TP HIT','RECOVERED CONFIRMATION'):
            row['category']='🔴 INVALID / IGNORE'
        if row['display_status']=='CONFIRMED' and row['payload'].get('evidence')=='PASS' and (row['payload'].get('fvg_quality') or 0)>=80:
            row['category']='🟢 HIGH-CONVICTION SETUP'
        # High conviction is an evidence category, not merely a high heuristic score.
        row['signal_may_form']=bool(row['payload'].get('assessment',{}).get('signal_may_form')) and not row['display_status'].startswith('EXPIRED') and row['status'] in ('DEVELOPING','POTENTIAL','ENTRY APPROACHING')
        row['note']='Historical journal observation; CONFIRMED means entry rules passed then, not a current order.'
    return rows


def paper_exists(key):
    with _conn() as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='signals'").fetchone():
            return False
        return bool(con.execute("SELECT 1 FROM signals WHERE json_extract(ml_features,'$.journal_id')=? LIMIT 1",(key,)).fetchone())


def record_trade_result(key, exit_reason, pnl_pct):
    if not key:
        return
    with _conn() as con:
        _schema(con)
        row=con.execute("SELECT payload FROM setup_events WHERE setup_id=? AND status IN ('CONFIRMED','RECOVERED CONFIRMATION') ORDER BY id LIMIT 1",(key,)).fetchone()
        if not row or con.execute("SELECT 1 FROM setup_events WHERE setup_id=? AND status IN ('TP HIT','SL HIT')",(key,)).fetchone():
            return
        payload=json.loads(row['payload'])
        payload['result']={'pnl_pct':pnl_pct,'exit_reason':exit_reason,'model':'paper point-price observations, not exchange fills'}
        con.execute('INSERT INTO setup_events(setup_id,observed_at,status,reason,payload) VALUES(?,?,?,?,?)',
                    (key,time.time(),'TP HIT' if exit_reason=='TP' else 'SL HIT','paper_position_closed',json.dumps(payload,allow_nan=False)))
