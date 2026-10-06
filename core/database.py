import os
import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager

# БД лежит в <root>/data/scalper.db (абсолютный путь — не зависит от места запуска).
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "scalper.db")


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    """Идемпотентная инициализация и миграция БД под любую версию бота.
    Безопасно вызывать сколько угодно раз: создаёт недостающие таблицы и
    добавляет недостающие колонки, не трогая существующие данные."""
    try:
        with _conn() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, entry_ts INTEGER, symbol TEXT NOT NULL,
                    side TEXT NOT NULL, score REAL NOT NULL, entry REAL NOT NULL,
                    sl REAL NOT NULL, tp REAL NOT NULL, rsi REAL, trend TEXT,
                    factors TEXT, outcome TEXT DEFAULT 'OPEN', exit_price REAL,
                    pnl_pct REAL, closed_ts TEXT
                )
            """)
            con.execute("CREATE TABLE IF NOT EXISTS weights (factor TEXT PRIMARY KEY, weight REAL NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL NOT NULL)")
            # Текстовые настройки (например, JSON уроков из post-mortem).
            con.execute("CREATE TABLE IF NOT EXISTS settings_text (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            # История качества ML-модели во времени (для графика обучаемости).
            con.execute("""
                CREATE TABLE IF NOT EXISTS ml_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, n INTEGER, auc REAL, acc REAL, kind TEXT
                )
            """)
            # Память проигрышных/выигрышных паттернов: бот учится на конкретных ошибках.
            con.execute("""
                CREATE TABLE IF NOT EXISTS loss_patterns (
                    signature TEXT PRIMARY KEY,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    net_pnl REAL NOT NULL DEFAULT 0,
                    updated_ts TEXT
                )
            """)
            # Теневые («бумажные») сделки — кандидаты, что НЕ прошли фильтр качества.
            # Они НЕ показываются как реальные сигналы и не влияют на статистику/PnL,
            # но их исход симулируется и идёт в обучение ML — чтобы бот учился даже
            # тогда, когда живых сигналов нет (решает «днями нет сигналов = нет учёбы»).
            con.execute("""
                CREATE TABLE IF NOT EXISTS shadow_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, entry_ts INTEGER, symbol TEXT NOT NULL,
                    side TEXT NOT NULL, score REAL, entry REAL NOT NULL,
                    sl REAL NOT NULL, tp REAL NOT NULL, rsi REAL, trend TEXT,
                    htf TEXT, adx REAL, factors TEXT, signature TEXT,
                    ml_features TEXT, ml_prob REAL,
                    outcome TEXT DEFAULT 'OPEN', exit_price REAL, pnl_pct REAL,
                    closed_ts TEXT, exit_reason TEXT
                )
            """)
            # Полный набор колонок signals для ВСЕХ версий бота. Если БД старая —
            # недостающие колонки добавляются; новая БД получает их сразу.
            existing = {row["name"] for row in con.execute("PRAGMA table_info(signals)")}
            new_cols = {"entry_ts": "INTEGER", "factors": "TEXT", "pnl_pct": "REAL",
                        "exit_price": "REAL", "closed_ts": "TEXT", "trend": "TEXT",
                        "rsi": "REAL", "htf": "TEXT", "signature": "TEXT", "adx": "REAL",
                        "ml_features": "TEXT", "ml_prob": "REAL",
                        "loss_tags": "TEXT", "loss_notes": "TEXT", "exit_reason": "TEXT",
                        "initial_risk": "REAL", "rr": "REAL", "signal_id": "TEXT", "closed_at_ms": "INTEGER"}
            for col, t in new_cols.items():
                if col not in existing:
                    try:
                        con.execute(f"ALTER TABLE signals ADD COLUMN {col} {t}")
                    except sqlite3.OperationalError as e:
                        print(f"[DB] миграция колонки {col}: {e}")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_generated_trade ON signals(signal_id) WHERE signal_id IS NOT NULL")
            con.execute("CREATE TABLE IF NOT EXISTS paper_level_events(id INTEGER PRIMARY KEY,trade_id INTEGER NOT NULL,observed_at REAL NOT NULL,sl REAL,tp REAL,reason TEXT NOT NULL)")
        print("[DB] схема готова (совместима со всеми версиями)")
    except sqlite3.Error as e:
        print(f"[DB] ошибка инициализации: {e}")


def log_signal(sig):
    with _conn() as con:
        con.execute('BEGIN IMMEDIATE')
        if sig.get('max_active_limit') is not None:
            if con.execute("SELECT COUNT(*) FROM signals WHERE outcome='OPEN'").fetchone()[0] >= sig['max_active_limit'] or con.execute("SELECT 1 FROM signals WHERE outcome='OPEN' AND symbol=?",(sig['symbol'],)).fetchone():
                return None
        cur = con.execute(
            """INSERT OR IGNORE INTO signals
               (ts, entry_ts, symbol, side, score, entry, sl, tp, rsi, trend, htf, adx,
                factors, signature, ml_features, ml_prob, initial_risk, rr, signal_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), sig["entry_ts"], sig["symbol"], sig["side"],
             sig["score"], sig["entry"], sig["sl"], sig["tp"], sig["rsi"], sig["trend"],
             sig.get("htf"), sig.get("adx"), json.dumps(sig.get("factors", [])),
             sig.get("signature"),
             json.dumps(sig.get("ml_features")) if sig.get("ml_features") is not None else None,
             sig.get("ml_prob"), sig.get("initial_risk"), sig.get("rr"), sig.get("signal_id")))
        if cur.rowcount == 0:
            return None
        trade_id=cur.lastrowid
        import time
        con.execute("INSERT INTO paper_level_events(trade_id,observed_at,sl,tp,reason) VALUES(?,?,?,?,?)",(trade_id,time.time(),sig['sl'],sig['tp'],'INITIAL'))
        return trade_id


def close_signal(signal_id, outcome, exit_price, pnl_pct, exit_reason=None):
    with _conn() as con:
        con.execute("UPDATE signals SET outcome=?, exit_price=?, pnl_pct=?, closed_ts=?, exit_reason=? WHERE id=?",
                    (outcome, exit_price, pnl_pct, datetime.now().isoformat(), exit_reason, signal_id))


def update_signal_levels(signal_id, sl=None, tp=None, reason="LEVEL_UPDATE"):
    """Обновляет уровни активной paper-сделки, например после breakeven/trailing.
    Возвращает True, если сделка найдена и обновлена."""
    fields, values = [], []
    if sl is not None:
        fields.append("sl=?")
        values.append(sl)
    if tp is not None:
        fields.append("tp=?")
        values.append(tp)
    if not fields:
        return False
    values.append(signal_id)
    with _conn() as con:
        cur = con.execute(f"UPDATE signals SET {', '.join(fields)} WHERE id=? AND outcome='OPEN'", values)
        if cur.rowcount > 0:
            import time
            levels=con.execute('SELECT sl,tp FROM signals WHERE id=?',(signal_id,)).fetchone()
            con.execute('INSERT INTO paper_level_events(trade_id,observed_at,sl,tp,reason) VALUES(?,?,?,?,?)',(signal_id,time.time(),levels['sl'],levels['tp'],reason))
        return cur.rowcount > 0


def get_signal(signal_id):
    with _conn() as con:
        row = con.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    return dict(row) if row else None


def get_open_signals():
    with _conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM signals WHERE outcome='OPEN'").fetchall()]


def get_recent_signals(limit=15):
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def get_active_signals():
    """Активные (ещё открытые) сигналы — для раздела «Активные сигналы».
    Самые свежие сверху."""
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, ts, symbol, side, score, entry, sl, tp, rsi, trend, htf, "
            "adx, ml_prob, factors, initial_risk, rr, ml_features FROM signals WHERE outcome='OPEN' "
            "ORDER BY id DESC").fetchall()]


def get_history_signals(limit=100):
    """Закрытые сигналы (WIN/LOSS) — для раздела «История сигналов».
    Самые свежие сверху."""
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, ts, closed_ts, symbol, side, score, entry, sl, tp, "
            "exit_price, pnl_pct, outcome, exit_reason, ml_prob, loss_tags "
            "FROM signals WHERE outcome IN ('WIN','LOSS') "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def get_closed_signals():
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT factors, outcome, pnl_pct, score, signature, ml_features FROM signals "
            "WHERE outcome IN ('WIN','LOSS')").fetchall()]


def get_closed_signals_full():
    """Полные строки закрытых сделок для обучения ML — РЕАЛЬНЫЕ + ТЕНЕВЫЕ вместе,
    в хронологическом порядке (по entry_ts) — recency-веса требуют верного порядка.
    Теневые сделки дают модели данные даже когда живых сигналов нет."""
    cols = ("side, factors, outcome, pnl_pct, score, signature, rsi, adx, "
            "trend, htf, ml_features, COALESCE(entry_ts,0) AS _ord")
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            f"SELECT {cols} FROM signals WHERE outcome IN ('WIN','LOSS') "
            f"UNION ALL "
            f"SELECT {cols} FROM shadow_signals WHERE outcome IN ('WIN','LOSS') "
            f"ORDER BY _ord ASC").fetchall()]
    for r in rows:
        r.pop("_ord", None)
    return rows


def log_shadow(sig):
    """Записывает теневого кандидата (не прошёл фильтр — для обучения, не для торговли)."""
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO shadow_signals
               (ts, entry_ts, symbol, side, score, entry, sl, tp, rsi, trend, htf, adx, factors, signature, ml_features, ml_prob)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), sig.get("entry_ts"), sig["symbol"], sig["side"],
             sig.get("score"), sig["entry"], sig["sl"], sig["tp"], sig.get("rsi"), sig.get("trend"),
             sig.get("htf"), sig.get("adx"), json.dumps(sig.get("factors", [])),
             sig.get("signature"),
             json.dumps(sig.get("ml_features")) if sig.get("ml_features") is not None else None,
             sig.get("ml_prob")))
        return cur.lastrowid


def get_open_shadows():
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM shadow_signals WHERE outcome='OPEN'").fetchall()]


def close_shadow(shadow_id, outcome, exit_price, pnl_pct, exit_reason=None):
    with _conn() as con:
        con.execute("UPDATE shadow_signals SET outcome=?, exit_price=?, pnl_pct=?, "
                    "closed_ts=?, exit_reason=? WHERE id=?",
                    (outcome, exit_price, pnl_pct, datetime.now().isoformat(), exit_reason, shadow_id))


def shadow_counts():
    """Сводка по теневым сделкам для дашборда: всего и закрытых."""
    with _conn() as con:
        total = con.execute("SELECT COUNT(*) c FROM shadow_signals").fetchone()["c"]
        closed = con.execute("SELECT COUNT(*) c FROM shadow_signals "
                             "WHERE outcome IN ('WIN','LOSS')").fetchone()["c"]
    return {"total": total, "closed": closed}


def shadow_stats():
    """Полная статистика теневых сделок: винрейт, PnL, разбивка по типу выхода
    и по стороне — видно, чему учится модель на отклонённых кандидатах."""
    with _conn() as con:
        by_out = {r["outcome"]: r["c"] for r in con.execute(
            "SELECT outcome, COUNT(*) c FROM shadow_signals GROUP BY outcome").fetchall()}
        agg = con.execute(
            "SELECT COUNT(*) c, SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) wins, "
            "COALESCE(SUM(pnl_pct),0) net, COALESCE(AVG(pnl_pct),0) avg "
            "FROM shadow_signals WHERE outcome IN ('WIN','LOSS')").fetchone()
        exits = con.execute(
            "SELECT COALESCE(exit_reason,'unknown') reason, COUNT(*) c, "
            "SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) wins "
            "FROM shadow_signals WHERE outcome IN ('WIN','LOSS') "
            "GROUP BY COALESCE(exit_reason,'unknown')").fetchall()
        sides = con.execute(
            "SELECT side, COUNT(*) c, SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) wins "
            "FROM shadow_signals WHERE outcome IN ('WIN','LOSS') GROUP BY side").fetchall()
    closed = agg["c"] or 0
    wins = agg["wins"] or 0
    return {
        "total": sum(by_out.values()),
        "open": by_out.get("OPEN", 0),
        "closed": closed,
        "wins": wins, "losses": closed - wins,
        "win_rate": round(wins / closed * 100, 1) if closed else 0.0,
        "net_pnl": round(agg["net"], 2),
        "avg_pnl": round(agg["avg"], 3),
        "exits": sorted([{"reason": e["reason"], "count": e["c"], "wins": e["wins"],
                          "win_rate": round(e["wins"] / e["c"] * 100, 1) if e["c"] else 0.0}
                         for e in exits], key=lambda x: -x["count"]),
        "sides": [{"side": s["side"], "count": s["c"], "wins": s["wins"],
                   "win_rate": round(s["wins"] / s["c"] * 100, 1) if s["c"] else 0.0}
                  for s in sides],
    }


def last_loss_time(symbol):
    with _conn() as con:
        row = con.execute("SELECT closed_ts FROM signals WHERE symbol=? AND outcome='LOSS' "
                          "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
    return row["closed_ts"] if row else None


def last_signal_time():
    """Время последнего залогированного сигнала (любого) — для индикатора «бот жив»."""
    with _conn() as con:
        row = con.execute("SELECT ts FROM signals ORDER BY id DESC LIMIT 1").fetchone()
    return row["ts"] if row else None


def save_weights(weights):
    with _conn() as con:
        for f, w in weights.items():
            con.execute("INSERT INTO weights (factor, weight) VALUES (?, ?) "
                        "ON CONFLICT(factor) DO UPDATE SET weight=excluded.weight", (f, w))


def load_weights():
    with _conn() as con:
        return {r["factor"]: r["weight"] for r in con.execute("SELECT * FROM weights").fetchall()}


def set_setting(key, value):
    with _conn() as con:
        con.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_setting(key, default):
    with _conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting_text(key, value):
    with _conn() as con:
        con.execute("INSERT INTO settings_text (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_setting_text(key, default):
    with _conn() as con:
        row = con.execute("SELECT value FROM settings_text WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def record_ml_snapshot(n, auc, acc, kind):
    """Сохраняет точку качества модели (раз в обучающий цикл) — для графика обучаемости.
    Записываем только при изменении n, чтобы не плодить дубли."""
    with _conn() as con:
        last = con.execute("SELECT n FROM ml_history ORDER BY id DESC LIMIT 1").fetchone()
        if last and last["n"] == n:
            return
        con.execute("INSERT INTO ml_history (ts, n, auc, acc, kind) VALUES (?, ?, ?, ?, ?)",
                    (datetime.now().isoformat(), n, auc, acc, kind))
        # Держим не более 500 точек.
        con.execute("DELETE FROM ml_history WHERE id NOT IN "
                    "(SELECT id FROM ml_history ORDER BY id DESC LIMIT 500)")


def get_ml_history(limit=120):
    with _conn() as con:
        rows = con.execute("SELECT ts, n, auc, acc, kind FROM ml_history "
                           "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def set_loss_diagnosis(signal_id, tags_json, notes):
    with _conn() as con:
        con.execute("UPDATE signals SET loss_tags=?, loss_notes=? WHERE id=?",
                    (tags_json, notes, signal_id))


def iter_loss_tags():
    with _conn() as con:
        return [r["loss_tags"] for r in con.execute(
            "SELECT loss_tags FROM signals WHERE outcome='LOSS' AND loss_tags IS NOT NULL").fetchall()]


def get_recent_losses(limit=10):
    """Последние убыточные сделки с разбором (для панели «работа над ошибками»)."""
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT ts, closed_ts, symbol, side, score, entry, sl, tp, rsi, trend, htf, "
            "pnl_pct, ml_prob, loss_tags, loss_notes FROM signals "
            "WHERE outcome='LOSS' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def get_equity_curve():
    """Кумулятивный PnL по закрытым сделкам в хронологическом порядке (для графика)."""
    with _conn() as con:
        rows = con.execute(
            "SELECT closed_ts, ts, pnl_pct FROM signals WHERE outcome IN ('WIN','LOSS') "
            "AND pnl_pct IS NOT NULL ORDER BY id ASC").fetchall()
    curve, cum = [], 0.0
    for i, r in enumerate(rows, 1):
        cum += r["pnl_pct"]
        curve.append({"n": i, "ts": (r["closed_ts"] or r["ts"] or "")[11:19],
                      "pnl": round(r["pnl_pct"], 3), "cum": round(cum, 3)})
    return curve


def get_closed_today(today_iso):
    """Закрытые сделки за сегодня (по дате закрытия) — для дневного риск-лимита."""
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT outcome, pnl_pct, ml_features FROM signals WHERE outcome IN ('WIN','LOSS') "
            "AND substr(COALESCE(closed_ts, ts),1,10)=?", (today_iso,)).fetchall()]


def current_loss_streak():
    """Сколько последних закрытых сделок подряд были LOSS."""
    with _conn() as con:
        rows = con.execute(
            "SELECT outcome FROM signals WHERE outcome IN ('WIN','LOSS') "
            "ORDER BY closed_ts DESC, id DESC LIMIT 50").fetchall()
    streak = 0
    for r in rows:
        if r["outcome"] == "LOSS":
            streak += 1
        else:
            break
    return streak


def last_any_loss_time():
    with _conn() as con:
        row = con.execute("SELECT closed_ts FROM signals WHERE outcome='LOSS' "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    return row["closed_ts"] if row else None


def get_closed_with_hour():
    """Закрытые сделки с временем закрытия — для статистики по часам суток."""
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT outcome, closed_ts, ts FROM signals WHERE outcome IN ('WIN','LOSS')").fetchall()]


def recent_winrate(n=25):
    """Винрейт последних n закрытых сделок (для адаптивного сайзинга)."""
    with _conn() as con:
        rows = con.execute("SELECT outcome FROM signals WHERE outcome IN ('WIN','LOSS') "
                           "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        return None
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    return round(wins / len(rows), 3)


def rebuild_pattern_memory():
    """Полностью пересобирает таблицу loss_patterns из закрытых сделок.
    Каждая сигнатура агрегирует wins/losses/net_pnl -> бот помнит свои ошибки."""
    with _conn() as con:
        rows = con.execute(
            "SELECT signature, outcome, COALESCE(pnl_pct,0) p FROM signals "
            "WHERE outcome IN ('WIN','LOSS') AND signature IS NOT NULL").fetchall()
        agg = {}
        for r in rows:
            sig = r["signature"]
            a = agg.setdefault(sig, {"wins": 0, "losses": 0, "net": 0.0})
            if r["outcome"] == "WIN":
                a["wins"] += 1
            else:
                a["losses"] += 1
            a["net"] += r["p"]
        con.execute("DELETE FROM loss_patterns")
        now = datetime.now().isoformat()
        for sig, a in agg.items():
            con.execute(
                "INSERT INTO loss_patterns (signature, wins, losses, net_pnl, updated_ts) "
                "VALUES (?, ?, ?, ?, ?)", (sig, a["wins"], a["losses"], a["net"], now))


def get_pattern_stats(signature):
    """Возвращает {'wins','losses','net_pnl'} для сигнатуры или None."""
    with _conn() as con:
        row = con.execute("SELECT wins, losses, net_pnl FROM loss_patterns WHERE signature=?",
                          (signature,)).fetchone()
    return dict(row) if row else None


def load_pattern_memory():
    with _conn() as con:
        return {r["signature"]: {"wins": r["wins"], "losses": r["losses"], "net_pnl": r["net_pnl"]}
                for r in con.execute("SELECT * FROM loss_patterns").fetchall()}


def get_exit_breakdown():
    """Сводка закрытых сделок по типу выхода (TP / трейл / б/у / стоп / тайм).
    Для каждого типа: число сделок, винрейт и суммарный PnL — чтобы видеть,
    какой механизм выхода приносит прибыль, а какой сливает."""
    with _conn() as con:
        rows = con.execute(
            "SELECT COALESCE(exit_reason,'unknown') reason, COUNT(*) c, "
            "SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) wins, "
            "COALESCE(SUM(pnl_pct),0) pnl "
            "FROM signals WHERE outcome IN ('WIN','LOSS') "
            "GROUP BY COALESCE(exit_reason,'unknown')").fetchall()
    out = []
    for r in rows:
        c = r["c"]
        out.append({"reason": r["reason"], "count": c, "wins": r["wins"],
                    "losses": c - r["wins"],
                    "win_rate": round(r["wins"] / c * 100, 1) if c else 0.0,
                    "pnl": round(r["pnl"], 2)})
    out.sort(key=lambda x: -x["count"])
    return out


def get_stats():
    with _conn() as con:
        rows = con.execute("SELECT outcome, COUNT(*) c FROM signals GROUP BY outcome").fetchall()
        pnl_row = con.execute("SELECT COALESCE(SUM(pnl_pct),0) net, COALESCE(AVG(pnl_pct),0) avg "
                              "FROM signals WHERE outcome IN ('WIN','LOSS')").fetchone()
        # Profit factor: сумма прибылей / сумма убытков (по знаку PnL после комиссий).
        pf_row = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN pnl_pct>0 THEN pnl_pct END),0) gross_win, "
            "COALESCE(-SUM(CASE WHEN pnl_pct<=0 THEN pnl_pct END),0) gross_loss "
            "FROM signals WHERE outcome IN ('WIN','LOSS')").fetchone()
    stats = {r["outcome"]: r["c"] for r in rows}
    wins, losses = stats.get("WIN", 0), stats.get("LOSS", 0)
    closed = wins + losses
    gw, gl = pf_row["gross_win"], pf_row["gross_loss"]
    pf = round(gw / gl, 2) if gl > 0 else None
    return {"total": sum(stats.values()), "open": stats.get("OPEN", 0),
            "wins": wins, "losses": losses,
            "win_rate": round(wins / closed * 100, 1) if closed else 0.0,
            "net_pnl": round(pnl_row["net"], 2), "avg_pnl": round(pnl_row["avg"], 3),
            "profit_factor": pf}
