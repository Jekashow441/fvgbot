"""SQLite is authoritative: settlement and account mutation commit together."""
import json
import math
import time
from datetime import datetime
from core.database import _conn


def schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS paper_account (id INTEGER PRIMARY KEY CHECK(id=1),balance REAL NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS paper_cashflows (id INTEGER PRIMARY KEY,trade_id INTEGER UNIQUE,observed_at REAL NOT NULL,amount REAL NOT NULL,balance_after REAL NOT NULL,kind TEXT NOT NULL)')


def balance(seed=None):
    with _conn() as con:
        schema(con)
        if seed is not None:
            con.execute('INSERT OR IGNORE INTO paper_account(id,balance) VALUES(1,?)',(seed,))
        row=con.execute('SELECT balance FROM paper_account WHERE id=1').fetchone()
        return row[0] if row else None


def reset_balance(value):
    if not math.isfinite(value) or value<=0:
        raise ValueError('Invalid account balance')
    with _conn() as con:
        schema(con);con.execute('BEGIN IMMEDIATE')
        old=con.execute('SELECT balance FROM paper_account WHERE id=1').fetchone()
        con.execute('INSERT INTO paper_account(id,balance) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET balance=excluded.balance',(value,))
        con.execute("INSERT INTO paper_cashflows(observed_at,amount,balance_after,kind) VALUES(?,?,?,'MANUAL_RESET')",(time.time(),value-(old[0] if old else value),value))


def settle(trade_id,outcome,exit_price,pnl_pct,exit_reason,seed):
    if outcome not in ('WIN','LOSS') or not all(math.isfinite(x) for x in (exit_price,pnl_pct,seed)) or exit_price<=0:
        raise ValueError('Invalid settlement')
    with _conn() as con:
        schema(con);con.execute('BEGIN IMMEDIATE')
        row=con.execute("SELECT ml_features FROM signals WHERE id=? AND outcome='OPEN'",(trade_id,)).fetchone()
        if not row:
            return None
        features=json.loads(row['ml_features'] or '{}')
        size=float(features['position_size_usdt'])
        if not math.isfinite(size) or size<0:
            raise ValueError('Unknown position allocation')
        con.execute('INSERT OR IGNORE INTO paper_account(id,balance) VALUES(1,?)',(seed,))
        current=con.execute('SELECT balance FROM paper_account WHERE id=1').fetchone()[0]
        amount=size*pnl_pct/100; new=current+amount
        con.execute("UPDATE signals SET outcome=?,exit_price=?,pnl_pct=?,closed_ts=?,closed_at_ms=?,exit_reason=? WHERE id=? AND outcome='OPEN'",(outcome,exit_price,pnl_pct,datetime.now().isoformat(),int(time.time()*1000),exit_reason,trade_id))
        con.execute('UPDATE paper_account SET balance=? WHERE id=1',(new,))
        con.execute("INSERT INTO paper_cashflows(trade_id,observed_at,amount,balance_after,kind) VALUES(?,?,?,?,'SETTLEMENT')",(trade_id,time.time(),amount,new))
        return amount,new
