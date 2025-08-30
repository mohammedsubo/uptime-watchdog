"""
Uptime Watchdog (Project #3 in DevCloud Toolkit)
------------------------------------------------
A tiny FastAPI service that monitors uptime and response time.
"""

from __future__ import annotations
import asyncio
import contextlib
import json
import math
import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, HttpUrl

DB_PATH = os.getenv("WATCHDOG_DB", "watchdog.db")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10"))

app = FastAPI(title="Uptime Watchdog", version="0.1.0")

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            ok INTEGER NOT NULL,
            status_code INTEGER,
            elapsed_ms REAL,
            error TEXT,
            FOREIGN KEY(check_id) REFERENCES checks(id)
        )
    """)
    conn.commit()
    conn.close()

class CheckIn(BaseModel):
    url: HttpUrl

class CheckOut(BaseModel):
    id: int
    url: HttpUrl
    created_at: str

class StatusItem(BaseModel):
    url: HttpUrl
    uptime_24h: float
    uptime_7d: float
    p95_ms_24h: Optional[float]
    total_samples_24h: int
    grade: str
    score: float

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def select_all_checks() -> List[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT id, url, created_at FROM checks ORDER BY id ASC").fetchall()
    conn.close()
    return rows

def insert_check(url: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO checks (url, created_at) VALUES (?, ?)", (url, now_utc_iso()))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        existing = conn.execute("SELECT id FROM checks WHERE url=?", (url,)).fetchone()
        if existing:
            return int(existing["id"])
        raise
    finally:
        conn.close()

def insert_result(check_id: int, ok: bool, status_code: Optional[int], elapsed_ms: Optional[float], error: Optional[str]) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO results (check_id, ts, ok, status_code, elapsed_ms, error) VALUES (?, ?, ?, ?, ?, ?)",
        (check_id, now_utc_iso(), 1 if ok else 0, status_code, elapsed_ms, error),
    )
    conn.commit()
    conn.close()

def fetch_results_since(check_id: int, since: datetime) -> List[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT ok, status_code, elapsed_ms, ts FROM results WHERE check_id=? AND ts>=? ORDER BY ts ASC",
        (check_id, since.replace(tzinfo=timezone.utc).isoformat()),
    ).fetchall()
    conn.close()
    return rows

def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    values_sorted = sorted(values)
    k = (len(values_sorted) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(values_sorted[int(k)])
    d0 = values_sorted[f] * (c - k)
    d1 = values_sorted[c] * (k - f)
    return float(d0 + d1)

def score_and_grade(uptime_pct: float, p95_ms: Optional[float]) -> Tuple[float, str]:
    uptime_score = max(0.0, min(100.0, uptime_pct))
    if p95_ms is None:
        latency_score = 0.0
    else:
        if p95_ms <= 300:
            latency_score = 100.0
        elif p95_ms >= 3000:
            latency_score = 0.0
        else:
            latency_score = 100.0 * (3000 - p95_ms) / (3000 - 300)
    score = 0.7 * uptime_score + 0.3 * latency_score
    if score >= 97:
        grade = "A+"
    elif score >= 93:
        grade = "A"
    elif score >= 90:
        grade = "A-"
    elif score >= 87:
        grade = "B+"
    elif score >= 83:
        grade = "B"
    elif score >= 80:
        grade = "B-"
    elif score >= 77:
        grade = "C+"
    elif score >= 73:
        grade = "C"
    elif score >= 70:
        grade = "C-"
    elif score >= 60:
        grade = "D"
    else:
        grade = "F"
    return float(score), grade

async def check_once(client: httpx.AsyncClient, check_id: int, url: str) -> None:
    try:
        start = datetime.now(timezone.utc)
        resp = await client.get(url, timeout=HTTP_TIMEOUT)
        elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000.0
        ok = 200 <= resp.status_code < 400
        insert_result(check_id, ok=ok, status_code=resp.status_code, elapsed_ms=elapsed_ms, error=None)
    except Exception as e:
        insert_result(check_id, ok=False, status_code=None, elapsed_ms=None, error=str(e)[:300])

async def monitor_loop() -> None:
    async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
        while True:
            checks = select_all_checks()
            tasks = [check_once(client, row["id"], row["url"]) for row in checks]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(CHECK_INTERVAL)

@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    asyncio.create_task(monitor_loop())

@app.get("/api/checks", response_model=List[CheckOut])
def api_list_checks():
    rows = select_all_checks()
    return [CheckOut(id=row["id"], url=row["url"], created_at=row["created_at"]) for row in rows]

@app.post("/api/checks", response_model=CheckOut)
def api_add_check(payload: CheckIn):
    check_id = insert_check(str(payload.url))
    rows = select_all_checks()
    row = next((r for r in rows if r["id"] == check_id), None)
    assert row is not None
    return CheckOut(id=row["id"], url=row["url"], created_at=row["created_at"])

@app.get("/api/status", response_model=List[StatusItem])
def api_status():
    rows = select_all_checks()
    out: List[StatusItem] = []
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    since_7d = now - timedelta(days=7)
    for r in rows:
        r24 = fetch_results_since(r["id"], since_24h)
        r7 = fetch_results_since(r["id"], since_7d)
        up24 = (sum(rr["ok"] for rr in r24) / len(r24) * 100.0) if r24 else 0.0
        up7 = (sum(rr["ok"] for rr in r7) / len(r7) * 100.0) if r7 else 0.0
        latencies_24 = [rr["elapsed_ms"] for rr in r24 if rr["ok"] and rr["elapsed_ms"] is not None]
        p95_24 = percentile(latencies_24, 95.0)
        score, grade = score_and_grade(up24, p95_24)
        out.append(
            StatusItem(
                url=r["url"],
                uptime_24h=round(up24, 2),
                uptime_7d=round(up7, 2),
                p95_ms_24h=round(p95_24, 1) if p95_24 is not None else None,
                total_samples_24h=len(r24),
                grade=grade,
                score=round(score, 1),
            )
        )
    return out

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Uptime Watchdog - Real-time Monitoring Dashboard</title>
  <style>
    * {
      margin: 0;
      padding: 0;
      box-sizing: border-box;
    }
    
    :root {
      --bg-primary: #0f172a;
      --bg-secondary: #1e293b;
      --bg-card: #1e293b;
      --border: #334155;
      --text-primary: #f1f5f9;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --accent: #10b981;
      --accent-hover: #059669;
      --danger: #ef4444;
      --warning: #f59e0b;
      --info: #3b82f6;
      --success: #10b981;
      --gradient: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    }
    
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background: var(--bg-primary);
      color: var(--text-primary);
      min-height: 100vh;
      background-image: 
        radial-gradient(at 20% 80%, rgba(16, 185, 129, 0.1) 0px, transparent 50%),
        radial-gradient(at 80% 20%, rgba(139, 92, 246, 0.1) 0px, transparent 50%),
        radial-gradient(at 40% 40%, rgba(59, 130, 246, 0.05) 0px, transparent 50%);
    }
    
    .container {
      max-width: 1400px;
      margin: 0 auto;
      padding: 20px;
    }
    
    /* Header */
    .header {
      background: var(--bg-secondary);
      border-radius: 16px;
      padding: 30px;
      margin-bottom: 30px;
      border: 1px solid var(--border);
      backdrop-filter: blur(10px);
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    }
    
    .header-content {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 20px;
    }
    
    .logo-section {
      display: flex;
      align-items: center;
      gap: 15px;
    }
    
    .logo {
      width: 50px;
      height: 50px;
      background: var(--gradient);
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 24px;
      animation: pulse 2s ease-in-out infinite;
    }
    
    @keyframes pulse {
      0%, 100% { transform: scale(1); }
      50% { transform: scale(1.05); }
    }
    
    h1 {
      font-size: 28px;
      font-weight: 700;
      background: var(--gradient);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }
    
    .subtitle {
      color: var(--text-secondary);
      font-size: 14px;
      margin-top: 5px;
    }
    
    .stats {
      display: flex;
      gap: 30px;
      align-items: center;
    }
    
    .stat {
      text-align: center;
    }
    
    .stat-value {
      font-size: 24px;
      font-weight: 700;
      color: var(--accent);
    }
    
    .stat-label {
      font-size: 12px;
      color: var(--text-muted);
      text-transform: uppercase;
      margin-top: 2px;
    }
    
    /* Add URL Section */
    .add-section {
      background: var(--bg-secondary);
      border-radius: 16px;
      padding: 25px;
      margin-bottom: 30px;
      border: 1px solid var(--border);
      display: flex;
      gap: 15px;
      align-items: center;
      flex-wrap: wrap;
    }
    
    .input-group {
      flex: 1;
      min-width: 300px;
      position: relative;
    }
    
    input[type="text"] {
      width: 100%;
      padding: 12px 15px 12px 45px;
      background: var(--bg-primary);
      border: 2px solid var(--border);
      border-radius: 10px;
      color: var(--text-primary);
      font-size: 14px;
      transition: all 0.3s;
    }
    
    input[type="text"]:focus {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.1);
    }
    
    input[type="text"]::placeholder {
      color: var(--text-muted);
    }
    
    .input-icon {
      position: absolute;
      left: 15px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-muted);
    }
    
    button {
      padding: 12px 30px;
      background: var(--gradient);
      border: none;
      border-radius: 10px;
      color: white;
      font-weight: 600;
      font-size: 14px;
      cursor: pointer;
      transition: all 0.3s;
      box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
    }
    
    button:hover:not(:disabled) {
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(102, 126, 234, 0.4);
    }
    
    button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    
    /* Cards Grid */
    .cards-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
      gap: 20px;
      margin-bottom: 30px;
    }
    
    .card {
      background: var(--bg-card);
      border-radius: 16px;
      padding: 20px;
      border: 1px solid var(--border);
      transition: all 0.3s;
      position: relative;
      overflow: hidden;
    }
    
    .card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      background: var(--gradient);
      transform: scaleX(0);
      transition: transform 0.3s;
    }
    
    .card:hover {
      transform: translateY(-5px);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
      border-color: var(--accent);
    }
    
    .card:hover::before {
      transform: scaleX(1);
    }
    
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: start;
      margin-bottom: 15px;
    }
    
    .url-info {
      flex: 1;
      min-width: 0;
    }
    
    .url-link {
      color: var(--text-primary);
      text-decoration: none;
      font-weight: 600;
      font-size: 14px;
      display: block;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      transition: color 0.3s;
    }
    
    .url-link:hover {
      color: var(--accent);
    }
    
    .url-domain {
      color: var(--text-muted);
      font-size: 12px;
      margin-top: 2px;
    }
    
    .grade-badge {
      padding: 6px 12px;
      border-radius: 20px;
      font-weight: 700;
      font-size: 14px;
      letter-spacing: 0.5px;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
    }
    
    .grade-A, .grade-A\\+ {
      background: linear-gradient(135deg, #10b981, #059669);
      color: white;
    }
    
    .grade-B {
      background: linear-gradient(135deg, #3b82f6, #2563eb);
      color: white;
    }
    
    .grade-C {
      background: linear-gradient(135deg, #f59e0b, #d97706);
      color: white;
    }
    
    .grade-D, .grade-F {
      background: linear-gradient(135deg, #ef4444, #dc2626);
      color: white;
    }
    
    .metrics {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 15px;
    }
    
    .metric {
      background: var(--bg-primary);
      padding: 12px;
      border-radius: 10px;
      border: 1px solid var(--border);
    }
    
    .metric-label {
      font-size: 11px;
      color: var(--text-muted);
      text-transform: uppercase;
      margin-bottom: 5px;
      display: flex;
      align-items: center;
      gap: 5px;
    }
    
    .metric-value {
      font-size: 20px;
      font-weight: 700;
      color: var(--text-primary);
    }
    
    .metric-unit {
      font-size: 12px;
      color: var(--text-secondary);
      font-weight: 400;
      margin-left: 2px;
    }
    
    /* Progress bars */
    .progress-bar {
      height: 4px;
      background: var(--border);
      border-radius: 2px;
      margin-top: 5px;
      overflow: hidden;
    }
    
    .progress-fill {
      height: 100%;
      background: var(--gradient);
      border-radius: 2px;
      transition: width 0.5s ease;
    }
    
    /* Status indicator */
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      display: inline-block;
      animation: blink 2s ease-in-out infinite;
    }
    
    .status-up {
      background: var(--success);
      box-shadow: 0 0 10px rgba(16, 185, 129, 0.5);
    }
    
    .status-down {
      background: var(--danger);
      box-shadow: 0 0 10px rgba(239, 68, 68, 0.5);
    }
    
    @keyframes blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }
    
    /* Footer */
    .footer {
      text-align: center;
      padding: 20px;
      color: var(--text-muted);
      font-size: 12px;
    }
    
    .api-info {
      background: var(--bg-secondary);
      border-radius: 10px;
      padding: 15px;
      margin-top: 10px;
      border: 1px solid var(--border);
      display: inline-block;
    }
    
    .mono {
      font-family: 'Consolas', 'Monaco', monospace;
      background: var(--bg-primary);
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      color: var(--accent);
    }
    
    /* Loading animation */
    .loading {
      display: inline-block;
      width: 20px;
      height: 20px;
      border: 3px solid var(--border);
      border-radius: 50%;
      border-top-color: var(--accent);
      animation: spin 1s ease-in-out infinite;
    }
    
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    
    /* Responsive */
    @media (max-width: 768px) {
      .header-content {
        flex-direction: column;
        text-align: center;
      }
      
      .stats {
        width: 100%;
        justify-content: space-around;
      }
      
      .cards-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <div class="header">
      <div class="header-content">
        <div class="logo-section">
          <div class="logo">👁️</div>
          <div>
            <h1>Uptime Watchdog</h1>
            <div class="subtitle">Real-time monitoring dashboard • Checks every <span id="interval">60</span>s</div>
          </div>
        </div>
        <div class="stats">
          <div class="stat">
            <div class="stat-value" id="total-services">0</div>
            <div class="stat-label">Services</div>
          </div>
          <div class="stat">
            <div class="stat-value" id="avg-uptime">0%</div>
            <div class="stat-label">Avg Uptime</div>
          </div>
          <div class="stat">
            <div class="stat-value" id="current-time">--:--</div>
            <div class="stat-label">Local Time</div>
          </div>
        </div>
      </div>
    </div>
    
    <!-- Add URL Section -->
    <div class="add-section">
      <div class="input-group">
        <span class="input-icon">🔗</span>
        <input type="text" id="url" placeholder="https://your-service.example.com" />
      </div>
      <button onclick="addUrl()" id="add-btn">
        Add Service
      </button>
    </div>
    
    <!-- Cards Grid -->
    <div class="cards-grid" id="cards-container">
      <!-- Cards will be inserted here -->
    </div>
    
    <!-- Footer -->
    <div class="footer">
      <div class="api-info">
        <strong>API Endpoints:</strong>
        <span class="mono">GET /api/status</span>
        <span class="mono">GET /api/checks</span>
        <span class="mono">POST /api/checks</span>
      </div>
    </div>
  </div>
  
  <script>
    const interval = %CHECK_INTERVAL%;
    document.getElementById('interval').textContent = interval;
    
    function formatUptime(value) {
      if (value == null) return '—';
      return value.toFixed(1) + '%';
    }
    
    function formatLatency(value) {
      if (value == null) return '—';
      if (value < 1000) return value.toFixed(0) + ' ms';
      return (value / 1000).toFixed(1) + ' s';
    }
    
    function getGradeClass(grade) {
      if (!grade) return '';
      const base = grade.replace('+', '\\+').replace('-', '');
      return 'grade-' + base;
    }
    
    function getDomainFromUrl(url) {
      try {
        const u = new URL(url);
        return u.hostname;
      } catch {
        return url;
      }
    }
    
    function createCard(item) {
      const gradeClass = getGradeClass(item.grade);
      const isUp = item.uptime_24h > 99;
      const statusClass = isUp ? 'status-up' : 'status-down';
      
      return `
        <div class="card">
          <div class="card-header">
            <div class="url-info">
              <a href="${item.url}" target="_blank" class="url-link">${item.url}</a>
              <div class="url-domain">
                <span class="status-dot ${statusClass}"></span>
                ${getDomainFromUrl(item.url)}
              </div>
            </div>
            <div class="grade-badge ${gradeClass}">${item.grade}</div>
          </div>
          
          <div class="metrics">
            <div class="metric">
              <div class="metric-label">
                📊 Uptime (24h)
              </div>
              <div class="metric-value">
                ${formatUptime(item.uptime_24h)}
              </div>
              <div class="progress-bar">
                <div class="progress-fill" style="width: ${item.uptime_24h}%"></div>
              </div>
            </div>
            
            <div class="metric">
              <div class="metric-label">
                📈 Uptime (7d)
              </div>
              <div class="metric-value">
                ${formatUptime(item.uptime_7d)}
              </div>
              <div class="progress-bar">
                <div class="progress-fill" style="width: ${item.uptime_7d}%"></div>
              </div>
            </div>
            
            <div class="metric">
              <div class="metric-label">
                ⚡ P95 Latency
              </div>
              <div class="metric-value">
                ${formatLatency(item.p95_ms_24h)}
              </div>
            </div>
            
            <div class="metric">
              <div class="metric-label">
                📝 Samples (24h)
              </div>
              <div class="metric-value">
                ${item.total_samples_24h}
              </div>
            </div>
          </div>
        </div>
      `;
    }
    
    async function load() {
      try {
        const response = await fetch('/api/status');
        const data = await response.json();
        
        const container = document.getElementById('cards-container');
        container.innerHTML = data.map(item => createCard(item)).join('');
        
        // Update stats
        document.getElementById('total-services').textContent = data.length;
        
        if (data.length > 0) {
          const avgUptime = data.reduce((sum, item) => sum + item.uptime_24h, 0) / data.length;
          document.getElementById('avg-uptime').textContent = avgUptime.toFixed(1) + '%';
        }
      } catch (error) {
        console.error('Error loading data:', error);
      }
    }
    
    async function addUrl() {
      const urlInput = document.getElementById('url');
      const addBtn = document.getElementById('add-btn');
      const url = urlInput.value.trim();
      
      if (!url) return;
      
      addBtn.disabled = true;
      addBtn.innerHTML = '<span class="loading"></span>';
      
      try {
        const response = await fetch('/api/checks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });
        
        if (response.ok) {
          urlInput.value = '';
          await load();
        } else {
          alert('Failed to add URL. Please check the format.');
        }
      } catch (error) {
        alert('Error: ' + error.message);
      } finally {
        addBtn.disabled = false;
        addBtn.innerHTML = 'Add Service';
      }
    }
    
    function updateTime() {
      const now = new Date();
      const timeStr = now.toLocaleTimeString('en-US', { 
        hour: '2-digit', 
        minute: '2-digit',
        hour12: false 
      });
      document.getElementById('current-time').textContent = timeStr;
    }
    
    // Enter key support
    document.getElementById('url').addEventListener('keypress', (e) => {
      if (e.key === 'Enter') addUrl();
    });
    
    // Initial load and intervals
    updateTime();
    load();
    setInterval(updateTime, 1000);
    setInterval(load, Math.max(15000, interval * 1000));
  </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    html = INDEX_HTML.replace("%CHECK_INTERVAL%", str(CHECK_INTERVAL))
    return HTMLResponse(content=html)

@app.get("/api/health")
def health():
    return JSONResponse({"status": "ok", "time": now_utc_iso(), "interval_s": CHECK_INTERVAL})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)