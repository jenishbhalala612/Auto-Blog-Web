"""
AutoBlog Engine — Monitoring Dashboard Server

A lightweight HTTP server that serves the real-time monitoring dashboard.
Reads pipeline status from the JSON status file and SQLite database.

Usage:
    python monitor.py             # Start on port 5000
    python monitor.py --port 8888 # Custom port
"""

from __future__ import annotations

import json
import sqlite3
import argparse
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import sys
sys.path.insert(0, str(Path(__file__).parent))

# Force UTF-8 output on Windows (avoids cp1252 emoji crash)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import config
from modules.status_tracker import get_status, force_stop

DASHBOARD_PORT = 5050


# ── Pipeline Process Manager ──────────────────────────────────────────────────

import subprocess
import signal

_pipeline_proc: subprocess.Popen | None = None
_pipeline_lock = threading.Lock()
_PIPELINE_DIR = Path(__file__).parent


def pipeline_start(resume: bool = True) -> dict:
    """Start the pipeline subprocess. Returns status dict."""
    global _pipeline_proc
    with _pipeline_lock:
        # Already running?
        if _pipeline_proc is not None and _pipeline_proc.poll() is None:
            return {"ok": False, "message": "Pipeline is already running"}
        
        status = get_status()
        cmd = [sys.executable, "-X", "utf8", "main.py"]
        # If interrupted and resume is requested
        if resume and status.get("status") in ("running", "error") and status.get("current_step", 1) > 1:
            cmd.append("--resume")
            msg = f"Pipeline resuming from step {status.get('current_step', 1)}"
        else:
            msg = "Pipeline started fresh"

        try:
            _pipeline_proc = subprocess.Popen(
                cmd,
                cwd=str(_PIPELINE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"ok": True, "message": f"{msg} (pid {_pipeline_proc.pid})"}
        except Exception as e:
            return {"ok": False, "message": f"Failed to start: {e}"}


def pipeline_stop() -> dict:
    """Stop the running pipeline subprocess."""
    global _pipeline_proc
    with _pipeline_lock:
        if _pipeline_proc is None or _pipeline_proc.poll() is not None:
            return {"ok": False, "message": "Pipeline is not running"}
        try:
            _pipeline_proc.terminate()
            try:
                _pipeline_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _pipeline_proc.kill()
            pid = _pipeline_proc.pid
            _pipeline_proc = None
            force_stop()
            return {"ok": True, "message": f"Pipeline stopped (was pid {pid})"}
        except Exception as e:
            return {"ok": False, "message": f"Failed to stop: {e}"}


def pipeline_proc_status() -> dict:
    """Return whether the pipeline subprocess is alive."""
    global _pipeline_proc
    with _pipeline_lock:
        if _pipeline_proc is None:
            return {"running": False, "pid": None}
        code = _pipeline_proc.poll()
        if code is None:
            return {"running": True, "pid": _pipeline_proc.pid}
        # Process finished
        _pipeline_proc = None
        return {"running": False, "pid": None, "exit_code": code}



def _db_query(sql: str, params=()) -> list[dict]:
    """Run a read-only query against the pipeline SQLite database (with timeout)."""
    try:
        # timeout=2 prevents blocking if pipeline has a write lock
        conn = sqlite3.connect(str(config.DATABASE_PATH), timeout=2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # allow concurrent reads
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _db_scalar(sql: str, params=(), default=0):
    rows = _db_query(sql, params)
    if rows:
        return list(rows[0].values())[0]
    return default


def get_dashboard_data() -> dict:
    """Aggregate all data for the dashboard API response."""
    today = datetime.now().strftime("%Y-%m-%d")
    last_7 = (datetime.now() - timedelta(days=7)).isoformat()

    # ── Today's stats ──────────────────────────────────────────────────────
    stats = {
        "topics_today":     _db_scalar("SELECT COUNT(*) FROM research WHERE DATE(created_at)=?", (today,)),
        "keywords_today":   _db_scalar("SELECT COUNT(*) FROM keywords WHERE DATE(created_at)=?", (today,)),
        "drafts_pending":   _db_scalar("SELECT COUNT(*) FROM posts WHERE status='draft'"),
        "approved_pending": _db_scalar("SELECT COUNT(*) FROM posts WHERE status='approved'"),
        "total_published":  _db_scalar("SELECT COUNT(*) FROM posts WHERE status='published'"),
        "published_today":  _db_scalar("SELECT COUNT(*) FROM posts WHERE DATE(published_at)=?", (today,)),
        "errors_today":     _db_scalar("SELECT COUNT(*) FROM errors WHERE DATE(created_at)=?", (today,)),
        "auto_fixed_today": _db_scalar("SELECT COUNT(*) FROM errors WHERE DATE(created_at)=? AND auto_fixed=1", (today,)),
    }

    # ── Pipeline run history (last 10) ─────────────────────────────────────
    runs = _db_query(
        "SELECT run_type, status, message, duration_seconds, started_at, completed_at "
        "FROM pipeline_runs ORDER BY id DESC LIMIT 10"
    )

    # ── Recent errors ──────────────────────────────────────────────────────
    errors = _db_query(
        "SELECT module, error_type, message, auto_fixed, fix_action, created_at "
        "FROM errors ORDER BY id DESC LIMIT 8"
    )

    # ── Latest health checks (one per type) ───────────────────────────────
    health_rows = _db_query(
        "SELECT check_type, status, response_time_ms, details, created_at "
        "FROM health_checks WHERE id IN ("
        "  SELECT MAX(id) FROM health_checks GROUP BY check_type"
        ") ORDER BY check_type"
    )

    # ── Recent posts ───────────────────────────────────────────────────────
    posts = _db_query(
        "SELECT title, language, status, word_count, created_at, published_at "
        "FROM posts ORDER BY id DESC LIMIT 10"
    )

    # ── Trending sources breakdown (today) ────────────────────────────────
    sources = _db_query(
        "SELECT source, COUNT(*) as count FROM research "
        "WHERE DATE(created_at)=? GROUP BY source ORDER BY count DESC",
        (today,)
    )

    # ── Keyword sources breakdown (today) ─────────────────────────────────
    kw_sources = _db_query(
        "SELECT source, COUNT(*) as count FROM keywords "
        "WHERE DATE(created_at)=? GROUP BY source ORDER BY count DESC LIMIT 6",
        (today,)
    )

    # ── Weekly research chart data ─────────────────────────────────────────
    weekly = _db_query(
        "SELECT DATE(created_at) as day, COUNT(*) as topics "
        "FROM research WHERE created_at >= ? GROUP BY day ORDER BY day",
        (last_7,)
    )

    return {
        "status": get_status(),
        "stats": stats,
        "runs": runs,
        "errors": errors,
        "health": health_rows,
        "posts": posts,
        "sources": sources,
        "kw_sources": kw_sources,
        "weekly": weekly,
        "server_time": datetime.now().isoformat(),
    }


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests for the monitoring dashboard."""

    def log_message(self, format, *args):
        pass  # Suppress default request logging

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "" or path == "/":
            self._serve_dashboard()
        elif path == "/api/data":
            self._serve_json(get_dashboard_data())
        elif path == "/api/status":
            self._serve_json(get_status())
        elif path == "/api/health":
            self._serve_json({"ok": True, "time": datetime.now().isoformat()})
        elif path == "/api/pipeline/proc":
            self._serve_json(pipeline_proc_status())
        else:
            self._send(404, "text/plain", b"Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/pipeline/start":
            self._serve_json(pipeline_start(resume=True))
        elif path == "/api/pipeline/start_fresh":
            self._serve_json(pipeline_start(resume=False))
        elif path == "/api/pipeline/stop":
            self._serve_json(pipeline_stop())
        else:
            self._send(404, "text/plain", b"Not Found")

    def do_OPTIONS(self):
        """CORS preflight support."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _serve_dashboard(self):
        html = DASHBOARD_HTML.encode("utf-8")
        self._send(200, "text/html; charset=utf-8", html)

    def _serve_json(self, data: dict):
        body = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AutoBlog Monitor — Live Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #0a0e1a;
    --bg2:       #0f1525;
    --bg3:       #161d30;
    --border:    rgba(99,120,255,0.18);
    --accent:    #6378ff;
    --accent2:   #a78bfa;
    --green:     #22d3a0;
    --red:       #f87171;
    --yellow:    #fbbf24;
    --blue:      #38bdf8;
    --text:      #e2e8f0;
    --text-dim:  #64748b;
    --text-muted:#94a3b8;
    --glow:      0 0 30px rgba(99,120,255,0.15);
    --card-bg:   rgba(15,21,37,0.8);
    --radius:    14px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Animated starfield background */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background:
      radial-gradient(ellipse at 20% 20%, rgba(99,120,255,0.06) 0%, transparent 60%),
      radial-gradient(ellipse at 80% 80%, rgba(167,139,250,0.05) 0%, transparent 60%);
    pointer-events: none;
    z-index: 0;
  }

  .wrap { position: relative; z-index: 1; max-width: 1600px; margin: 0 auto; padding: 24px; }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 28px;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
  }

  .logo { display: flex; align-items: center; gap: 12px; }
  .logo-icon {
    width: 42px; height: 42px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
    box-shadow: 0 0 20px rgba(99,120,255,0.4);
  }
  .logo-text h1 { font-size: 1.3rem; font-weight: 700; color: var(--text); }
  .logo-text p  { font-size: 0.75rem; color: var(--text-dim); margin-top: 1px; }

  .header-right { display: flex; align-items: center; gap: 16px; }

  #live-badge {
    display: flex; align-items: center; gap: 6px;
    padding: 5px 12px;
    background: rgba(34,211,160,0.1);
    border: 1px solid rgba(34,211,160,0.3);
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--green);
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  #live-badge::before {
    content: '';
    width: 7px; height: 7px;
    background: var(--green);
    border-radius: 50%;
    animation: pulse 1.5s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%       { opacity: 0.4; transform: scale(0.85); }
  }

  #server-time { font-size: 0.78rem; color: var(--text-dim); font-family: 'JetBrains Mono', monospace; }

  /* ── Grid layout ── */
  .grid-main   { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  .grid-middle { display: grid; grid-template-columns: 1.4fr 1fr; gap: 20px; margin-bottom: 20px; }
  .grid-bottom { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  .grid-last   { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }

  @media (max-width: 1200px) {
    .grid-main, .grid-bottom { grid-template-columns: 1fr 1fr; }
    .grid-middle, .grid-last { grid-template-columns: 1fr; }
  }
  @media (max-width: 700px) {
    .grid-main, .grid-middle, .grid-bottom, .grid-last { grid-template-columns: 1fr; }
  }

  /* ── Card base ── */
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    backdrop-filter: blur(12px);
    transition: border-color 0.25s, box-shadow 0.25s;
  }
  .card:hover {
    border-color: rgba(99,120,255,0.35);
    box-shadow: var(--glow);
  }
  .card-title {
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-dim);
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    gap: 7px;
  }
  .card-title span { font-size: 14px; }

  /* ── Current Task Card (hero) ── */
  #current-task-card {
    grid-column: 1 / -1;
    background: linear-gradient(135deg, rgba(99,120,255,0.08) 0%, rgba(167,139,250,0.05) 100%);
    border-color: rgba(99,120,255,0.3);
  }

  .task-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 18px;
  }

  #task-name {
    font-size: 1.6rem;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.02em;
  }
  #task-name.idle  { color: var(--text-dim); font-weight: 500; }
  #task-name.error { color: var(--red); }

  #task-status-badge {
    padding: 5px 14px;
    border-radius: 999px;
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    flex-shrink: 0;
  }
  .badge-running { background: rgba(99,120,255,0.2); border: 1px solid rgba(99,120,255,0.5); color: var(--accent); }
  .badge-idle    { background: rgba(100,116,139,0.15); border: 1px solid rgba(100,116,139,0.3); color: var(--text-dim); }
  .badge-done    { background: rgba(34,211,160,0.15); border: 1px solid rgba(34,211,160,0.4); color: var(--green); }
  .badge-error   { background: rgba(248,113,113,0.15); border: 1px solid rgba(248,113,113,0.4); color: var(--red); }

  /* Progress bar */
  .progress-wrap { position: relative; height: 8px; background: rgba(255,255,255,0.06); border-radius: 999px; overflow: hidden; margin-bottom: 12px; }
  #progress-bar {
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    border-radius: 999px;
    transition: width 0.6s cubic-bezier(0.4,0,0.2,1);
    position: relative;
  }
  #progress-bar::after {
    content: '';
    position: absolute;
    right: 0; top: 0; bottom: 0;
    width: 40px;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.4));
    animation: shimmer 1.5s ease-in-out infinite;
  }
  @keyframes shimmer {
    0%   { opacity: 0; }
    50%  { opacity: 1; }
    100% { opacity: 0; }
  }

  .progress-meta { display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-dim); }
  #step-label  { font-family: 'JetBrains Mono', monospace; }
  #progress-pct { font-weight: 600; color: var(--accent); }

  /* Counter pills row */
  .counter-row {
    display: flex;
    gap: 12px;
    margin-top: 18px;
    flex-wrap: wrap;
  }
  .counter-pill {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 14px;
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border);
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 500;
  }
  .counter-pill .val { font-weight: 700; font-size: 0.95rem; }
  .cp-topics   .val { color: var(--blue); }
  .cp-keywords .val { color: var(--accent2); }
  .cp-drafts   .val { color: var(--yellow); }
  .cp-pub      .val { color: var(--green); }

  /* ── Stat cards ── */
  .stat-card { text-align: center; }
  .stat-card .big-num {
    font-size: 2.6rem;
    font-weight: 700;
    letter-spacing: -0.03em;
    line-height: 1;
    margin-bottom: 4px;
  }
  .stat-card .sub { font-size: 0.72rem; color: var(--text-dim); }

  /* ── Log viewer ── */
  #log-box {
    background: rgba(0,0,0,0.4);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
    height: 180px;
    overflow-y: auto;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: var(--text-muted);
    scroll-behavior: smooth;
  }
  #log-box .log-line { margin-bottom: 3px; }
  #log-box .log-line:last-child { color: var(--text); }
  #log-box .ts { color: var(--text-dim); margin-right: 6px; }

  /* ── Steps timeline ── */
  .steps-wrap { display: flex; flex-direction: column; gap: 6px; }
  .step-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 12px;
    border-radius: 8px;
    transition: background 0.2s;
  }
  .step-item.active  { background: rgba(99,120,255,0.12); }
  .step-item.done    { background: rgba(34,211,160,0.07); }
  .step-item.pending { opacity: 0.4; }

  .step-dot {
    width: 28px; height: 28px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px;
    flex-shrink: 0;
  }
  .step-dot.active  { background: var(--accent); color: #fff; animation: spin 2s linear infinite; }
  .step-dot.done    { background: var(--green); color: #fff; }
  .step-dot.pending { background: rgba(255,255,255,0.06); color: var(--text-dim); font-size: 10px; border: 1px solid var(--border); }
  @keyframes spin {
    from { box-shadow: 0 0 0 0 rgba(99,120,255,0.4); }
    50%  { box-shadow: 0 0 0 6px rgba(99,120,255,0); }
    to   { box-shadow: 0 0 0 0 rgba(99,120,255,0); }
  }

  .step-label { font-size: 0.82rem; font-weight: 500; flex: 1; }
  .step-badge {
    font-size: 0.65rem;
    font-weight: 600;
    text-transform: uppercase;
    padding: 2px 8px;
    border-radius: 999px;
  }
  .step-badge.active  { background: rgba(99,120,255,0.2); color: var(--accent); }
  .step-badge.done    { background: rgba(34,211,160,0.15); color: var(--green); }
  .step-badge.pending { background: rgba(255,255,255,0.05); color: var(--text-dim); }

  /* ── Health table ── */
  .health-grid { display: flex; flex-direction: column; gap: 8px; }
  .health-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 12px;
    background: rgba(255,255,255,0.03);
    border-radius: 8px;
    border: 1px solid transparent;
    transition: border-color 0.2s;
  }
  .health-row:hover { border-color: var(--border); }
  .health-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .health-dot.ok      { background: var(--green); box-shadow: 0 0 8px rgba(34,211,160,0.5); }
  .health-dot.warning { background: var(--yellow); }
  .health-dot.error, .health-dot.down { background: var(--red); }
  .health-dot.offline { background: var(--text-dim); }

  .health-name { flex: 1; font-size: 0.82rem; font-weight: 500; text-transform: capitalize; }
  .health-status { font-size: 0.72rem; color: var(--text-dim); font-family: 'JetBrains Mono', monospace; }
  .health-time { font-size: 0.68rem; color: var(--text-dim); }

  /* ── Run History table ── */
  .run-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
  .run-table th {
    text-align: left;
    padding: 6px 10px;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    border-bottom: 1px solid var(--border);
  }
  .run-table td { padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: top; }
  .run-table tr:last-child td { border-bottom: none; }
  .run-table tr:hover td { background: rgba(255,255,255,0.02); }

  .run-status {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.67rem;
    font-weight: 600;
    text-transform: uppercase;
  }
  .run-status.success { background: rgba(34,211,160,0.12); color: var(--green); }
  .run-status.error   { background: rgba(248,113,113,0.12); color: var(--red); }
  .run-status.running { background: rgba(99,120,255,0.15); color: var(--accent); }

  /* ── Posts list ── */
  .post-item {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 9px 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .post-item:last-child { border-bottom: none; }
  .post-lang {
    font-size: 0.6rem;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 4px;
    text-transform: uppercase;
    flex-shrink: 0;
    margin-top: 2px;
  }
  .post-lang.en { background: rgba(56,189,248,0.15); color: var(--blue); }
  .post-lang.hi { background: rgba(251,191,36,0.15); color: var(--yellow); }
  .post-title  { font-size: 0.8rem; font-weight: 500; line-height: 1.35; flex: 1; }
  .post-status { font-size: 0.65rem; color: var(--text-dim); margin-top: 3px; }
  .post-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; margin-top: 6px; }
  .post-dot.draft     { background: var(--yellow); }
  .post-dot.approved  { background: var(--blue); }
  .post-dot.published { background: var(--green); }

  /* ── Error list ── */
  .error-item {
    display: flex;
    gap: 10px;
    padding: 10px 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .error-item:last-child { border-bottom: none; }
  .error-icon { font-size: 16px; flex-shrink: 0; line-height: 1.3; }
  .error-module  { font-size: 0.68rem; font-family: 'JetBrains Mono', monospace; color: var(--accent2); margin-bottom: 2px; }
  .error-msg     { font-size: 0.77rem; color: var(--text-muted); line-height: 1.4; }
  .error-time    { font-size: 0.65rem; color: var(--text-dim); margin-top: 3px; }
  .error-fixed   { font-size: 0.65rem; color: var(--green); margin-top: 2px; }

  /* ── Source chart bars ── */
  .source-list { display: flex; flex-direction: column; gap: 8px; }
  .source-row { display: flex; align-items: center; gap: 10px; font-size: 0.75rem; }
  .source-name { width: 130px; flex-shrink: 0; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .source-bar-wrap { flex: 1; height: 6px; background: rgba(255,255,255,0.06); border-radius: 999px; overflow: hidden; }
  .source-bar { height: 100%; border-radius: 999px; transition: width 0.6s ease; }
  .source-count { font-size: 0.7rem; color: var(--text-dim); width: 24px; text-align: right; }

  /* ── Footer ── */
  footer {
    text-align: center;
    padding: 20px;
    font-size: 0.7rem;
    color: var(--text-dim);
    border-top: 1px solid var(--border);
    margin-top: 8px;
  }

  /* ── Control Buttons ── */
  .ctrl-btns { display: flex; gap: 10px; align-items: center; }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 9px 20px;
    border-radius: 10px;
    font-size: 0.82rem;
    font-weight: 600;
    border: none;
    cursor: pointer;
    transition: all 0.2s cubic-bezier(0.4,0,0.2,1);
    letter-spacing: 0.01em;
    white-space: nowrap;
    position: relative;
    overflow: hidden;
  }
  .btn::after {
    content: '';
    position: absolute;
    inset: 0;
    background: rgba(255,255,255,0.12);
    opacity: 0;
    transition: opacity 0.15s;
  }
  .btn:hover:not(:disabled)::after { opacity: 1; }
  .btn:active:not(:disabled) { transform: scale(0.97); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .btn-start {
    background: linear-gradient(135deg, #22d3a0, #059669);
    color: #fff;
    box-shadow: 0 4px 15px rgba(34,211,160,0.35);
  }
  .btn-start:hover:not(:disabled) {
    box-shadow: 0 6px 20px rgba(34,211,160,0.5);
    transform: translateY(-1px);
  }

  .btn-fresh {
    background: linear-gradient(135deg, #3b82f6, #2563eb);
    color: #fff;
    box-shadow: 0 4px 15px rgba(59,130,246,0.35);
  }
  .btn-fresh:hover:not(:disabled) {
    box-shadow: 0 6px 20px rgba(59,130,246,0.5);
    transform: translateY(-1px);
  }

  .btn-stop {
    background: linear-gradient(135deg, #f87171, #dc2626);
    color: #fff;
    box-shadow: 0 4px 15px rgba(248,113,113,0.35);
  }
  .btn-stop:hover:not(:disabled) {
    box-shadow: 0 6px 20px rgba(248,113,113,0.5);
    transform: translateY(-1px);
  }

  .btn-spin { animation: btnSpin 0.8s linear infinite; }
  @keyframes btnSpin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }

  /* ── Toast notifications ── */
  #toast-wrap {
    position: fixed;
    bottom: 28px;
    right: 28px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 10px;
    pointer-events: none;
  }
  .toast {
    padding: 12px 18px;
    border-radius: 10px;
    font-size: 0.82rem;
    font-weight: 500;
    color: #fff;
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.15);
    pointer-events: auto;
    animation: toastIn 0.3s ease forwards;
    max-width: 320px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .toast.success { background: rgba(34,211,160,0.85); }
  .toast.error   { background: rgba(248,113,113,0.85); }
  .toast.info    { background: rgba(99,120,255,0.85); }
  .toast.out     { animation: toastOut 0.3s ease forwards; }
  @keyframes toastIn  { from { opacity:0; transform: translateY(12px); } to { opacity:1; transform: translateY(0); } }
  @keyframes toastOut { from { opacity:1; transform: translateY(0); } to { opacity:0; transform: translateY(12px); } }

  /* ── Confirm modal ── */
  #modal-overlay {
    display: none;
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.6);
    z-index: 8888;
    backdrop-filter: blur(4px);
    align-items: center;
    justify-content: center;
  }
  #modal-overlay.show { display: flex; }
  #modal-box {
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 28px;
    max-width: 380px;
    width: 90%;
    text-align: center;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
    animation: modalIn 0.25s ease;
  }
  @keyframes modalIn { from { opacity:0; transform: scale(0.92); } to { opacity:1; transform: scale(1); } }
  .modal-icon { font-size: 36px; margin-bottom: 12px; }
  .modal-title { font-size: 1.1rem; font-weight: 700; margin-bottom: 8px; }
  .modal-msg   { font-size: 0.82rem; color: var(--text-muted); margin-bottom: 22px; line-height: 1.5; }
  .modal-btns  { display: flex; gap: 10px; justify-content: center; }
  .modal-btns .btn { min-width: 110px; justify-content: center; }
  .btn-ghost {
    background: rgba(255,255,255,0.06);
    border: 1px solid var(--border);
    color: var(--text-muted);
    padding: 9px 20px;
    border-radius: 10px;
    font-size: 0.82rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s;
  }
  .btn-ghost:hover { background: rgba(255,255,255,0.1); }
  ::-webkit-scrollbar       { width: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; }

  /* empty state */
  .empty { text-align: center; color: var(--text-dim); font-size: 0.78rem; padding: 20px 0; }

  /* Skeleton loader */
  .skel {
    background: linear-gradient(90deg, rgba(255,255,255,0.04) 25%, rgba(255,255,255,0.08) 50%, rgba(255,255,255,0.04) 75%);
    background-size: 200% 100%;
    animation: skeletonPulse 1.5s ease infinite;
    border-radius: 4px;
    height: 14px;
  }
  @keyframes skeletonPulse {
    0%   { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }
</style>
</head>
<body>
<div class="wrap">

  <!-- Toast container -->
  <div id="toast-wrap"></div>

  <!-- Confirm modal -->
  <div id="modal-overlay">
    <div id="modal-box">
      <div class="modal-icon">⏹️</div>
      <div class="modal-title">Stop the pipeline?</div>
      <div class="modal-msg">This will immediately terminate the running pipeline process. Any incomplete step will be abandoned.</div>
      <div class="modal-btns">
        <button class="btn-ghost" onclick="closeModal()">Cancel</button>
        <button class="btn btn-stop" onclick="confirmStop()">Stop Pipeline</button>
      </div>
    </div>
  </div>

  <!-- Header -->
  <header>
    <div class="logo">
      <div class="logo-icon">⚡</div>
      <div class="logo-text">
        <h1>AutoBlog Monitor</h1>
        <p>Real-time pipeline dashboard</p>
      </div>
    </div>
    <div class="header-right">
      <div class="ctrl-btns">
        <button id="btn-fresh" class="btn btn-fresh" onclick="startFresh()" title="Start the pipeline fresh from Step 1">
          <span id="btn-fresh-icon">🔄</span> Restart Fresh
        </button>
        <button id="btn-start" class="btn btn-start" onclick="startPipeline()" title="Resume or start the full pipeline">
          <span id="btn-start-icon">▶</span> Start Pipeline
        </button>
        <button id="btn-stop" class="btn btn-stop" onclick="askStop()" title="Stop the running pipeline" style="display:none">
          <span>⏹</span> Stop
        </button>
      </div>
      <div id="live-badge">Live</div>
      <div id="server-time">--:--:--</div>
    </div>
  </header>

  <!-- ── Row 1: Current Task hero card (full width) ── -->
  <div style="margin-bottom:20px">
    <div class="card" id="current-task-card">
      <div class="task-header">
        <div>
          <div class="card-title"><span>⚙️</span> Currently Running</div>
          <div id="task-name" class="idle">Pipeline is idle</div>
        </div>
        <div id="task-status-badge" class="badge-idle">Idle</div>
      </div>

      <div class="progress-wrap">
        <div id="progress-bar" style="width:0%"></div>
      </div>
      <div class="progress-meta">
        <span id="step-label">Waiting for pipeline...</span>
        <span id="progress-pct">0%</span>
      </div>

      <div class="counter-row">
        <div class="counter-pill cp-topics">
          <span>🔬 Topics (This Run)</span>
          <span class="val" id="c-topics">0</span>
        </div>
        <div class="counter-pill cp-keywords">
          <span>🔑 Keywords (This Run)</span>
          <span class="val" id="c-keywords">0</span>
        </div>
        <div class="counter-pill cp-drafts">
          <span>📝 Drafts (This Run)</span>
          <span class="val" id="c-drafts">0</span>
        </div>
        <div class="counter-pill cp-pub">
          <span>✅ Published (This Run)</span>
          <span class="val" id="c-pub">0</span>
        </div>
      </div>
    </div>
  </div>

  <!-- ── Row 2: Steps timeline + Stats + Log ── -->
  <div class="grid-main" style="margin-bottom:20px">

    <!-- Steps timeline -->
    <div class="card">
      <div class="card-title"><span>🗺️</span> Pipeline Steps</div>
      <div class="steps-wrap" id="steps-list">
        <!-- filled by JS -->
      </div>
    </div>

    <!-- Today stats -->
    <div class="card">
      <div class="card-title"><span>📊</span> Today's Stats</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
        <div class="stat-card">
          <div class="big-num" id="s-topics" style="color:var(--blue)">—</div>
          <div class="sub">Total Topics (Today)</div>
        </div>
        <div class="stat-card">
          <div class="big-num" id="s-keywords" style="color:var(--accent2)">—</div>
          <div class="sub">Keywords</div>
        </div>
        <div class="stat-card">
          <div class="big-num" id="s-pending" style="color:var(--yellow)">—</div>
          <div class="sub">Drafts Pending</div>
        </div>
        <div class="stat-card">
          <div class="big-num" id="s-published" style="color:var(--green)">—</div>
          <div class="sub">Total Published</div>
        </div>
        <div class="stat-card">
          <div class="big-num" id="s-errors" style="color:var(--red)">—</div>
          <div class="sub">Errors Today</div>
        </div>
        <div class="stat-card">
          <div class="big-num" id="s-fixed" style="color:var(--green)">—</div>
          <div class="sub">Auto-Fixed</div>
        </div>
      </div>
    </div>

    <!-- Live log -->
    <div class="card">
      <div class="card-title"><span>📋</span> Live Log</div>
      <div id="log-box"><div class="empty">Waiting for pipeline activity...</div></div>
    </div>

  </div>

  <!-- ── Row 3: Health + Pipeline Runs ── -->
  <div class="grid-middle" style="margin-bottom:20px">

    <!-- Pipeline run history -->
    <div class="card">
      <div class="card-title"><span>🕐</span> Run History</div>
      <div style="overflow-x:auto">
        <table class="run-table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Status</th>
              <th>Message</th>
              <th>Duration</th>
              <th>Time</th>
            </tr>
          </thead>
          <tbody id="runs-body">
            <tr><td colspan="5" class="empty">No runs yet</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Health status -->
    <div class="card">
      <div class="card-title"><span>💚</span> Service Health</div>
      <div class="health-grid" id="health-grid">
        <div class="empty">No health data yet</div>
      </div>
    </div>

  </div>

  <!-- ── Row 4: Posts + Errors + Sources ── -->
  <div class="grid-bottom" style="margin-bottom:20px">

    <!-- Recent posts -->
    <div class="card">
      <div class="card-title"><span>📝</span> Recent Posts</div>
      <div id="posts-list"><div class="empty">No posts yet</div></div>
    </div>

    <!-- Recent errors -->
    <div class="card">
      <div class="card-title"><span>⚠️</span> Recent Errors</div>
      <div id="errors-list"><div class="empty">No errors. All good! 🎉</div></div>
    </div>

    <!-- Topic sources chart -->
    <div class="card">
      <div class="card-title"><span>📡</span> Topic Sources (Today)</div>
      <div class="source-list" id="sources-list"><div class="empty">No data yet</div></div>
    </div>

  </div>

</div><!-- /wrap -->

<footer>
  AutoBlog Engine Monitor &nbsp;•&nbsp; Refreshes every 3s &nbsp;•&nbsp;
  Built with ❤️ &nbsp;•&nbsp; <span id="footer-time"></span>
</footer>

<script>
const STEPS = [
  "Trend Research",
  "Keyword Research",
  "Content Writing",
  "Image Handling",
  "Publishing",
  "Health Check"
];

const STEP_ICONS = {
  "Trend Research":    "🔬",
  "Keyword Research":  "🔑",
  "Content Writing":   "✍️",
  "Image Handling":    "🖼️",
  "Publishing":        "🚀",
  "Health Check":      "💚",
};

// ── Fetch & Render ────────────────────────────────────────────────────────────
// (fetchData is defined at the bottom of this script)

function render(data) {
  const { status, stats, runs, errors, health, posts, sources, kw_sources, server_time } = data;

  // Header time
  const t = new Date(server_time);
  document.getElementById('server-time').textContent = t.toLocaleTimeString();
  document.getElementById('footer-time').textContent = t.toLocaleString();

  renderCurrentTask(status);
  renderSteps(status);
  renderStats(stats);
  renderLog(status.log_lines || []);
  renderRuns(runs || []);
  renderHealth(health || []);
  renderPosts(posts || []);
  renderErrors(errors || []);
  renderSources(sources || [], kw_sources || []);
}

// ── Current Task ──────────────────────────────────────────────────────────────
function renderCurrentTask(s) {
  const nameEl   = document.getElementById('task-name');
  const badge    = document.getElementById('task-status-badge');
  const bar      = document.getElementById('progress-bar');
  const stepLbl  = document.getElementById('step-label');
  const pctLbl   = document.getElementById('progress-pct');

  const pct  = s.progress_pct || 0;
  bar.style.width = pct + '%';
  pctLbl.textContent = pct + '%';

  if (s.status === 'running') {
    nameEl.textContent = s.current_task || 'Initializing...';
    nameEl.className   = '';
    badge.textContent  = '⚡ Running';
    badge.className    = 'badge-running';
    stepLbl.textContent = `Step ${s.current_step} of ${s.total_steps}: ${s.current_task || '...'}`;
  } else if (s.status === 'done') {
    nameEl.textContent = 'Pipeline completed ✅';
    nameEl.className   = '';
    badge.textContent  = '✅ Done';
    badge.className    = 'badge-done';
    stepLbl.textContent = 'Last run completed successfully';
  } else if (s.status === 'error') {
    nameEl.textContent = 'Pipeline error ❌';
    nameEl.className   = 'error';
    badge.textContent  = '❌ Error';
    badge.className    = 'badge-error';
    stepLbl.textContent = s.error || 'Unknown error';
  } else {
    nameEl.textContent = 'Pipeline is idle';
    nameEl.className   = 'idle';
    badge.textContent  = '⏸ Idle';
    badge.className    = 'badge-idle';
    stepLbl.textContent = 'Start the pipeline with: python main.py';
  }

  // Counters
  const c = s.counters || {};
  document.getElementById('c-topics').textContent   = c.topics_found   || 0;
  document.getElementById('c-keywords').textContent = c.keywords_found || 0;
  document.getElementById('c-drafts').textContent   = c.drafts_created || 0;
  document.getElementById('c-pub').textContent      = c.posts_published || 0;
}

// ── Steps Timeline ────────────────────────────────────────────────────────────
function renderSteps(s) {
  const wrap = document.getElementById('steps-list');
  const curStep = s.current_step || 0;
  const status  = s.status;

  wrap.innerHTML = STEPS.map((name, i) => {
    const num   = i + 1;
    let state   = 'pending';
    if (status === 'done') state = 'done';
    else if (status === 'running') {
      if (num < curStep) state = 'done';
      else if (num === curStep) state = 'active';
    }

    const icon = state === 'done'   ? '✓'
               : state === 'active' ? '◎'
               : String(num);

    const badgeLabel = state === 'active' ? 'Running'
                     : state === 'done'   ? 'Done'
                     :                      'Pending';

    return `
      <div class="step-item ${state}">
        <div class="step-dot ${state}">${icon}</div>
        <div class="step-label">${STEP_ICONS[name] || ''} ${name}</div>
        <span class="step-badge ${state}">${badgeLabel}</span>
      </div>`;
  }).join('');
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function renderStats(s) {
  if (!s) return;
  document.getElementById('s-topics').textContent    = s.topics_today ?? '—';
  document.getElementById('s-keywords').textContent  = s.keywords_today ?? '—';
  document.getElementById('s-pending').textContent   = s.drafts_pending ?? '—';
  document.getElementById('s-published').textContent = s.total_published ?? '—';
  document.getElementById('s-errors').textContent    = s.errors_today ?? '—';
  document.getElementById('s-fixed').textContent     = s.auto_fixed_today ?? '—';
}

// ── Log viewer ────────────────────────────────────────────────────────────────
function renderLog(lines) {
  const box = document.getElementById('log-box');
  if (!lines || lines.length === 0) {
    box.innerHTML = '<div class="empty">Waiting for pipeline activity...</div>';
    return;
  }
  box.innerHTML = lines.map(l => {
    const match = l.match(/^\[(\d{2}:\d{2}:\d{2})\] (.*)/);
    if (match) return `<div class="log-line"><span class="ts">${match[1]}</span>${escHtml(match[2])}</div>`;
    return `<div class="log-line">${escHtml(l)}</div>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

// ── Run history ───────────────────────────────────────────────────────────────
function renderRuns(runs) {
  const tbody = document.getElementById('runs-body');
  if (!runs.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">No pipeline runs yet</td></tr>';
    return;
  }
  tbody.innerHTML = runs.map(r => {
    const dur = r.duration_seconds ? r.duration_seconds.toFixed(1) + 's' : '—';
    const ts  = r.completed_at ? new Date(r.completed_at).toLocaleTimeString() : '—';
    const cls = r.status === 'success' ? 'success' : r.status === 'error' ? 'error' : 'running';
    const ico = r.status === 'success' ? '✓' : r.status === 'error' ? '✗' : '◎';
    return `<tr>
      <td style="font-family:monospace;font-size:0.72rem">${r.run_type || '—'}</td>
      <td><span class="run-status ${cls}">${ico} ${r.status}</span></td>
      <td style="color:var(--text-muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(r.message || '—')}</td>
      <td style="font-family:monospace;font-size:0.72rem">${dur}</td>
      <td style="font-size:0.72rem;color:var(--text-dim)">${ts}</td>
    </tr>`;
  }).join('');
}

// ── Health ────────────────────────────────────────────────────────────────────
function renderHealth(rows) {
  const wrap = document.getElementById('health-grid');
  if (!rows.length) {
    wrap.innerHTML = '<div class="empty">No health check data yet</div>';
    return;
  }
  wrap.innerHTML = rows.map(h => {
    const cls = ['ok','warning','error','down'].includes(h.status) ? h.status : 'offline';
    const ts  = h.created_at ? new Date(h.created_at).toLocaleTimeString() : '';
    const ms  = h.response_time_ms ? `${h.response_time_ms.toFixed(0)}ms` : '';
    return `<div class="health-row">
      <div class="health-dot ${cls}"></div>
      <div class="health-name">${escHtml(h.check_type)}</div>
      <div class="health-status">${escHtml(h.details || h.status)}</div>
      <div class="health-time">${ms} ${ts}</div>
    </div>`;
  }).join('');
}

// ── Posts ─────────────────────────────────────────────────────────────────────
function renderPosts(posts) {
  const wrap = document.getElementById('posts-list');
  if (!posts.length) {
    wrap.innerHTML = '<div class="empty">No posts yet</div>';
    return;
  }
  wrap.innerHTML = posts.map(p => {
    const status = p.status || 'draft';
    const ts = p.created_at ? new Date(p.created_at).toLocaleDateString() : '';
    return `<div class="post-item">
      <div class="post-dot ${status}"></div>
      <span class="post-lang ${p.language || 'en'}">${p.language || 'en'}</span>
      <div style="flex:1;min-width:0">
        <div class="post-title">${escHtml(p.title || 'Untitled')}</div>
        <div class="post-status">${status} • ${p.word_count || 0} words • ${ts}</div>
      </div>
    </div>`;
  }).join('');
}

// ── Errors ────────────────────────────────────────────────────────────────────
function renderErrors(errors) {
  const wrap = document.getElementById('errors-list');
  if (!errors.length) {
    wrap.innerHTML = '<div class="empty">No errors. All good! 🎉</div>';
    return;
  }
  wrap.innerHTML = errors.map(e => {
    const ts = e.created_at ? new Date(e.created_at).toLocaleTimeString() : '';
    const fixed = e.auto_fixed ? `<div class="error-fixed">✓ Auto-fixed: ${escHtml(e.fix_action || '')}</div>` : '';
    return `<div class="error-item">
      <div class="error-icon">${e.auto_fixed ? '🔧' : '⚠️'}</div>
      <div>
        <div class="error-module">${escHtml(e.module)}.${escHtml(e.error_type)}</div>
        <div class="error-msg">${escHtml((e.message || '').substring(0, 120))}</div>
        <div class="error-time">${ts}</div>
        ${fixed}
      </div>
    </div>`;
  }).join('');
}

// ── Source bars ───────────────────────────────────────────────────────────────
function renderSources(sources, kwSources) {
  const wrap = document.getElementById('sources-list');
  const all  = sources.length ? sources : kwSources;
  if (!all.length) {
    wrap.innerHTML = '<div class="empty">No data yet</div>';
    return;
  }
  const max = Math.max(...all.map(s => s.count || 0), 1);
  const colors = ['#6378ff','#a78bfa','#38bdf8','#22d3a0','#fbbf24','#f87171'];
  wrap.innerHTML = all.map((s, i) => {
    const pct = Math.round((s.count / max) * 100);
    return `<div class="source-row">
      <div class="source-name" title="${escHtml(s.source)}">${escHtml(s.source)}</div>
      <div class="source-bar-wrap">
        <div class="source-bar" style="width:${pct}%;background:${colors[i % colors.length]}"></div>
      </div>
      <div class="source-count">${s.count}</div>
    </div>`;
  }).join('');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Pipeline Control ──────────────────────────────────────────────────────────
let _procRunning = false;

async function startPipeline() {
  const btn = document.getElementById('btn-start');
  const ico = document.getElementById('btn-start-icon');
  btn.disabled = true;
  ico.className = 'btn-spin';
  ico.textContent = '↻';
  try {
    const r = await fetch('/api/pipeline/start', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      showToast('success', '▶ ' + d.message);
    } else {
      showToast('error', '✗ ' + d.message);
      btn.disabled = false;
    }
  } catch(e) {
    showToast('error', 'Could not reach monitor server');
    btn.disabled = false;
  } finally {
    ico.className = '';
    ico.textContent = '▶';
  }
  // Force immediate refresh
  setTimeout(fetchData, 800);
}

async function startFresh() {
  const btn = document.getElementById('btn-fresh');
  const ico = document.getElementById('btn-fresh-icon');
  btn.disabled = true;
  ico.className = 'btn-spin';
  try {
    const r = await fetch('/api/pipeline/start_fresh', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      showToast('success', '🔄 ' + d.message);
    } else {
      showToast('error', '✗ ' + d.message);
    }
  } catch(e) {
    showToast('error', 'Could not reach monitor server');
  } finally {
    btn.disabled = false;
    ico.className = '';
  }
  setTimeout(fetchData, 800);
}

function askStop() {
  document.getElementById('modal-overlay').classList.add('show');
}
function closeModal() {
  document.getElementById('modal-overlay').classList.remove('show');
}
async function confirmStop() {
  closeModal();
  const btn = document.getElementById('btn-stop');
  btn.disabled = true;
  btn.textContent = '⏳ Stopping...';
  try {
    const r = await fetch('/api/pipeline/stop', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      showToast('success', '⏹ ' + d.message);
    } else {
      showToast('error', '✗ ' + d.message);
    }
  } catch(e) {
    showToast('error', 'Could not reach monitor server');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span>⏹</span> Stop';
  }
  setTimeout(fetchData, 800);
}

function updateButtons(procRunning, pipelineStatus) {
  const startBtn = document.getElementById('btn-start');
  const freshBtn = document.getElementById('btn-fresh');
  const stopBtn  = document.getElementById('btn-stop');
  const isRunning = procRunning || pipelineStatus === 'running';
  if (isRunning) {
    startBtn.style.display = 'none';
    freshBtn.style.display = 'none';
    stopBtn.style.display  = 'inline-flex';
    stopBtn.disabled = false;
  } else {
    // If it was interrupted, Start becomes Resume
    if (pipelineStatus === 'running' || pipelineStatus === 'error') {
      document.getElementById('btn-start-icon').textContent = '▶';
      startBtn.innerHTML = '<span id="btn-start-icon">▶</span> Resume Pipeline';
    } else {
      startBtn.innerHTML = '<span id="btn-start-icon">▶</span> Start Pipeline';
    }
    startBtn.style.display = 'inline-flex';
    freshBtn.style.display = 'inline-flex';
    startBtn.disabled = false;
    freshBtn.disabled = false;
    stopBtn.style.display  = 'none';
  }
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function showToast(type, msg, ms = 4000) {
  const wrap  = document.getElementById('toast-wrap');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = msg;
  wrap.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('out');
    setTimeout(() => toast.remove(), 350);
  }, ms);
}

// ── Modal close on backdrop click ─────────────────────────────────────────────
document.getElementById('modal-overlay').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeModal();
});

// ── Init & poll ───────────────────────────────────────────────────────────────
async function fetchData() {
  try {
    const [dataRes, procRes] = await Promise.all([
      fetch('/api/data'),
      fetch('/api/pipeline/proc'),
    ]);
    const data = await dataRes.json();
    const proc = await procRes.json();
    render(data);
    updateButtons(proc.running, data.status?.status);
    _procRunning = proc.running;
  } catch(e) {
    console.warn('Fetch failed:', e);
  }
}

fetchData();
setInterval(fetchData, 3000);
</script>
</body>
</html>
"""


# ── Server entry point ────────────────────────────────────────────────────────

def start_server(port: int = DASHBOARD_PORT):
    """Start the monitoring HTTP server (threaded for concurrent requests)."""
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    except Exception:
        # Fallback if ThreadingHTTPServer not available
        server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"\n  [AutoBlog Monitor] Dashboard  ->  http://localhost:{port}")
    print(f"  [AutoBlog Monitor] API data   ->  http://localhost:{port}/api/data")
    print(f"  [AutoBlog Monitor] Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMonitor stopped.")



def main():
    parser = argparse.ArgumentParser(description="AutoBlog Engine — Monitoring Dashboard")
    parser.add_argument("--port", type=int, default=DASHBOARD_PORT, help="HTTP port (default: 5000)")
    args = parser.parse_args()

    # Ensure DB exists
    try:
        import db
        db.init_database()
    except Exception:
        pass

    start_server(args.port)


if __name__ == "__main__":
    main()
