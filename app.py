import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from core.settings import cfg
from trading.engine import LATEST_DATA
from trading.paper_trading import get_paper_stats
from core.database import get_equity_curve, get_history_signals, get_active_signals

app = FastAPI(title="FVG Scalper Dashboard")

@app.get("/")
async def index():
    return HTMLResponse(HTML_PAGE)

@app.get("/api/status")
async def get_status():
    return LATEST_DATA

@app.get("/api/equity")
async def api_equity():
    return get_equity_curve()

@app.get("/api/trades")
async def api_trades():
    return get_history_signals(limit=50)

@app.get("/api/active_trades")
async def api_active_trades():
    return get_active_signals()

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    last_data_hash = 0
    try:
        while True:
            # We don't need to load_settings, we use global cfg and assume settings updates reflect there
            current_data = {
                "is_running": cfg.is_running,
                "data": LATEST_DATA,
                "stats": get_paper_stats()
            }
            
            # Simple hash to check if data changed significantly
            current_hash = hash(str(current_data))
            
            if current_hash != last_data_hash:
                await websocket.send_text(json.dumps(current_data))
                last_data_hash = current_hash
                
            await asyncio.sleep(1) # Check every second, but only send if changed
    except WebSocketDisconnect:
        pass


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FVG Scalper Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {
  --bg: #0b0e14; --surface: #121620; --surface2: #171c28; --line: #232a38;
  --text: #e6e9ef; --text2: #aab3c2; --muted: #6b7585;
  --accent: #3b82f6; --accent2: #2563eb;
  --green: #16c784; --red: #ea3943; --amber: #f0a020;
  --radius: 12px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: 'Inter', sans-serif;
  padding: 20px;
}
.header {
  display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 15px;
  margin-bottom: 20px;
  padding: 20px; background: var(--surface);
  border-radius: var(--radius); border: 1px solid var(--line);
}
.header h1 { font-size: clamp(20px, 5vw, 24px); font-weight: 800; }
.header .status { color: var(--green); font-weight: 600; font-size: 14px; }

.stats-panel {
  display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 30px;
}
.stat-box {
  flex: 1 1 120px; background: var(--surface); border: 1px solid var(--line);
  padding: 15px; border-radius: var(--radius); text-align: center;
}
.stat-box .lbl { color: var(--text2); font-size: 12px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 5px;}
.stat-box .val { font-size: 20px; font-weight: 700; color: var(--text); }
.stat-box.profit .val { color: var(--green); }
.stat-box.loss .val { color: var(--red); }

.grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px;
}
.card {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 20px; margin-bottom: 20px;
}
.card h2 { font-size: 18px; margin-bottom: 15px; display: flex; justify-content: space-between; border-bottom: 1px solid var(--line); padding-bottom: 10px;}
.card p { color: var(--text2); margin-bottom: 8px; font-size: 14px;}
.signal {
  margin-top: 15px; padding: 15px; border-radius: 8px;
  background: var(--surface2); border-left: 4px solid var(--accent);
}
.signal.long { border-left-color: var(--green); }
.signal.short { border-left-color: var(--red); }
.val { color: var(--text); font-weight: 600; }
.bullish { color: var(--green); font-weight: bold; }
.bearish { color: var(--red); font-weight: bold; }

/* Table styles */
table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }
th, td { padding: 12px; text-align: left; border-bottom: 1px solid var(--line); }
th { color: var(--text2); font-weight: 600; }
tr:hover { background: var(--surface2); }
.t-profit { color: var(--green); font-weight: bold; }
.t-loss { color: var(--red); font-weight: bold; }
</style>
</head>
<body>
  <div class="header">
    <h1>FVG Strategy Dashboard</h1>
    <div class="status" id="conn-status">🟢 Connected to Engine</div>
  </div>
  
  <div class="stats-panel" id="stats-panel" style="display:none;">
    <div class="stat-box"><div class="lbl">Virtual Balance</div><div class="val" id="st-bal">0 USDT</div></div>
    <div class="stat-box"><div class="lbl">Win Rate</div><div class="val" id="st-win">0%</div></div>
    <div class="stat-box"><div class="lbl">Total PnL (%)</div><div class="val" id="st-pnl">0%</div></div>
    <div class="stat-box"><div class="lbl">Active Trades</div><div class="val" id="st-act">0</div></div>
  </div>

  <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-bottom: 20px;">
      <div class="card">
        <h2>Equity Curve</h2>
        <canvas id="equityChart" height="100"></canvas>
      </div>
      <div class="card">
        <h2>Active Positions</h2>
        <div id="active-trades-list">Fetching...</div>
      </div>
  </div>

  <div class="card">
    <h2>Recent Trade History</h2>
    <div style="overflow-x: auto;">
        <table>
            <thead>
                <tr>
                    <th>Time</th>
                    <th>Pair</th>
                    <th>Side</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>PnL (%)</th>
                    <th>Result</th>
                </tr>
            </thead>
            <tbody id="history-body">
                <tr><td colspan="7" style="text-align: center;">Loading...</td></tr>
            </tbody>
        </table>
    </div>
  </div>

  <h2 style="margin: 30px 0 15px 0;">Live Market Data</h2>
  <div class="grid" id="pairs-grid">
    <!-- Content injected via JS -->
  </div>

  <script>
    // Initialize Chart
    const ctx = document.getElementById('equityChart').getContext('2d');
    const equityChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Cumulative PnL (%)',
                data: [],
                borderColor: '#3b82f6',
                backgroundColor: 'rgba(59, 130, 246, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.1,
                pointRadius: 0
            }]
        },
        options: {
            responsive: true,
            scales: {
                y: { grid: { color: '#232a38' }, ticks: { color: '#aab3c2' } },
                x: { grid: { display: false }, ticks: { color: '#aab3c2' } }
            },
            plugins: {
                legend: { display: false }
            }
        }
    });

    async function fetchEquity() {
        try {
            const res = await fetch('/api/equity');
            const data = await res.json();
            const labels = data.map(d => d.ts);
            const pnl = data.map(d => d.cum);
            equityChart.data.labels = labels;
            equityChart.data.datasets[0].data = pnl;
            equityChart.update();
        } catch(e) { console.error('Error fetching equity', e); }
    }

    async function fetchHistory() {
        try {
            const res = await fetch('/api/trades');
            const data = await res.json();
            const tbody = document.getElementById('history-body');
            tbody.innerHTML = '';
            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align: center;">No history yet</td></tr>';
                return;
            }
            data.forEach(t => {
                const isWin = t.outcome === 'WIN';
                const pnlClass = isWin ? 't-profit' : 't-loss';
                const pnlSign = t.pnl_pct > 0 ? '+' : '';
                const row = `<tr>
                    <td>${(t.closed_ts || t.ts).substring(11, 19)}</td>
                    <td><b>${t.symbol}</b></td>
                    <td class="${t.side === 'LONG' ? 't-profit' : 't-loss'}">${t.side}</td>
                    <td>${t.entry.toFixed(4)}</td>
                    <td>${t.exit_price ? t.exit_price.toFixed(4) : '-'}</td>
                    <td class="${pnlClass}">${pnlSign}${t.pnl_pct.toFixed(2)}%</td>
                    <td>${isWin ? '✅' : '❌'} ${t.exit_reason || ''}</td>
                </tr>`;
                tbody.innerHTML += row;
            });
        } catch(e) { console.error('Error fetching history', e); }
    }

    async function fetchActiveTrades() {
        try {
            const res = await fetch('/api/active_trades');
            const data = await res.json();
            const div = document.getElementById('active-trades-list');
            if (data.length === 0) {
                div.innerHTML = '<p style="color: var(--text2);">No active positions.</p>';
                return;
            }
            let html = '';
            data.forEach(t => {
                html += `<div style="padding: 10px; border-bottom: 1px solid var(--line);">
                    <div style="display:flex; justify-content:space-between; margin-bottom:5px;">
                        <b>${t.symbol}</b>
                        <span class="${t.side === 'LONG' ? 't-profit' : 't-loss'}">${t.side}</span>
                    </div>
                    <div style="color:var(--text2); font-size:13px;">
                        Entry: ${t.entry.toFixed(4)} | SL: ${t.sl.toFixed(4)} | TP: ${t.tp.toFixed(4)}
                    </div>
                </div>`;
            });
            div.innerHTML = html;
        } catch(e) { console.error('Error fetching active trades', e); }
    }

    // Refresh charts and tables periodically
    setInterval(fetchEquity, 10000);
    setInterval(fetchHistory, 10000);
    setInterval(fetchActiveTrades, 5000);
    
    // Initial fetch
    fetchEquity();
    fetchHistory();
    fetchActiveTrades();

    const ws = new WebSocket(`ws://${window.location.host}/ws`);
    
    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      const is_running = payload.is_running;
      const data = payload.data;
      const stats = payload.stats;
      
      if (stats) {
          document.getElementById('stats-panel').style.display = 'flex';
          document.getElementById('st-bal').innerText = stats.balance.toFixed(2) + ' USDT';
          document.getElementById('st-win').innerText = stats.win_rate + '%';
          
          const pnlEl = document.getElementById('st-pnl');
          pnlEl.innerText = (stats.total_pnl > 0 ? '+' : '') + stats.total_pnl.toFixed(2) + '%';
          pnlEl.parentElement.className = 'stat-box ' + (stats.total_pnl >= 0 ? 'profit' : 'loss');
          
          document.getElementById('st-act').innerText = stats.active_count;
      }
      
      const grid = document.getElementById('pairs-grid');
      const statusEl = document.getElementById('conn-status');
      
      if (!is_running) {
        statusEl.innerText = '⏸️ Bot is Stopped';
        statusEl.style.color = 'var(--amber)';
        return;
      }
      
      statusEl.innerText = '🟢 Connected to Engine';
      statusEl.style.color = 'var(--green)';
      
      if (Object.keys(data).length === 0) {
        grid.innerHTML = '<div style="color:var(--text2);text-align:center;grid-column:1/-1;padding:40px;font-size:18px;">Fetching market data from Bybit... please wait.</div>';
        return;
      }
      
      grid.innerHTML = '';
      
      for (const [symbol, info] of Object.entries(data)) {
        const fvg = info.fvg;
        const sig = info.signal;
        const price = info.current_price;
        
        let html = `<div class="card" style="margin-bottom:0;">
          <h2 style="border:none; padding:0; margin-bottom:10px;"><span>${symbol}</span> <span>${price ? price.toFixed(4) : '...'}</span></h2>`;
          
        if (fvg) {
            const fvgClass = fvg.type === 'BULLISH' ? 'bullish' : 'bearish';
            html += `<p>Latest FVG: <span class="${fvgClass}">${fvg.type}</span></p>`;
            html += `<p>Top: <span class="val">${fvg.top.toFixed(4)}</span> | Bottom: <span class="val">${fvg.bottom.toFixed(4)}</span></p>`;
        } else {
            html += `<p>No recent FVG detected.</p>`;
        }
        
        if (sig) {
            const sigClass = sig.signal === 'LONG' ? 'long' : 'short';
            html += `<div class="signal ${sigClass}">
                <p style="color:var(--text);font-weight:bold;margin-bottom:10px;">${sig.signal} SIGNAL</p>
                <p>Entry: <span class="val">${sig.entry.toFixed(4)}</span></p>
                <p>Stop Loss: <span class="val">${sig.sl.toFixed(4)}</span></p>
                <p>Take Profit: <span class="val">${sig.tp.toFixed(4)}</span></p>
            </div>`;
        }
        
        html += `</div>`;
        grid.innerHTML += html;
      }
    };
    
    ws.onclose = () => {
        document.getElementById('conn-status').innerText = '🔴 Disconnected';
        document.getElementById('conn-status').style.color = 'var(--red)';
    };
  </script>
</body>
</html>
"""
