import asyncio
import json
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from core.settings import cfg, reload_settings
from trading.engine import LATEST_DATA, MARKET_DATA, BENCHMARKS, SCAN_STATUS
from trading.news import NEWS_STATE, news_context
from trading.research import REPORTS, QUEUE, RUNNING, load_report, qualify
from trading.paper_trading import get_paper_stats
from trading.learning import learning_summary, factor_performance
from core.database import get_equity_curve, get_history_signals, get_active_signals

app = FastAPI(title="FVG Scalper Dashboard")

@app.get("/health")
async def health():
    reload_settings()
    from trading.telemetry import system_health
    diagnostics=system_health()
    return {
        "ok": diagnostics['status']!='CRITICAL',
        "service_alive":True,
        "system_status":diagnostics['status'],
        "diagnostics":diagnostics,
        "is_running": cfg.is_running,
        "symbols_count": SCAN_STATUS['selected'] if cfg.scan_all_symbols else len(cfg.symbols),
        "configured_symbols_count": len(cfg.symbols),
        "active_trades": get_paper_stats().get("active_count", 0),
    }

@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)

@app.get("/api/status")
async def get_status():
    reload_settings()
    return {"is_running": cfg.is_running, "data": LATEST_DATA, "stats": get_paper_stats(), "scan":SCAN_STATUS}

@app.get("/api/market")
async def api_market():
    return {"contracts": len(MARKET_DATA), "eligible": sum(x["eligible"] for x in MARKET_DATA.values()), "data": MARKET_DATA}

@app.get("/api/quality")
async def api_quality():
    from trading.quality import quality_report
    return quality_report()


@app.get('/api/setups')
async def api_setups():
    from trading.setup_journal import report
    return {'mode':'historical_observations','setups':report()}


@app.get('/api/setups/{key}')
async def api_setup_history(key: str):
    from trading.setup_journal import history
    return {'events':history(key)}

@app.get("/api/research")
async def api_research():
    return {"queued": len(QUEUE), "running": sorted(RUNNING), "reports": {s: qualify(r) for s, r in REPORTS.items()},
            "news_sources": NEWS_STATE,
            "benchmarks": {s: {k: v for k, v in row.items() if k != "candles"} for s, row in BENCHMARKS.items()}}

@app.get("/api/coin/{symbol}")
async def api_coin(symbol: str):
    import re
    from fastapi import HTTPException
    if not re.fullmatch(r"[A-Z0-9]+USDT", symbol):
        raise HTTPException(400, "Expected an uppercase USDT symbol")
    from trading.market_watch import commentary
    from trading.dashboard import active_positions, clean
    report = load_report(symbol)
    news = news_context(symbol)
    active = next((t for t in active_positions(LATEST_DATA) if t["symbol"] == symbol), None)
    return clean({"symbol": symbol, "live": LATEST_DATA.get(symbol), "research": report,
                  "qualification": qualify(report), "news": news,
                  "commentary": commentary(symbol, LATEST_DATA.get(symbol), news, active)})


@app.get("/api/brief")
async def api_brief():
    from trading.market_watch import market_brief, feed
    from trading.dashboard import clean
    return clean({"brief": market_brief(LATEST_DATA, MARKET_DATA, BENCHMARKS), "feed": feed()})

@app.get("/api/equity")
async def api_equity():
    return get_equity_curve()

@app.get("/api/trades")
async def api_trades():
    return get_history_signals(limit=50)

@app.get("/api/active_trades")
async def api_active_trades():
    return get_active_signals()

@app.get("/api/factors")
async def api_factors():
    reload_settings()
    return learning_summary()

@app.get("/api/factor_performance")
async def api_factor_performance():
    reload_settings()
    return factor_performance(min_trades=1)

def _snapshot():
    """Copies taken on the event loop, so the worker thread never iterates dicts the engine is mutating."""
    return ({k: dict(v or {}) for k, v in list(LATEST_DATA.items())}, dict(SCAN_STATUS),
            dict(MARKET_DATA), {k: dict(v) for k, v in list(BENCHMARKS.items())})


def _overview(snapshot=None):
    from trading.dashboard import overview
    from trading.telemetry import system_health
    latest, scan, market, benchmarks = snapshot or _snapshot()
    try:
        health = system_health()
    except Exception as exc:
        health = {"status": "UNKNOWN", "reasons": [type(exc).__name__]}
    return overview(latest, scan, market, benchmarks, health)


@app.get("/api/overview")
async def api_overview():
    reload_settings()
    return await asyncio.to_thread(_overview, _snapshot())


@app.get("/api/equity_history")
async def api_equity_history():
    from trading.dashboard import equity_history
    return equity_history()


@app.get("/api/history")
async def api_history(limit: int = 100):
    from trading.dashboard import trade_history
    return trade_history(max(1, min(limit, 500)))


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    last_payload = None
    try:
        while True:
            reload_settings()
            data = await asyncio.to_thread(_overview, _snapshot())
            stamp = data.pop("generated_at")
            body = json.dumps(data, default=str, sort_keys=True)
            if body != last_payload:
                await websocket.send_text(json.dumps(dict(data, generated_at=stamp), default=str))
                last_payload = body
            await asyncio.sleep(2)
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        # A client can disconnect between the check and the send.
        pass


HTML_PAGE = (Path(__file__).parent / "web" / "dashboard.html").read_text(encoding="utf-8")
