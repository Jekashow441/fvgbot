"""Per-coin historical qualification; no automatic parameter optimization."""
import asyncio
import hashlib
import json
import time
from pathlib import Path
import numpy as np
import pandas as pd
from core.settings import cfg, DATA_DIR
from core.bybit import get_history
from trading.context import closed_candles
from trading.backtest import run_backtest, metrics

REPORT_DIR = Path(DATA_DIR)/"research"
VERSION = "research_v6.4.2"
REPORTS = {}
QUEUE = []
QUEUED = set()
RUNNING = set()


def fingerprint(settings):
    # Explicitly exclude secrets, balances, operational controls and research gates.
    fields = [k for k in type(settings).model_fields if k.startswith(("fvg_", "rsi_", "rr_", "sl_"))]
    fields += ["timeframe", "context_timeframe", "ema_period", "risk_reward", "dynamic_rr", "adx_strong", "min_signal_score", "struct_sl",
               "swing_lookback", "structure_pivot", "require_structure", "min_relative_volume", "displacement_atr",
               "strategy_profile", "continuation_min_rvol", "retest_window_bars", "trendline_tolerance_atr", "min_net_rr", "max_entry_distance_atr", "fee_bps", "slippage_bps", "benchmark_correlation_min", "research_days"]
    data = {k: getattr(settings, k) for k in fields}
    return hashlib.sha256(json.dumps({"version": VERSION, "settings": data}, sort_keys=True).encode()).hexdigest()[:20]


def qualify(report, settings=None, now_ms=None):
    settings = settings or cfg
    now = int(time.time()*1000) if now_ms is None else now_ms
    if not report:
        return {"status": "PENDING", "reasons": ["backtest_pending"]}
    if report.get("status") == "ERROR":
        return {"status": "ERROR", "reasons": ["backtest_error"]}
    if report.get("fingerprint") != fingerprint(settings):
        return {"status": "STALE", "reasons": ["backtest_settings_changed"]}
    if not 0 <= now-report.get("as_of", 0) <= settings.research_refresh_hours*3600000:
        return {"status": "STALE", "reasons": ["backtest_stale"]}
    sample = report["enhanced"]["out_of_sample"]
    reasons = []
    if not report.get("data_quality", {}).get("complete", False):
        reasons.append("incomplete_history")
    if report["enhanced"].get("open_at_fold_ends", 0):
        reasons.append("unresolved_fold_positions")
    if sample["trades"] < settings.research_min_trades or any(f["trades"] < settings.research_min_test_trades for f in report["enhanced"]["folds"]):
        reasons.append("insufficient_out_of_sample_trades")
    if sample["expectancy_r"] is None or sample["expectancy_r"] <= 0:
        reasons.append("nonpositive_expectancy")
    if sample["profit_factor"] is not None and sample["profit_factor"] < settings.research_min_profit_factor:
        reasons.append("weak_profit_factor")
    if sample["max_drawdown_r"] > settings.research_max_drawdown_r:
        reasons.append("excessive_drawdown")
    if any(f["expectancy_r"] is None or f["expectancy_r"] <= 0 for f in report["enhanced"]["folds"]):
        reasons.append("unstable_test_periods")
    return {"status": "PASS" if not reasons else "REJECT", "reasons": reasons, "sample": sample}


def load_report(symbol):
    path = REPORT_DIR/f"{symbol}.json"
    if symbol not in REPORTS and path.exists():
        try:
            REPORTS[symbol] = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return REPORTS.get(symbol)


def request_research(symbol):
    import re
    if not re.fullmatch(r"[A-Z0-9]+USDT", symbol):
        return
    report = load_report(symbol)
    status = qualify(report)["status"]
    # Errors retry after a bounded cooldown, never hammer the API.
    if status == "ERROR" and time.time()*1000-report.get("as_of", 0) < 3600000:
        return
    if status in ("PENDING", "STALE", "ERROR") and symbol not in QUEUED and symbol not in RUNNING:
        QUEUE.append(symbol)
        QUEUED.add(symbol)


def evaluate_coin(symbol, df, htf, btc, settings, as_of, requested_start):
    step = int(settings.timeframe)*60000
    complete = (len(df) > settings.ema_period+300
                and int(df.timestamp.iloc[0]) <= requested_start+step
                and np.all(np.diff(df.timestamp) == step)
                and len(htf) >= settings.ema_period
                and np.all(np.diff(htf.timestamp) == int(settings.context_timeframe)*60000)
                and np.all(np.diff(btc.timestamp) == step)
                and int(btc.timestamp.iloc[0]) <= int(df.timestamp.iloc[0])
                and int(btc.timestamp.iloc[-1]) >= int(df.timestamp.iloc[-1])
                and as_of-int(df.timestamp.iloc[-1]) < step*2)
    # First 40% is warmup/development context, remaining three 20% folds are
    # replayed with no carried positions and no parameter selection on test data.
    boundaries = [int(len(df)*x) for x in (.4, .6, .8, 1)]
    variants = {}
    for enhanced in (False, True):
        logs, folds, unresolved = [], [], 0
        for start, end in zip(boundaries, boundaries[1:]):
            report = run_backtest(df.iloc[:end], htf, settings.context_timeframe, settings, btc, enhanced, start_index=start)
            logs.extend(report["trade_log"])
            folds.append(report["all"])
            unresolved += report["open_positions"]
        variants["enhanced" if enhanced else "baseline"] = {"folds": folds, "out_of_sample": metrics(logs), "open_at_fold_ends": unresolved}
    return {"symbol": symbol, "version": VERSION, "fingerprint": fingerprint(settings), "as_of": as_of,
            "status": "COMPLETE", "data_quality": {"complete": bool(complete), "bars": len(df),
                "start_ms": int(df.timestamp.iloc[0]), "end_ms": int(df.timestamp.iloc[-1]),
                "htf_bars": len(htf), "btc_bars": len(btc)}, **variants,
            "method": "Fixed parameters; chronological 40/20/20/20 split; next-open entries; SL first if ambiguous; fees/slippage deducted.",
            "limitations": ["Not full live portfolio execution: no trailing, cooldown or adaptive learning.",
                "No historical news, orderbook, funding charges or universe snapshots in this price-only test.",
                "Open positions at fold ends excluded from closed-trade metrics.",
                "Current-listed universe can have survivorship bias; 90 days is not all market regimes.",
                "Comparison tests a hypothesis and does not guarantee higher future winrate."]}


async def research_coin(symbol, settings=None):
    settings = (settings or cfg).model_copy(deep=True)
    if not str(settings.timeframe).isdigit() or int(settings.timeframe) >= 60:
        raise ValueError("Research currently requires a numeric entry timeframe below 60 minutes")
    as_of = int(time.time()*1000)
    start = as_of-settings.research_days*86400000
    async def history(pair, timeframe, since):
        directory = REPORT_DIR/"candles"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory/f"{pair}_{timeframe}.csv"
        step = int(timeframe)*60000
        if path.exists():
            cached = pd.read_csv(path)
            cached = cached[(cached.timestamp >= since) & (cached.timestamp+step <= as_of)]
            if len(cached) and cached.timestamp.iloc[0] <= since+step and as_of-cached.timestamp.iloc[-1] < 2*step:
                return cached.reset_index(drop=True)
        frame = closed_candles(await get_history(pair, timeframe, since, as_of), timeframe, as_of)
        temporary = path.with_suffix(".tmp")
        frame.to_csv(temporary, index=False)
        temporary.replace(path)
        return frame
    df = await history(symbol, settings.timeframe, start)
    htf = await history(symbol, settings.context_timeframe, start-350*int(settings.context_timeframe)*60000)
    btc = df if symbol == "BTCUSDT" else await history("BTCUSDT", settings.timeframe, start)
    if df.empty or htf.empty or btc.empty:
        raise ValueError("Missing historical series")
    report = await asyncio.to_thread(evaluate_coin, symbol, df, htf, btc, settings, as_of, start)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR/f"{symbol}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    REPORTS[symbol] = report
    return report


async def research_worker():
    while True:
        if not QUEUE or not cfg.is_running or not cfg.enable_coin_research:
            await asyncio.sleep(2)
            continue
        symbol = QUEUE.pop(0)
        QUEUED.discard(symbol)
        RUNNING.add(symbol)
        try:
            await research_coin(symbol)
        except Exception as exc:
            REPORTS[symbol] = {"status": "ERROR", "as_of": int(time.time()*1000), "error": f"{type(exc).__name__}: {exc}"}
            from trading.telemetry import event
            event('RESEARCH_FAILED',symbol=symbol,error=type(exc).__name__)
        finally:
            RUNNING.discard(symbol)


def research_gate(report, settings=None):
    settings = settings or cfg
    q = qualify(report, settings)
    if not settings.require_backtest_pass:
        return q, []
    if settings.research_gate_mode == "strict":
        return q, q["reasons"]
    sample = q.get("sample", {})
    upper = sample.get("expectancy_r_upper95")
    # Paper mode blocks a coin only when its loss is statistically clear; ~30 noisy trades
    # with a slightly negative mean are not evidence and used to block almost every coin.
    if sample.get("trades", 0) >= settings.research_min_trades and q["status"] == "REJECT" and upper is not None and upper < 0:
        return q, ["coin_backtest_clearly_negative"]
    # Unproven setups may enter paper observation, labelled explicitly; no fabricated probability.
    return q, []
