"""Recover links from persisted facts; never reopen trades or backdate events."""
import json
import math
import time
from core.database import _conn
from trading.setup_journal import snapshot, _schema, record_trade_result


def record_policy(settings):
    from trading.version import STRATEGY_VERSION
    from trading.research import fingerprint
    public_settings=settings.model_dump(exclude={'tg_token','tg_chat_id','paper_balance','symbols','dashboard_host','dashboard_port','news_feeds'})
    payload=json.dumps(dict(version=STRATEGY_VERSION,fingerprint=fingerprint(settings),min_score=settings.min_signal_score,min_turnover=settings.min_turnover_24h,max_spread_bps=settings.max_spread_bps,scan_all_symbols=settings.scan_all_symbols,settings=public_settings),sort_keys=True)
    with _conn() as con:
        con.execute('CREATE TABLE IF NOT EXISTS runtime_policy_events(id INTEGER PRIMARY KEY,observed_at REAL NOT NULL,payload TEXT NOT NULL)')
        last=con.execute('SELECT payload FROM runtime_policy_events ORDER BY id DESC LIMIT 1').fetchone()
        if not last or last[0]!=payload:
            con.execute('INSERT INTO runtime_policy_events(observed_at,payload) VALUES(?,?)',(time.time(),payload))


def reconcile():
    recovered=0
    verified_trade_ids=set()
    with _conn() as con:
        _schema(con)
        outbox=[dict(r) for r in con.execute('SELECT signal_id,created,expires,payload FROM signal_outbox')] if con.execute("SELECT 1 FROM sqlite_master WHERE name='signal_outbox'").fetchone() else []
        trades=[dict(r) for r in con.execute('SELECT * FROM signals')]
    for item in outbox:
        raw=json.loads(item['payload']);signal=raw['signal'];key=signal.get('journal_id')
        if not key or not signal.get('fvg'):
            continue # Before instrumentation: cannot fabricate a missing FVG.
        with _conn() as con:
            found=con.execute("SELECT 1 FROM setup_events WHERE setup_id=? AND status IN ('CONFIRMED','RECOVERED CONFIRMATION')",(key,)).fetchone()
            if not found:
                row={'fvg':signal['fvg'],'signal':signal,'signal_created':item['created'],'htf_context':{'trend':signal.get('htf','UNKNOWN')},'strict_ce':signal.get('strict_ce',True)}
                payload=snapshot(raw['symbol'],signal.get('timeframe','5'),row)
                payload.update(source_observed_at=item['created'],recovered_at=time.time(),source_signal_id=item['signal_id'],expires_at=item['expires'],recovery=True)
                con.execute('INSERT INTO setup_events(setup_id,observed_at,status,reason,payload) VALUES(?,?,?,?,?)',(key,time.time(),'RECOVERED CONFIRMATION','Recovered from immutable generated/outbox payload; original journal event was missing',json.dumps(payload,allow_nan=False)))
                recovered+=1
            for trade in trades:
                f=json.loads(trade.get('ml_features') or '{}')
                if f.get('journal_id')!=key:
                    continue
                if trade['symbol']!=raw['symbol'] or trade['side']!=signal['signal']:
                    continue
                original_entry=f.get('signal_entry',trade['entry'])
                original_sl=f.get('initial_sl',trade['entry']-(trade.get('initial_risk') or 0)*(1 if trade['side']=='LONG' else -1))
                if not all(math.isclose(float(a),float(b),rel_tol=1e-8,abs_tol=1e-10) for a,b in ((original_entry,signal['entry']),(original_sl,signal['sl']),(f.get('initial_tp',trade['tp']),signal['tp']))):
                    continue
                verified_trade_ids.add(trade['id'])
                if not trade.get('signal_id'):
                    # Link metadata only; do not rewrite the trade or original event.
                    con.execute('UPDATE OR IGNORE signals SET signal_id=? WHERE id=? AND signal_id IS NULL',(item['signal_id'],trade['id']))
    for trade in trades:
        if trade['outcome'] not in ('WIN','LOSS') or trade['id'] not in verified_trade_ids:
            continue
        f=json.loads(trade.get('ml_features') or '{}')
        record_trade_result(f.get('journal_id'),trade.get('exit_reason'),trade['pnl_pct'])
    return recovered
