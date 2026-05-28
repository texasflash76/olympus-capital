from trade_thesis_store import get_all_trade_theses
import os
import json
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone
from html import escape

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from logger import DB_PATH, init_db
from orchestrator import run_one_ticker
from screener import screen_market
from recommender import run_recommender
from tools.broker import (
    get_account_summary as fetch_account_summary,
    get_positions as fetch_positions,
)
from tools.web_research import get_web_research
from fast_scan import run_fast_scan, get_fast_scan_results, get_fast_scan_status
from deep_review import run_deep_review, get_deep_review_results, get_deep_review_status
from performance_tracker import (
    PERFORMANCE_STATUS_PATH,
    update_performance,
    get_outcomes,
    summarize_outcomes,
)
from tools.sector_intelligence import (
    CANONICAL_SECTORS,
    SECTOR_INTELLIGENCE_PATH,
    SECTOR_SCAN_RESULTS_PATH,
    refresh_all_sector_intelligence,
    get_cached_sector_intelligence,
    scan_sector,
)

from position_monitor import review_all_positions, get_position_review, execute_position_sell
from ai_position_review import run_ai_position_review, get_ai_position_review, execute_ai_sell

app = FastAPI(title="Olympus Capital Dashboard")


SCREENER_RESULTS_PATH = Path("screener_results.json")
SCREENER_STATUS_PATH = Path("screener_status.json")
screener_lock = threading.Lock()

RECOMMENDER_RESULTS_PATH = Path("recommender_results.json")
RECOMMENDER_STATUS_PATH = Path("recommender_status.json")
RECOMMENDER_HISTORY_PATH = Path("recommender_history.json")
recommender_lock = threading.Lock()

performance_lock = threading.Lock()

fast_scan_lock = threading.Lock()
deep_review_lock = threading.Lock()

ACCOUNT_CACHE = {
    "account": None,
    "positions": None,
    "cached_at": None,
}
ACCOUNT_CACHE_SECONDS = 60

sector_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_cached_account_and_positions():
    """
    Avoids hitting Alpaca every single time the dashboard page loads.
    This makes navigation back to / much faster.
    """
    now = datetime.now(timezone.utc)
    cached_at = ACCOUNT_CACHE.get("cached_at")

    if (
        cached_at is not None
        and ACCOUNT_CACHE.get("account") is not None
        and ACCOUNT_CACHE.get("positions") is not None
        and (now - cached_at).total_seconds() < ACCOUNT_CACHE_SECONDS
    ):
        return ACCOUNT_CACHE["account"], ACCOUNT_CACHE["positions"]

    account = fetch_account_summary()
    positions = fetch_positions()

    ACCOUNT_CACHE["account"] = account
    ACCOUNT_CACHE["positions"] = positions
    ACCOUNT_CACHE["cached_at"] = now

    return account, positions


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def read_json(path: Path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def safe_json_loads(value):
    try:
        if value is None:
            return {}
        return json.loads(value)
    except Exception:
        return {"raw": value}


def money(value):
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "$0.00"


def pct(value):
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return "0.0%"


def status_class(status):
    status = str(status)

    if "PAPER_TRADE_SUBMITTED" in status:
        return "approved"
    if "RECOMMENDED_NOT_EXECUTED" in status:
        return "recommended"
    if "APPROVED" in status:
        return "approved"
    if "VETOED" in status:
        return "vetoed"
    if "BLOCKED" in status:
        return "blocked"
    if "ERROR" in status:
        return "error"

    return "neutral"


def badge(label, css_class="neutral"):
    return f'<span class="badge {css_class}">{escape(str(label))}</span>'


def parse_max_symbols(value: str):
    value = str(value).strip().lower()

    if value in ["", "all", "none"]:
        return None

    return int(value)


def shared_css():
    return """
    <style>
        :root {
            --bg: #070b14;
            --bg-soft: #0b1220;
            --panel: rgba(17, 24, 39, 0.86);
            --panel-solid: #111827;
            --panel-2: #020617;
            --border: rgba(148, 163, 184, 0.18);
            --border-strong: rgba(148, 163, 184, 0.30);
            --text: #f8fafc;
            --muted: #94a3b8;
            --muted-2: #64748b;
            --blue: #3b82f6;
            --blue-dark: #1d4ed8;
            --green: #22c55e;
            --green-dark: #15803d;
            --red: #ef4444;
            --red-dark: #b91c1c;
            --yellow: #f59e0b;
            --purple: #8b5cf6;
            --shadow: 0 24px 70px rgba(0, 0, 0, 0.35);
            --radius: 20px;
        }

        * {
            box-sizing: border-box;
        }

        html {
            scroll-behavior: smooth;
        }

        body {
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
            background:
                radial-gradient(circle at top left, rgba(59, 130, 246, 0.20), transparent 34%),
                radial-gradient(circle at top right, rgba(34, 197, 94, 0.12), transparent 30%),
                linear-gradient(180deg, #070b14 0%, #0f172a 100%);
            color: var(--text);
            margin: 0;
            padding: 32px;
            min-height: 100vh;
        }

        a {
            color: #93c5fd;
            text-decoration: none;
        }

        a:hover {
            text-decoration: underline;
        }

        h1 {
            font-size: clamp(34px, 5vw, 56px);
            margin: 0 0 10px 0;
            letter-spacing: -0.055em;
            line-height: 0.95;
        }

        h2 {
            margin-top: 0;
            letter-spacing: -0.025em;
        }

        h3 {
            margin-top: 0;
            letter-spacing: -0.015em;
        }

        p {
            line-height: 1.55;
        }

        .page-header {
            max-width: 1180px;
            margin: 0 auto 26px auto;
            padding: 8px 0;
        }

        .subtitle {
            color: var(--muted);
            margin-bottom: 24px;
            max-width: 980px;
            line-height: 1.6;
            font-size: 16px;
        }

        .card {
            background: var(--panel);
            backdrop-filter: blur(18px);
            -webkit-backdrop-filter: blur(18px);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 24px;
            margin: 0 auto 22px auto;
            box-shadow: var(--shadow);
            max-width: 1180px;
        }

        .card-soft {
            background:
                linear-gradient(180deg, rgba(15, 23, 42, 0.98), rgba(2, 6, 23, 0.96));
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 20px;
            box-shadow: 0 12px 38px rgba(0,0,0,0.22);
            transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
        }

        .card-soft:hover {
            transform: translateY(-2px);
            border-color: var(--border-strong);
            box-shadow: 0 18px 46px rgba(0,0,0,0.30);
        }

        .grid {
            display: grid;
            gap: 16px;
            max-width: 1180px;
            margin-left: auto;
            margin-right: auto;
        }

        .grid-2 {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .grid-3 {
            grid-template-columns: repeat(3, minmax(0, 1fr));
        }

        .grid-4 {
            grid-template-columns: repeat(4, minmax(0, 1fr));
        }

        .tool-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(285px, 1fr));
            gap: 16px;
        }

        .metric-title {
            color: var(--muted);
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 10px;
        }

        .big-number {
            font-size: 30px;
            font-weight: 850;
            letter-spacing: -0.045em;
        }

        .muted {
            color: var(--muted);
        }

        .small {
            font-size: 14px;
        }

        .section-title {
            max-width: 1180px;
            margin: 34px auto 14px auto;
            font-size: 28px;
            letter-spacing: -0.035em;
        }

        .button-row {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
            margin-top: 16px;
        }

        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            padding: 12px 18px;
            border-radius: 12px;
            text-decoration: none;
            font-weight: 800;
            color: white;
            border: 1px solid transparent;
            cursor: pointer;
            font-size: 15px;
            transition: transform 0.15s ease, filter 0.15s ease, box-shadow 0.15s ease;
            box-shadow: 0 10px 24px rgba(0,0,0,0.22);
        }

        .btn:hover {
            transform: translateY(-1px);
            filter: brightness(1.06);
            text-decoration: none;
        }

        .btn-blue {
            background: linear-gradient(135deg, var(--blue), var(--blue-dark));
        }

        .btn-green {
            background: linear-gradient(135deg, var(--green), var(--green-dark));
        }

        .btn-red {
            background: linear-gradient(135deg, var(--red), var(--red-dark));
        }

        .btn-dark {
            background: linear-gradient(135deg, #334155, #0f172a);
            border-color: var(--border);
        }

        input, button, select {
            padding: 12px 13px;
            border-radius: 12px;
            border: 1px solid var(--border);
            background: rgba(2, 6, 23, 0.80);
            color: var(--text);
            font-size: 15px;
            margin: 6px 8px 6px 0;
            outline: none;
        }

        input:focus, select:focus {
            border-color: rgba(59, 130, 246, 0.80);
            box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.15);
        }

        button {
            cursor: pointer;
            background: linear-gradient(135deg, var(--blue), var(--blue-dark));
            color: white;
            font-weight: 800;
            border: none;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(2, 6, 23, 0.72);
            border: 1px solid var(--border);
            border-radius: 16px;
            overflow: hidden;
            margin-top: 14px;
            box-shadow: 0 12px 32px rgba(0,0,0,0.18);
        }

        th, td {
            padding: 13px;
            border-bottom: 1px solid rgba(148, 163, 184, 0.12);
            text-align: left;
            vertical-align: top;
        }

        th {
            color: #bfdbfe;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.07em;
            background: rgba(15, 23, 42, 0.95);
        }

        tr:hover td {
            background: rgba(15, 23, 42, 0.48);
        }

        pre {
            white-space: pre-wrap;
            font-size: 12px;
            background: rgba(2, 6, 23, 0.82);
            padding: 14px;
            border-radius: 14px;
            max-height: 260px;
            overflow: auto;
            border: 1px solid var(--border);
        }

        .badge {
            display: inline-flex;
            align-items: center;
            padding: 6px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 850;
            white-space: nowrap;
            border: 1px solid transparent;
        }

        .approved {
            background: rgba(34, 197, 94, 0.14);
            color: #bbf7d0;
            border-color: rgba(34, 197, 94, 0.30);
        }

        .recommended {
            background: rgba(20, 184, 166, 0.14);
            color: #99f6e4;
            border-color: rgba(20, 184, 166, 0.30);
        }

        .vetoed {
            background: rgba(245, 158, 11, 0.15);
            color: #fde68a;
            border-color: rgba(245, 158, 11, 0.32);
        }

        .blocked {
            background: rgba(239, 68, 68, 0.15);
            color: #fecaca;
            border-color: rgba(239, 68, 68, 0.32);
        }

        .error {
            background: rgba(100, 116, 139, 0.18);
            color: #f1f5f9;
            border-color: rgba(148, 163, 184, 0.22);
        }

        .neutral {
            background: rgba(59, 130, 246, 0.13);
            color: #dbeafe;
            border-color: rgba(59, 130, 246, 0.26);
        }

        .progress-shell {
            width: 100%;
            height: 32px;
            background: rgba(2, 6, 23, 0.90);
            border-radius: 999px;
            overflow: hidden;
            border: 1px solid var(--border);
            margin: 18px 0;
            box-shadow: inset 0 2px 10px rgba(0,0,0,0.35);
        }

        .progress-bar {
            height: 100%;
            background: linear-gradient(90deg, #2563eb, #22c55e);
            transition: width 0.35s ease;
            border-radius: 999px;
        }

        .chart {
            height: 240px;
        }

        .chart-row {
            display: flex;
            align-items: end;
            gap: 6px;
            height: 170px;
            border-left: 1px solid var(--border);
            border-bottom: 1px solid var(--border);
            padding-left: 8px;
        }

        .bar {
            flex: 1;
            background: linear-gradient(180deg, #60a5fa, #2563eb);
            min-height: 4px;
            border-radius: 8px 8px 0 0;
            opacity: 0.92;
        }

        .callout,
        .warning-callout,
        .success-callout,
        .danger-callout {
            padding: 14px 16px;
            border-radius: 14px;
            line-height: 1.55;
            border: 1px solid transparent;
        }

        .callout {
            border-left: 4px solid #3b82f6;
            background: rgba(37, 99, 235, 0.12);
            color: #bfdbfe;
            border-color: rgba(59, 130, 246, 0.18);
        }

        .warning-callout {
            border-left: 4px solid var(--yellow);
            background: rgba(245, 158, 11, 0.12);
            color: #fde68a;
            border-color: rgba(245, 158, 11, 0.18);
        }

        .success-callout {
            border-left: 4px solid var(--green);
            background: rgba(22, 163, 74, 0.12);
            color: #bbf7d0;
            border-color: rgba(34, 197, 94, 0.18);
        }

        .danger-callout {
            border-left: 4px solid var(--red);
            background: rgba(220, 38, 38, 0.12);
            color: #fecaca;
            border-color: rgba(239, 68, 68, 0.18);
        }

        .tool-number {
            width: 34px;
            height: 34px;
            border-radius: 999px;
            background: linear-gradient(135deg, #2563eb, #7c3aed);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 900;
            margin-right: 10px;
            box-shadow: 0 8px 18px rgba(37, 99, 235, 0.25);
        }

        .empty {
            color: var(--muted);
            font-style: italic;
        }

        @media (max-width: 1000px) {
            .grid-4, .grid-3, .grid-2 {
                grid-template-columns: 1fr;
            }

            body {
                padding: 18px;
            }

            h1 {
                font-size: 36px;
            }

            table {
                display: block;
                overflow-x: auto;
            }
        }
    </style>
    """


@app.middleware("http")
async def password_protect_dashboard(request: Request, call_next):
    expected_password = os.getenv("DASHBOARD_PASSWORD")

    if not expected_password:
        return await call_next(request)

    if request.url.path == "/health":
        return await call_next(request)

    provided_password = request.query_params.get("password")
    saved_password = request.cookies.get("dashboard_password")

    if provided_password == expected_password:
        response = await call_next(request)
        response.set_cookie(
            key="dashboard_password",
            value=provided_password,
            httponly=True,
            max_age=60 * 60 * 12,
        )
        return response

    if saved_password == expected_password:
        return await call_next(request)

    login_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Capital Login</title>
        {shared_css()}
    </head>
    <body>
        <div style="max-width: 440px; margin: 90px auto;">
            <div class="card">
                <h1>Olympus Capital</h1>
                <p class="muted">Enter the dashboard password to continue.</p>
                <form method="get" action="/">
                    <input
                        type="password"
                        name="password"
                        placeholder="Dashboard password"
                        required
                        style="width: 100%; margin-bottom: 12px;"
                    >
                    <button type="submit" class="btn btn-blue" style="width:100%;">
                        Enter Dashboard
                    </button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=login_html, status_code=401)


def ensure_snapshot_table():
    init_db()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            portfolio_value REAL NOT NULL,
            cash REAL NOT NULL,
            buying_power REAL NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def save_portfolio_snapshot(account):
    ensure_snapshot_table()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO portfolio_snapshots (
            timestamp,
            portfolio_value,
            cash,
            buying_power
        )
        VALUES (?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        float(account.get("portfolio_value", 0)),
        float(account.get("cash", 0)),
        float(account.get("buying_power", 0)),
    ))

    conn.commit()
    conn.close()


def get_recent_trade_logs(limit=20):
    init_db()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            timestamp,
            ticker,
            research_brief,
            quant_signal,
            pm_decision,
            risk_result,
            final_status
        FROM trade_logs
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    logs = []

    for row in rows:
        (
            log_id,
            timestamp,
            ticker,
            research_brief,
            quant_signal,
            pm_decision,
            risk_result,
            final_status,
        ) = row

        logs.append({
            "id": log_id,
            "timestamp": timestamp,
            "ticker": ticker,
            "research_brief": safe_json_loads(research_brief),
            "quant_signal": safe_json_loads(quant_signal),
            "pm_decision": safe_json_loads(pm_decision),
            "risk_result": safe_json_loads(risk_result),
            "final_status": final_status,
        })

    return logs


def get_today_trade_logs():
    today = datetime.now().date().isoformat()
    logs = get_recent_trade_logs(limit=100)

    return [
        log for log in logs
        if str(log.get("timestamp", "")).startswith(today)
    ]


def get_portfolio_history(limit=30):
    ensure_snapshot_table()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT timestamp, portfolio_value
        FROM portfolio_snapshots
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    rows = list(reversed(rows))

    return [
        {
            "timestamp": row[0],
            "portfolio_value": row[1],
        }
        for row in rows
    ]


def summarize_logs(logs):
    recommended = 0
    submitted = 0
    vetoed = 0
    blocked = 0
    errors = 0

    for log in logs:
        status = str(log.get("final_status", ""))

        if "PAPER_TRADE_SUBMITTED" in status:
            submitted += 1
        elif "RECOMMENDED_NOT_EXECUTED" in status:
            recommended += 1
        elif "VETOED" in status:
            vetoed += 1
        elif "BLOCKED" in status:
            blocked += 1
        elif "ERROR" in status:
            errors += 1

    return {
        "recommended": recommended,
        "submitted": submitted,
        "vetoed": vetoed,
        "blocked": blocked,
        "errors": errors,
        "total": len(logs),
    }


def build_portfolio_chart(portfolio_history):
    if len(portfolio_history) == 0:
        return "<p class='empty'>No portfolio snapshots yet.</p>"

    values = [float(point["portfolio_value"]) for point in portfolio_history]
    max_value = max(values)
    min_value = min(values)
    range_value = max_value - min_value

    bars = ""

    for point in portfolio_history:
        value = float(point["portfolio_value"])

        if range_value == 0:
            height = 80
        else:
            height = 20 + ((value - min_value) / range_value * 140)

        title = f"{point.get('timestamp', '')}: {money(value)}"

        bars += f"""
            <div
                class="bar"
                title="{escape(title)}"
                style="height: {height}px;"
            ></div>
        """

    latest_value = portfolio_history[-1]["portfolio_value"]

    return f"""
        <div class="chart-row">
            {bars}
        </div>
        <p class="muted">Latest snapshot: {money(latest_value)}</p>
    """


def run_screener_background(top_n=25, max_symbols=None):
    if screener_lock.locked():
        return

    with screener_lock:
        write_json(SCREENER_STATUS_PATH, {
            "status": "running",
            "current": 0,
            "total": 0,
            "percent": 0,
            "current_ticker": None,
            "message": "Market screener is running.",
            "started_at": now_iso(),
            "finished_at": None,
            "error": None,
            "top_n": top_n,
            "max_symbols": max_symbols,
        })

        try:
            candidates = screen_market(top_n=top_n, max_symbols=max_symbols)

            write_json(SCREENER_RESULTS_PATH, {
                "generated_at": now_iso(),
                "top_n": top_n,
                "max_symbols": max_symbols,
                "candidates": candidates,
            })

            status = read_json(SCREENER_STATUS_PATH, {})
            status.update({
                "status": "complete",
                "current_ticker": None,
                "message": f"Screen complete. Found {len(candidates)} top candidate(s).",
                "finished_at": now_iso(),
                "error": None,
                "top_n": top_n,
                "max_symbols": max_symbols,
            })
            write_json(SCREENER_STATUS_PATH, status)

        except Exception as e:
            status = read_json(SCREENER_STATUS_PATH, {})
            status.update({
                "status": "error",
                "message": "Market screener failed.",
                "current_ticker": None,
                "finished_at": now_iso(),
                "error": str(e),
                "top_n": top_n,
                "max_symbols": max_symbols,
            })
            write_json(SCREENER_STATUS_PATH, status)


def run_recommender_background(top_screener_n=10, final_n=5, max_symbols=None):
    if recommender_lock.locked():
        return

    with recommender_lock:
        try:
            write_json(RECOMMENDER_STATUS_PATH, {
                "batch_id": None,
                "status": "running",
                "current": 0,
                "total": top_screener_n,
                "percent": 0,
                "current_ticker": None,
                "message": "Full AI recommendation scan is starting.",
                "updated_at": now_iso(),
                "top_screener_n": top_screener_n,
                "final_n": final_n,
                "max_symbols": max_symbols,
            })

            run_recommender(
                top_screener_n=top_screener_n,
                final_n=final_n,
                max_symbols=max_symbols,
            )

        except Exception as e:
            status = read_json(RECOMMENDER_STATUS_PATH, {})
            status.update({
                "status": "error",
                "message": str(e),
                "updated_at": now_iso(),
                "top_screener_n": top_screener_n,
                "final_n": final_n,
                "max_symbols": max_symbols,
            })
            write_json(RECOMMENDER_STATUS_PATH, status)



NEWS_HEADLINE_CACHE = {}


def render_ticker_headlines(ticker: str):
    """
    Fast homepage headline renderer.

    The old version fetched live web research for every ticker on the homepage.
    That made going back to the dashboard slow. Homepage now avoids live web calls
    by default. Set DASHBOARD_LIVE_HEADLINES=true if you want the old behavior.
    """
    ticker = str(ticker or "").upper().strip()

    if not ticker:
        return "<span class='empty'>No ticker found.</span>"

    live_headlines_enabled = os.getenv("DASHBOARD_LIVE_HEADLINES", "false").lower() == "true"

    if not live_headlines_enabled:
        return (
            "<span class='muted small'>Live headlines disabled on homepage for speed. "
            "Use Recommendations or Sector Intelligence for headline review.</span>"
        )

    now = datetime.now(timezone.utc)
    cached = NEWS_HEADLINE_CACHE.get(ticker)

    if cached:
        cached_at = cached.get("cached_at")
        html = cached.get("html")
        if cached_at and html and (now - cached_at).total_seconds() < 1800:
            return html

    try:
        data = get_web_research(ticker, limit=3)
        headline_items = []

        for item in data.get("yahoo_news", []):
            title = escape(str(item.get("title", "")))
            link = escape(str(item.get("link", "")))
            if title and link:
                headline_items.append(
                    f'<li><a href="{link}" target="_blank">{title}</a></li>'
                )

        for item in data.get("google_news", []):
            title = escape(str(item.get("title", "")))
            link = escape(str(item.get("link", "")))
            if title and link:
                headline_items.append(
                    f'<li><a href="{link}" target="_blank">{title}</a></li>'
                )

        if headline_items:
            html = "<ul>" + "".join(headline_items[:5]) + "</ul>"
        else:
            html = "<span class='empty'>No recent headlines found.</span>"

    except Exception as e:
        html = f"<span class='empty'>Could not load headlines: {escape(str(e))}</span>"

    NEWS_HEADLINE_CACHE[ticker] = {
        "cached_at": now,
        "html": html,
    }

    return html


@app.get("/", response_class=HTMLResponse)
def dashboard_home():
    account, positions = get_cached_account_and_positions()

    save_portfolio_snapshot(account)

    recent_logs = get_recent_trade_logs(limit=20)
    today_logs = get_today_trade_logs()
    portfolio_history = get_portfolio_history(limit=30)
    summary = summarize_logs(today_logs)

    paper_enabled = os.getenv("PAPER_TRADING_ENABLED", "false").lower() == "true"
    paper_status = "Enabled" if paper_enabled else "Disabled"
    paper_css = "approved" if paper_enabled else "vetoed"

    position_rows = ""

    if len(positions) == 0:
        position_rows = "<p class='empty'>No open positions.</p>"
    else:
        rows = ""

        for position in positions:
            rows += f"""
                <tr>
                    <td>{escape(str(position.get("symbol", "")))}</td>
                    <td>{escape(str(position.get("qty", "")))}</td>
                    <td>{money(position.get("market_value", 0))}</td>
                    <td>{money(position.get("unrealized_pl", 0))}</td>
                </tr>
            """

        position_rows = f"""
            <table>
                <tr>
                    <th>Symbol</th>
                    <th>Qty</th>
                    <th>Market Value</th>
                    <th>Unrealized P/L</th>
                </tr>
                {rows}
            </table>
        """

    log_rows = ""

    if len(recent_logs) == 0:
        log_rows = "<p class='empty'>No trade logs yet.</p>"
    else:
        rows = ""

        for log in recent_logs:
            pm_reasoning = log.get("pm_decision", {}).get("reasoning", "No PM reasoning found.")
            risk_reasons = log.get("risk_result", {}).get("reasons", [])

            risk_html = ""
            if risk_reasons:
                risk_html = "<ul>" + "".join(
                    f"<li>{escape(str(reason))}</li>"
                    for reason in risk_reasons
                ) + "</ul>"
            else:
                risk_html = "<span class='empty'>No risk reasons found.</span>"

            status = log.get("final_status", "")
            css_class = status_class(status)
            news_html = render_ticker_headlines(log.get("ticker", ""))

            rows += f"""
                <tr>
                    <td>{log.get("id", "")}</td>
                    <td>{escape(str(log.get("timestamp", "")))}</td>
                    <td>{escape(str(log.get("ticker", "")))}</td>
                    <td>{badge(status, css_class)}</td>
                    <td>{escape(str(pm_reasoning))}</td>
                    <td>{news_html}</td>
                    <td>{risk_html}</td>
                </tr>
            """

        log_rows = f"""
            <table>
                <tr>
                    <th>ID</th>
                    <th>Time</th>
                    <th>Ticker</th>
                    <th>Status</th>
                    <th>PM Reasoning</th>
                    <th>Recent Headlines</th>
                    <th>Risk Result</th>
                </tr>
                {rows}
            </table>
        """

    latest_raw = ""

    if len(recent_logs) > 0:
        latest = recent_logs[0]

        latest_raw = f"""
            <h2 class="section-title">Raw Latest Log Detail</h2>

            <div class="card">
                <h2>Research Brief</h2>
                <pre>{escape(json.dumps(latest.get("research_brief", {}), indent=2))}</pre>

                <h2>Quant Signal</h2>
                <pre>{escape(json.dumps(latest.get("quant_signal", {}), indent=2))}</pre>

                <h2>PM Decision</h2>
                <pre>{escape(json.dumps(latest.get("pm_decision", {}), indent=2))}</pre>

                <h2>Risk Result</h2>
                <pre>{escape(json.dumps(latest.get("risk_result", {}), indent=2))}</pre>
            </div>
        """

    chart_html = build_portfolio_chart(portfolio_history)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Capital Dashboard</title>
        {shared_css()}
    </head>

    <body>
        <div class="page-header">
            <h1>Olympus Capital Dashboard</h1>
            <p class="subtitle">
                Paper trading dashboard for market discovery, AI recommendations, single-ticker trade tests,
                risk checks, portfolio state, and audit logs.
            </p>
        </div>

        <div class="grid grid-4">
            <div class="card">
                <div class="metric-title">Portfolio Value</div>
                <div class="big-number">{money(account.get("portfolio_value", 0))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Cash</div>
                <div class="big-number">{money(account.get("cash", 0))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Buying Power</div>
                <div class="big-number">{money(account.get("buying_power", 0))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Paper Trading</div>
                <div class="big-number">{badge(paper_status, paper_css)}</div>
                <p class="muted small">Single-ticker trade tests may submit paper orders only if enabled.</p>
            </div>
        </div>

        <section class="card">
            <h2>Discovery and Trading Tools</h2>
            <p class="subtitle">
                Use these tools in order. The screener finds technical setups, the recommender researches and ranks ideas,
                and the single-ticker test is the only mode that may submit a paper trade.
            </p>

            <div class="button-row" style="margin-bottom: 18px;">
                <a href="/fast-scan" class="btn btn-blue">Fast Scan</a>
                <a href="/deep-review" class="btn btn-green">Deep AI Review</a>
                <a href="/positions" class="btn btn-red">Position Monitor</a>
                <a href="/performance" class="btn btn-dark">Performance Tracker</a>
            </div>

            <div class="tool-grid">
                <div class="card-soft">
                    <h3><span class="tool-number">1</span>Market Screener</h3>
                    <p>
                        Scans the tradable stock universe from Alpaca and finds stocks with strong technical setups.
                    </p>
                    <p class="muted small">
                        Uses RSI, MACD, volume ratio, Bollinger Bands, and basic filters. This is only the first filter,
                        not a final trade decision.
                    </p>
                    <div class="button-row">
                        <a href="/screener-results" class="btn btn-blue">Open Screener</a>
                    </div>
                </div>

                <div class="card-soft">
                    <h3><span class="tool-number">2</span>Full AI Recommendation Scan</h3>
                    <p>
                        Takes top screener candidates and runs web research, Research Analyst, Quant Analyst,
                        Portfolio Manager, and Risk Engine.
                    </p>
                    <p class="muted small">
                        Use this to find ranked trade ideas with reasoning. This mode does not submit trades.
                    </p>
                    <div class="button-row">
                        <a href="/recommendations" class="btn btn-green">Find Recommendations</a>
                    </div>
                </div>

                <div class="card-soft">
                    <h3><span class="tool-number">P</span>Performance Tracker</h3>
                    <p>
                        Tracks how past recommendations performed after they were generated.
                    </p>
                    <p class="muted small">
                        Use this to measure win rate, average return, drawdown, and whether Olympus is actually finding useful ideas.
                    </p>
                    <div class="button-row">
                        <a href="/performance" class="btn btn-blue">Open Performance</a>
                        <a href="/positions/ai" class="btn btn-red">AI Position Review</a>
                    </div>
                </div>

                <div class="card-soft">
                    <h3><span class="tool-number">3</span>Sector Intelligence</h3>
                    <p>
                        Reviews sector-level sentiment and headlines so you can choose where to scan before running stock analysis.
                    </p>
                    <p class="muted small">
                        Use this to decide whether Technology, Energy, Financials, Healthcare, or another sector has the strongest setup.
                    </p>
                    <div class="button-row">
                        <a href="/sectors" class="btn btn-green">Open Sector Scanner</a>
                    </div>
                </div>

                <div class="card-soft">
                    <h3><span class="tool-number">4</span>Single-Ticker AI Trade Test</h3>
                    <p>
                        Submit one ticker to the full Olympus system and let the AI decide whether to trade or veto.
                    </p>
                    <p class="muted small">
                        This is the execution-capable mode. If paper trading is enabled and the trade is approved,
                        it may submit an Alpaca paper order.
                    </p>
                    <div class="button-row">
                        <a href="#run-ticker" class="btn btn-red">Run Ticker Trade Test</a>
                    </div>
                </div>
            </div>
        </section>

        <section class="card" id="run-ticker">
            <h2>Single-Ticker AI Trade Test</h2>
            <div class="danger-callout">
                This mode is different from the recommendation scan. Here, you submit one ticker, and Olympus runs the full
                AI process. If PM and risk approve and PAPER_TRADING_ENABLED=true, this mode may submit an Alpaca paper order.
            </div>

            <form method="post" action="/run-ticker" style="margin-top: 16px;">
                <input
                    type="text"
                    name="ticker"
                    placeholder="Enter ticker, e.g. TSLA"
                    required
                >
                <button type="submit">Run AI Trade Test</button>
            </form>

            <p class="muted small">
                Flow: Research Analyst → Quant Analyst → Portfolio Manager → Risk Engine → optional paper execution → logged result.
            </p>
        </section>

        <h2 class="section-title">Today's Decision Summary</h2>

        <div class="grid grid-4">
            <div class="card">
                <div class="metric-title">Total Decisions</div>
                <div class="big-number">{summary["total"]}</div>
            </div>

            <div class="card">
                <div class="metric-title">Recommended Only</div>
                <div class="big-number">{summary["recommended"]}</div>
            </div>

            <div class="card">
                <div class="metric-title">Paper Submitted</div>
                <div class="big-number">{summary["submitted"]}</div>
            </div>

            <div class="card">
                <div class="metric-title">Vetoed / Blocked / Errors</div>
                <div class="big-number">{summary["vetoed"] + summary["blocked"] + summary["errors"]}</div>
            </div>
        </div>

        <h2 class="section-title">Portfolio Performance Snapshot</h2>
        <div class="card chart">
            {chart_html}
        </div>

        <h2 class="section-title">Current Positions</h2>
        <div class="card">
            {position_rows}
        </div>

        <h2 class="section-title">Recent Agent Decisions</h2>
        <div class="card">
            {log_rows}
        </div>

        {latest_raw}
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.post("/run-ticker")
def run_ticker(ticker: str = Form(...)):
    ticker = ticker.strip().upper()

    if not ticker.isalnum() or len(ticker) > 10:
        return HTMLResponse(
            "<h2>Invalid ticker.</h2><p>Use symbols like NVDA, TSLA, AAPL.</p>",
            status_code=400,
        )

    try:
        run_one_ticker(ticker, allow_execution=True)
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        return HTMLResponse(
            f"""
            <h2>Trade cycle failed for {escape(ticker)}</h2>
            <pre>{escape(str(e))}</pre>
            <p><a href="/">Back to dashboard</a></p>
            """,
            status_code=500,
        )


@app.post("/run-screener")
def run_screener_route(
    top_n: int = Form(25),
    max_symbols: str = Form("all"),
):
    if screener_lock.locked():
        return RedirectResponse(url="/screener-results", status_code=303)

    parsed_max_symbols = parse_max_symbols(max_symbols)

    thread = threading.Thread(
        target=run_screener_background,
        kwargs={
            "top_n": top_n,
            "max_symbols": parsed_max_symbols,
        },
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url="/screener-results", status_code=303)


@app.get("/screener-results", response_class=HTMLResponse)
def screener_results_page():
    status = read_json(SCREENER_STATUS_PATH, {
        "status": "not_started",
        "current": 0,
        "total": 0,
        "percent": 0,
        "current_ticker": None,
        "message": "No scan has started yet.",
        "error": None,
    })

    results = read_json(SCREENER_RESULTS_PATH, {
        "generated_at": None,
        "candidates": [],
    })

    candidates = results.get("candidates", [])

    percent = float(status.get("percent", 0) or 0)
    current = status.get("current", 0)
    total = status.get("total", 0)
    current_ticker = status.get("current_ticker")
    message = status.get("message")
    scan_status = status.get("status")

    rows = ""
    cards = ""

    for item in candidates:
        ticker = escape(str(item.get("ticker", "")))
        score = escape(str(item.get("screener_score", item.get("score", ""))))
        reasoning = escape(str(item.get("reasoning", "")))
        summary = item.get("technical_summary", {})

        close = escape(str(summary.get("close", "")))
        rsi = escape(str(summary.get("rsi", "")))
        macd = escape(str(summary.get("macd", "")))
        volume_ratio = escape(str(summary.get("volume_ratio", "")))

        rows += f"""
        <tr>
            <td>{ticker}</td>
            <td>{score}</td>
            <td>{close}</td>
            <td>{rsi}</td>
            <td>{macd}</td>
            <td>{volume_ratio}</td>
            <td>{reasoning}</td>
        </tr>
        """

        cards += f"""
        <div class="card-soft">
            <h3>{ticker}</h3>
            <p><b>Screener Score:</b> {score}</p>
            <p class="muted small">{reasoning}</p>
        </div>
        """

    if not rows:
        rows = """
        <tr>
            <td colspan="9">No screener results yet.</td>
        </tr>
        """

    if not cards:
        cards = """
        <div class="card-soft">
            <h3>No candidates yet</h3>
            <p class="muted">Run the screener first. When it finishes, the best technical candidates will appear here.</p>
        </div>
        """

    refresh_tag = ""
    if scan_status == "running":
        refresh_tag = '<meta http-equiv="refresh" content="10">'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Screener Results</title>
        {refresh_tag}
        {shared_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a></p>

        <div class="page-header">
            <h1>Market Screener</h1>
            <p class="subtitle">
                This scans the tradable stock universe for technical setups. It does not research companies,
                make final trade decisions, or submit paper trades.
            </p>
        </div>

        <div class="card">
            <h2>Run Market Screener</h2>

            <div class="callout">
                Use this first when you want to discover technically interesting stocks.
                The Full AI Recommendation Scan will later research and rank these kinds of candidates.
            </div>

            <p style="margin-top:16px;">{badge(scan_status, status_class(scan_status))}</p>
            <p><b>{pct(percent)}</b> complete</p>

            <div class="progress-shell">
                <div class="progress-bar" style="width: {percent}%;"></div>
            </div>

            <p>{escape(str(current))} / {escape(str(total))} stocks scanned</p>
            <p>Current ticker: {escape(str(current_ticker))}</p>
            <p>Message: {escape(str(message))}</p>
            <p class="muted">Generated At: {escape(str(results.get("generated_at")))}</p>
            <p>Error: {escape(str(status.get("error")))}</p>

            <form method="post" action="/run-screener">
                <label>Top N:</label>
                <input type="number" name="top_n" value="25" min="1" max="100">

                <label>Max Symbols:</label>
                <input type="text" name="max_symbols" value="all">

                <button type="submit">Run Market Screener</button>
            </form>

            <p class="muted small">
                Use <b>all</b> to scan every tradable stock from Alpaca. Use <b>300</b> or <b>1000</b> for faster tests.
            </p>
        </div>

        <div class="card">
            <h2>Top Technical Candidates</h2>
            <div class="tool-grid">
                {cards}
            </div>
        </div>

        <div class="card">
            <h2>Full Screener Results</h2>
            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Score</th>
                    <th>Close</th>
                    <th>RSI</th>
                    <th>MACD</th>
                    <th>Volume Ratio</th>
                    <th>Reasoning</th>
                </tr>
                {rows}
            </table>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.post("/run-recommender")
def run_recommender_route(
    top_screener_n: int = Form(10),
    final_n: int = Form(5),
    max_symbols: str = Form("300"),
):
    if recommender_lock.locked():
        return RedirectResponse(url="/recommendations", status_code=303)

    parsed_max_symbols = parse_max_symbols(max_symbols)

    thread = threading.Thread(
        target=run_recommender_background,
        kwargs={
            "top_screener_n": top_screener_n,
            "final_n": final_n,
            "max_symbols": parsed_max_symbols,
        },
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url="/recommendations", status_code=303)



def render_recent_headlines(rec):
    headlines_data = rec.get("recent_headlines", {})
    headline_items = []

    for item in headlines_data.get("yahoo_news", []):
        title = escape(str(item.get("title", "")))
        link = escape(str(item.get("link", "")))
        if title and link:
            headline_items.append(
                f'<li><a href="{link}" target="_blank">{title}</a></li>'
            )

    for item in headlines_data.get("google_news", []):
        title = escape(str(item.get("title", "")))
        link = escape(str(item.get("link", "")))
        if title and link:
            headline_items.append(
                f'<li><a href="{link}" target="_blank">{title}</a></li>'
            )

    if headline_items:
        return "<ul>" + "".join(headline_items[:5]) + "</ul>"

    return "<span class='empty'>No recent headlines found.</span>"


@app.get("/recommendations", response_class=HTMLResponse)
def recommendations_page():
    status = read_json(RECOMMENDER_STATUS_PATH, {
        "batch_id": None,
        "status": "idle",
        "current": 0,
        "total": 0,
        "percent": 0,
        "current_ticker": None,
        "message": "No recommendation scan has been started yet.",
    })

    results = read_json(RECOMMENDER_RESULTS_PATH, {
        "batch_id": None,
        "generated_at": None,
        "recommendations": [],
        "watchlist_candidates": [],
        "all_reviewed": [],
        "summary": {},
    })

    history = read_json(RECOMMENDER_HISTORY_PATH, {"batches": []})

    recommendations = results.get("recommendations", [])
    watchlist_candidates = results.get("watchlist_candidates", [])
    all_reviewed = results.get("all_reviewed", [])
    summary = results.get("summary", {})

    percent = float(status.get("percent", 0) or 0)
    rec_status = status.get("status", "idle")

    rec_rows = ""

    if not recommendations:
        rec_rows = """
        <tr>
            <td colspan="8">No approved recommendations in the latest batch.</td>
        </tr>
        """
    else:
        for rec in recommendations:
            ticker = escape(str(rec.get("ticker", "Unknown")))
            score = escape(str(rec.get("recommendation_score", "N/A")))
            final_status = escape(str(rec.get("final_status", "N/A")))

            research = rec.get("research_brief", {})
            quant = rec.get("quant_signal", {})
            pm = rec.get("pm_decision", {})
            risk = rec.get("risk_result", {})

            research_confidence = escape(str(research.get("confidence", "N/A")))
            quant_strength = escape(str(quant.get("strength", "N/A")))
            pm_decision = escape(str(pm.get("decision", "N/A")))
            risk_approved = escape(str(risk.get("approved", "N/A")))
            sector = escape(str(rec.get("sector", "Unknown")))
            market_sentiment = escape(str(rec.get("market_sentiment", {}).get("label", "mixed")))
            reasoning = escape(str(pm.get("reasoning", rec.get("error", ""))))

            headlines_html = "<span class='empty'>No recent headlines found.</span>"
            headlines_data = rec.get("recent_headlines", {})
            headline_items = []

            for item in headlines_data.get("yahoo_news", []):
                title = escape(str(item.get("title", "")))
                link = escape(str(item.get("link", "")))
                if title and link:
                    headline_items.append(
                        f'<li><a href="{link}" target="_blank">{title}</a></li>'
                    )

            for item in headlines_data.get("google_news", []):
                title = escape(str(item.get("title", "")))
                link = escape(str(item.get("link", "")))
                if title and link:
                    headline_items.append(
                        f'<li><a href="{link}" target="_blank">{title}</a></li>'
                    )

            if headline_items:
                headlines_html = "<ul>" + "".join(headline_items[:5]) + "</ul>"

            headlines_html = locals().get(
                "headlines_html",
                "<span class='empty'>No recent headlines found.</span>"
            )

            rec_rows += f"""
            <tr>
                <td>{ticker}</td>
                <td>{score}</td>
                <td>{badge(final_status, status_class(final_status))}</td>
                <td>{research_confidence}</td>
                <td>{quant_strength}</td>
                <td>{pm_decision}</td>
                <td>{risk_approved}</td>
                <td>{sector}</td>
                <td>{market_sentiment}</td>
                <td>{reasoning}</td>
                <td>{render_recent_headlines(rec)}</td>
            </tr>
            """
    watch_rows = ""

    if not watchlist_candidates:
        watch_rows = """
        <tr>
            <td colspan="5">No watchlist candidates in the latest batch.</td>
        </tr>
        """
    else:
        for rec in watchlist_candidates[:10]:
            ticker = escape(str(rec.get("ticker", "Unknown")))
            score = escape(str(rec.get("recommendation_score", "N/A")))
            final_status = escape(str(rec.get("final_status", "N/A")))
            pm = rec.get("pm_decision", {})
            sector = escape(str(rec.get("sector", "Unknown")))
            market_sentiment = escape(str(rec.get("market_sentiment", {}).get("label", "mixed")))
            reasoning = escape(str(pm.get("reasoning", rec.get("error", ""))))

            quality = rec.get("quality_review", {})
            quality_reasons = quality.get("reasons", [])

            if quality_reasons:
                quality_html = "<ul>" + "".join(
                    f"<li>{escape(str(reason))}</li>"
                    for reason in quality_reasons
                ) + "</ul>"
            else:
                quality_html = "<span class='empty'>No quality rejection reasons.</span>"

            watch_rows += f"""
            <tr>
                <td>{ticker}</td>
                <td>{score}</td>
                <td>{badge(final_status, status_class(final_status))}</td>
                <td>{escape(str(rec.get("recommendation_type", "")))}</td>
                <td>{sector}</td>
                <td>{market_sentiment}</td>
                <td>{reasoning}<br><br><b>Quality Review</b>{quality_html}</td>
                <td>{render_recent_headlines(rec)}</td>
            </tr>
            """

    history_cards = ""

    for batch in history.get("batches", [])[:6]:
        batch_id = escape(str(batch.get("batch_id", "")))
        generated_at = escape(str(batch.get("generated_at", "")))
        batch_summary = batch.get("summary", {})
        total_reviewed = escape(str(batch_summary.get("total_reviewed", 0)))
        recommended_count = escape(str(batch_summary.get("recommended_not_executed", 0)))
        errors = escape(str(batch_summary.get("errors", 0)))

        history_cards += f"""
        <div class="card-soft">
            <h3>Batch {batch_id}</h3>
            <p class="muted small">Generated: {generated_at}</p>
            <p>Total reviewed: <b>{total_reviewed}</b></p>
            <p>Recommendations: <b>{recommended_count}</b></p>
            <p>Errors: <b>{errors}</b></p>
        </div>
        """

    if not history_cards:
        history_cards = """
        <div class="card-soft">
            <h3>No previous batches</h3>
            <p class="muted">Each full recommendation scan will be saved here as a separate batch.</p>
        </div>
        """

    refresh_tag = ""
    if rec_status == "running":
        refresh_tag = '<meta http-equiv="refresh" content="10">'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus AI Recommendations</title>
        {refresh_tag}
        {shared_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a></p>

        <div class="page-header">
            <h1>Full AI Recommendation Scan</h1>
            <p class="subtitle">
                This finds trade ideas. It does not submit trades. It scans the market, researches top screener candidates,
                runs PM and risk review, and returns recommendations as a separate batch.
            </p>
        </div>

        <div class="card">
            <h2>Run Recommendation Scan</h2>

            <div class="success-callout">
                Recommendation mode is research-only. If a stock passes PM and risk, it will be marked
                <b>RECOMMENDED_NOT_EXECUTED</b>. No Alpaca order should be submitted from this page.
            </div>

            <p style="margin-top:16px;">{badge(rec_status, status_class(rec_status))}</p>
            <p>{escape(str(status.get("message", "")))}</p>

            <div class="progress-shell">
                <div class="progress-bar" style="width: {percent}%;"></div>
            </div>

            <p><b>{pct(percent)}</b> complete</p>
            <p>Batch ID: {escape(str(status.get("batch_id")))}</p>
            <p>{escape(str(status.get("current")))} / {escape(str(status.get("total")))} candidates analyzed</p>
            <p>Current ticker: {escape(str(status.get("current_ticker")))}</p>
            <p class="muted small">Updated: {escape(str(status.get("updated_at")))}</p>

            <form method="post" action="/run-recommender">
                <label>Top screener candidates:</label>
                <input type="number" name="top_screener_n" value="10" min="1" max="50">

                <label>Final recommendations:</label>
                <input type="number" name="final_n" value="5" min="1" max="20">

                <label>Max symbols:</label>
                <input type="text" name="max_symbols" value="300">

                <button type="submit">Find Recommendations</button>
            </form>

            <p class="muted small">
                Use max_symbols=300 for testing. Use max_symbols=all for a full market scan.
            </p>
        </div>

        <div class="grid grid-4">
            <div class="card">
                <div class="metric-title">Latest Batch</div>
                <div class="big-number">{escape(str(results.get("batch_id")))}</div>
            </div>
            <div class="card">
                <div class="metric-title">Total Reviewed</div>
                <div class="big-number">{escape(str(summary.get("total_reviewed", len(all_reviewed))))}</div>
            </div>
            <div class="card">
                <div class="metric-title">Recommended</div>
                <div class="big-number">{escape(str(summary.get("recommended_not_executed", len(recommendations))))}</div>
            </div>
            <div class="card">
                <div class="metric-title">Errors</div>
                <div class="big-number">{escape(str(summary.get("errors", 0)))}</div>
            </div>
        </div>

        <div class="card">
            <h2>Approved Recommendations, Not Executed</h2>
            <p class="muted small">
                These passed PM and risk checks, but no paper order was submitted because this is recommendation-only mode.
            </p>

            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Score</th>
                    <th>Final Status</th>
                    <th>Research Confidence</th>
                    <th>Quant Strength</th>
                    <th>PM Decision</th>
                    <th>Risk Approved</th>
                    <th>Sector</th>
                    <th>Market Mood</th>
                    <th>Reasoning</th>
                    <th>Recent Headlines</th>
                </tr>
                {rec_rows}
            </table>
        </div>

        <div class="card">
            <h2>Watchlist / Rejected Candidates</h2>
            <p class="muted small">
                These were reviewed but did not become final recommendations. They may still be useful to inspect.
            </p>

            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Score</th>
                    <th>Final Status</th>
                    <th>Type</th>
                    <th>Sector</th>
                    <th>Market Mood</th>
                    <th>Reasoning</th>
                    <th>Recent Headlines</th>
                </tr>
                {watch_rows}
            </table>
        </div>

        <div class="card">
            <h2>Previous Recommendation Batches</h2>
            <p class="muted small">
                Each full scan is saved as its own batch, so new scans do not get confused with older outputs.
            </p>

            <div class="tool-grid">
                {history_cards}
            </div>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.get("/api/logs")
def api_logs():
    return get_recent_trade_logs(limit=50)


@app.get("/api/account")
def api_account():
    return get_account_summary()


@app.get("/api/positions")
def api_positions():
    return get_positions()


@app.get("/api/portfolio-history")
def api_portfolio_history():
    return get_portfolio_history(limit=100)


@app.get("/health")
def health_check():
    return {"status": "ok"}

def render_sector_headlines(headlines):
    items = []

    for item in headlines:
        title = escape(str(item.get("title", "")))
        link = escape(str(item.get("link", "")))
        published = escape(str(item.get("published", "")))

        if title and link:
            items.append(
                f'<li><a href="{link}" target="_blank">{title}</a><br><span class="muted small">{published}</span></li>'
            )
        elif title:
            items.append(f'<li>{title}</li>')

    if not items:
        return "<span class='empty'>No headlines found.</span>"

    return "<ul>" + "".join(items[:8]) + "</ul>"


def run_sector_scan_background(sector, top_n=25, max_symbols=500, max_sector_symbols=150):
    if sector_lock.locked():
        return

    with sector_lock:
        write_json(SECTOR_SCAN_RESULTS_PATH, {
            "status": "running",
            "generated_at": now_iso(),
            "sector": sector,
            "message": f"Scanning {sector} sector.",
            "candidates": [],
        })

        try:
            result = scan_sector(
                sector=sector,
                top_n=top_n,
                max_symbols=max_symbols,
                max_sector_symbols=max_sector_symbols,
            )

            result["status"] = "complete"
            result["message"] = f"Sector scan complete for {sector}."
            write_json(SECTOR_SCAN_RESULTS_PATH, result)

        except Exception as e:
            write_json(SECTOR_SCAN_RESULTS_PATH, {
                "status": "error",
                "generated_at": now_iso(),
                "sector": sector,
                "message": str(e),
                "candidates": [],
            })


@app.post("/refresh-sector-intelligence")
def refresh_sector_intelligence_route():
    refresh_all_sector_intelligence(limit=6)
    return RedirectResponse(url="/sectors", status_code=303)


@app.post("/run-sector-scan")
def run_sector_scan_route(
    sector: str = Form(...),
    top_n: int = Form(25),
    max_symbols: str = Form("500"),
    max_sector_symbols: int = Form(150),
):
    if sector_lock.locked():
        return RedirectResponse(url="/sectors", status_code=303)

    parsed_max_symbols = parse_max_symbols(max_symbols)

    thread = threading.Thread(
        target=run_sector_scan_background,
        kwargs={
            "sector": sector,
            "top_n": top_n,
            "max_symbols": parsed_max_symbols,
            "max_sector_symbols": max_sector_symbols,
        },
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url="/sectors", status_code=303)


@app.get("/sectors", response_class=HTMLResponse)
def sectors_page():
    sector_intel = get_cached_sector_intelligence()

    if not sector_intel.get("sectors"):
        try:
            sector_intel = refresh_all_sector_intelligence(limit=6)
        except Exception:
            sector_intel = {
                "generated_at": None,
                "sectors": [],
            }

    sector_scan = read_json(SECTOR_SCAN_RESULTS_PATH, {
        "status": "not_started",
        "generated_at": None,
        "sector": None,
        "message": "No sector scan has been run yet.",
        "candidates": [],
    })

    sector_options = ""

    for sector in CANONICAL_SECTORS:
        selected = "selected" if sector == sector_scan.get("sector") else ""
        sector_options += f'<option value="{escape(sector)}" {selected}>{escape(sector)}</option>'

    sector_cards = ""

    for item in sector_intel.get("sectors", []):
        sector = escape(str(item.get("sector", "")))
        sentiment = item.get("sentiment", {})
        label = escape(str(sentiment.get("label", "mixed")))
        relative_label = escape(str(sentiment.get("relative_label", "neutral")))
        rank = escape(str(sentiment.get("rank", "")))
        score = escape(str(sentiment.get("score", "50")))
        headlines_html = render_sector_headlines(item.get("headlines", []))

        positive_drivers = sentiment.get("top_positive_drivers", [])
        negative_drivers = sentiment.get("top_negative_drivers", [])

        driver_items = []

        for driver in positive_drivers[:2]:
            term = escape(str(driver.get("term", "")))
            headline = escape(str(driver.get("headline", "")))
            driver_items.append(f"<li><b>Positive:</b> {term} — {headline}</li>")

        for driver in negative_drivers[:2]:
            term = escape(str(driver.get("term", "")))
            headline = escape(str(driver.get("headline", "")))
            driver_items.append(f"<li><b>Negative:</b> {term} — {headline}</li>")

        if driver_items:
            drivers_html = "<ul>" + "".join(driver_items) + "</ul>"
        else:
            drivers_html = "<p class='muted small'>No strong sentiment keywords found. Score is mostly neutral.</p>"

        css_class = "approved" if relative_label == "favorable" else "blocked" if relative_label == "weak" else "vetoed"

        sector_cards += f"""
        <div class="card-soft">
            <h3>#{rank} {sector}</h3>
            <p>
                {badge(relative_label, css_class)}
                {badge(label, "neutral")}
                <span class="muted small">Score: {score}/100</span>
            </p>
            <h4>Sentiment Drivers</h4>
            {drivers_html}
            <h4>Important Headlines</h4>
            {headlines_html}
        </div>
        """

    if not sector_cards:
        sector_cards = """
        <div class="card-soft">
            <h3>No sector intelligence yet</h3>
            <p class="muted">Refresh sector intelligence to load headlines and sentiment.</p>
        </div>
        """

    candidates = sector_scan.get("candidates", [])
    candidate_rows = ""

    for candidate in candidates:
        ticker = escape(str(candidate.get("ticker", "")))
        company_name = escape(str(candidate.get("company_name", "")))
        industry = escape(str(candidate.get("industry", "")))
        score = escape(str(candidate.get("screener_score", "")))
        reasoning = escape(str(candidate.get("reasoning", "")))
        summary = candidate.get("technical_summary", {})

        close = escape(str(summary.get("close", "")))
        rsi = escape(str(summary.get("rsi", "")))
        macd = escape(str(summary.get("macd", "")))
        volume_ratio = escape(str(summary.get("volume_ratio", "")))

        candidate_rows += f"""
        <tr>
            <td>{ticker}</td>
            <td>{company_name}</td>
            <td>{industry}</td>
            <td>{score}</td>
            <td>{close}</td>
            <td>{rsi}</td>
            <td>{macd}</td>
            <td>{volume_ratio}</td>
            <td>{reasoning}</td>
        </tr>
        """

    if not candidate_rows:
        candidate_rows = """
        <tr>
            <td colspan="9">No sector scan candidates yet.</td>
        </tr>
        """

    scan_status = sector_scan.get("status", "not_started")
    refresh_tag = ""

    if scan_status == "running":
        refresh_tag = '<meta http-equiv="refresh" content="10">'

    scan_sector_name = escape(str(sector_scan.get("sector", "None")))
    scan_message = escape(str(sector_scan.get("message", "")))

    scan_headlines_html = ""
    scan_intel = sector_scan.get("sector_intelligence", {})

    if scan_intel:
        scan_headlines_html = render_sector_headlines(scan_intel.get("headlines", []))

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Sector Intelligence</title>
        {refresh_tag}
        {shared_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a></p>

        <div class="page-header">
            <h1>Sector Intelligence</h1>
            <p class="subtitle">
                Review sector-level sentiment and headlines first, then choose which sector to scan for stock candidates.
            </p>
        </div>

        <div class="card">
            <h2>Refresh Sector Sentiment</h2>
            <p class="muted small">
                This pulls important headlines for each sector and creates a basic sentiment score.
            </p>

            <form method="post" action="/refresh-sector-intelligence">
                <button type="submit">Refresh Sector Intelligence</button>
            </form>

            <p class="muted small">Last refreshed: {escape(str(sector_intel.get("generated_at")))}</p>
        </div>

        <div class="card">
            <h2>Scan a Selected Sector</h2>

            <div class="callout">
                Use the sector sentiment cards below to decide what looks strongest, then scan only that sector.
            </div>

            <form method="post" action="/run-sector-scan" style="margin-top:16px;">
                <label>Sector:</label>
                <select name="sector">
                    {sector_options}
                </select>

                <label>Top N:</label>
                <input type="number" name="top_n" value="25" min="1" max="100">

                <label>Max universe symbols:</label>
                <input type="text" name="max_symbols" value="500">

                <label>Max sector symbols:</label>
                <input type="number" name="max_sector_symbols" value="150" min="10" max="1000">

                <button type="submit">Run Sector Scan</button>
            </form>

            <p class="muted small">
                For testing, use max universe symbols = 500. Later, use all for broader coverage.
            </p>
        </div>

        <h2 class="section-title">Sector Sentiment Board</h2>
        <div class="grid grid-3">
            {sector_cards}
        </div>

        <h2 class="section-title">Latest Sector Scan</h2>
        <div class="card">
            <p>{badge(scan_status, status_class(scan_status))}</p>
            <p><b>Sector:</b> {scan_sector_name}</p>
            <p><b>Message:</b> {scan_message}</p>
            <p><b>Generated:</b> {escape(str(sector_scan.get("generated_at")))}</p>
            <p><b>Universe checked:</b> {escape(str(sector_scan.get("universe_checked", "")))}</p>
            <p><b>Sector ticker count:</b> {escape(str(sector_scan.get("sector_ticker_count", "")))}</p>

            <h3>Important Headlines for Selected Sector</h3>
            {scan_headlines_html}
        </div>

        <div class="card">
            <h2>Top Technical Candidates in Selected Sector</h2>
            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Company</th>
                    <th>Industry</th>
                    <th>Score</th>
                    <th>Close</th>
                    <th>RSI</th>
                    <th>MACD</th>
                    <th>Volume Ratio</th>
                    <th>Reasoning</th>
                </tr>
                {candidate_rows}
            </table>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


def run_performance_update_background():
    if performance_lock.locked():
        return

    with performance_lock:
        update_performance(limit=500)


@app.post("/update-performance")
def update_performance_route():
    if performance_lock.locked():
        return RedirectResponse(url="/performance", status_code=303)

    thread = threading.Thread(
        target=run_performance_update_background,
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url="/performance", status_code=303)


def format_return(value):
    if value is None:
        return "Pending"

    try:
        value = float(value)
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.2f}%"
    except Exception:
        return "Pending"


def return_class(value):
    if value is None:
        return "neutral"

    try:
        value = float(value)
    except Exception:
        return "neutral"

    if value > 0:
        return "approved"

    if value < 0:
        return "blocked"

    return "vetoed"


@app.get("/performance", response_class=HTMLResponse)
def performance_page():
    outcomes = get_outcomes(limit=250)
    summary = summarize_outcomes(outcomes)

    status = read_json(PERFORMANCE_STATUS_PATH, {
        "status": "not_started",
        "message": "Performance has not been updated yet.",
        "current": 0,
        "total": 0,
        "updated_at": None,
    })

    rows = ""

    for item in outcomes:
        ticker = escape(str(item.get("ticker", "")))
        recommended_at = escape(str(item.get("recommended_at", "")))
        final_status = escape(str(item.get("final_status", "")))
        outcome_status = escape(str(item.get("outcome_status", "PENDING")))
        score = escape(str(item.get("recommendation_score", "")))
        entry_price = money(item.get("entry_price"))
        latest_price = money(item.get("latest_price"))
        error = escape(str(item.get("error") or ""))

        rows += f"""
        <tr>
            <td>{ticker}</td>
            <td>{recommended_at}</td>
            <td>{badge(final_status, status_class(final_status))}</td>
            <td>{score}</td>
            <td>{entry_price}</td>
            <td>{latest_price}</td>
            <td>{badge(format_return(item.get("return_1d")), return_class(item.get("return_1d")))}</td>
            <td>{badge(format_return(item.get("return_3d")), return_class(item.get("return_3d")))}</td>
            <td>{badge(format_return(item.get("return_7d")), return_class(item.get("return_7d")))}</td>
            <td>{badge(format_return(item.get("return_30d")), return_class(item.get("return_30d")))}</td>
            <td>{badge(format_return(item.get("max_drawdown")), return_class(item.get("max_drawdown")))}</td>
            <td>{badge(outcome_status, status_class(outcome_status))}</td>
            <td>{error}</td>
        </tr>
        """

    if not rows:
        rows = """
        <tr>
            <td colspan="13">No tracked recommendations yet. Run a recommendation scan first, then update performance.</td>
        </tr>
        """

    update_status = escape(str(status.get("status", "not_started")))
    update_message = escape(str(status.get("message", "")))
    current = escape(str(status.get("current", 0)))
    total = escape(str(status.get("total", 0)))
    updated_at = escape(str(status.get("updated_at", "")))

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Performance Tracker</title>
        {shared_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a></p>

        <div class="page-header">
            <h1>Performance Tracker</h1>
            <p class="subtitle">
                Tracks how Olympus recommendations performed after they were generated.
                This is the proof layer for whether the system is actually useful.
            </p>
        </div>

        <div class="card">
            <h2>Update Performance</h2>
            <p>{badge(update_status, status_class(update_status))}</p>
            <p>{update_message}</p>
            <p class="muted small">Progress: {current} / {total}</p>
            <p class="muted small">Last updated: {updated_at}</p>

            <form method="post" action="/update-performance">
                <button type="submit">Update Recommendation Performance</button>
            </form>
        </div>

        <div class="grid grid-4">
            <div class="card">
                <div class="metric-title">Tracked Ideas</div>
                <div class="big-number">{escape(str(summary.get("total_tracked", 0)))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Win Rate</div>
                <div class="big-number">{format_return(summary.get("win_rate")) if summary.get("win_rate") is not None else "N/A"}</div>
            </div>

            <div class="card">
                <div class="metric-title">Avg 7D Return</div>
                <div class="big-number">{format_return(summary.get("avg_7d"))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Avg Max Drawdown</div>
                <div class="big-number">{format_return(summary.get("avg_max_drawdown"))}</div>
            </div>
        </div>

        <div class="grid grid-4">
            <div class="card">
                <div class="metric-title">Avg 1D Return</div>
                <div class="big-number">{format_return(summary.get("avg_1d"))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Avg 3D Return</div>
                <div class="big-number">{format_return(summary.get("avg_3d"))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Avg 30D Return</div>
                <div class="big-number">{format_return(summary.get("avg_30d"))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Completed</div>
                <div class="big-number">{escape(str(summary.get("completed", 0)))}</div>
            </div>
        </div>

        <div class="grid grid-4">
            <div class="card">
                <div class="metric-title">Early</div>
                <div class="big-number">{escape(str(summary.get("early", 0)))}</div>
                <p class="muted small">Has 1D data, not enough time to judge.</p>
            </div>

            <div class="card">
                <div class="metric-title">Pending</div>
                <div class="big-number">{escape(str(summary.get("pending", 0)))}</div>
                <p class="muted small">No forward return data yet.</p>
            </div>

            <div class="card">
                <div class="metric-title">Wins</div>
                <div class="big-number">{escape(str(summary.get("wins", 0)))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Losses</div>
                <div class="big-number">{escape(str(summary.get("losses", 0)))}</div>
            </div>
        </div>

        <div class="card">
            <h2>Recommendation Outcomes</h2>
            <p class="muted small">
                Returns are calculated from daily close prices using the first available bar at or after the recommendation date.
            </p>

            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Recommended At</th>
                    <th>Original Status</th>
                    <th>Score</th>
                    <th>Entry</th>
                    <th>Latest</th>
                    <th>1D</th>
                    <th>3D</th>
                    <th>7D</th>
                    <th>30D</th>
                    <th>Max Drawdown</th>
                    <th>Outcome</th>
                    <th>Error</th>
                </tr>
                {rows}
            </table>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html)

def run_fast_scan_background(top_n=50, max_symbols=500, sector="", only_quality_approved=False):
    if fast_scan_lock.locked():
        return

    with fast_scan_lock:
        run_fast_scan(
            top_n=top_n,
            max_symbols=max_symbols,
            sector=sector if sector else None,
            only_quality_approved=only_quality_approved,
        )


@app.post("/run-fast-scan")
def run_fast_scan_route(
    top_n: int = Form(50),
    max_symbols: str = Form("500"),
    sector: str = Form(""),
    only_quality_approved: str = Form("false"),
):
    if fast_scan_lock.locked():
        return RedirectResponse(url="/fast-scan", status_code=303)

    parsed_max_symbols = parse_max_symbols(max_symbols)
    only_quality = str(only_quality_approved).lower() in ["true", "on", "1", "yes"]

    thread = threading.Thread(
        target=run_fast_scan_background,
        kwargs={
            "top_n": top_n,
            "max_symbols": parsed_max_symbols,
            "sector": sector,
            "only_quality_approved": only_quality,
        },
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url="/fast-scan", status_code=303)


@app.get("/fast-scan", response_class=HTMLResponse)
def fast_scan_page():
    status = get_fast_scan_status()
    results = get_fast_scan_results()

    candidates = results.get("candidates", [])
    summary = results.get("summary", {})

    sector_options = '<option value="">All</option>'

    try:
        for sector in CANONICAL_SECTORS:
            selected = "selected" if sector == results.get("sector") else ""
            sector_options += f'<option value="{escape(sector)}" {selected}>{escape(sector)}</option>'
    except Exception:
        pass

    rows = ""

    for item in candidates:
        ticker = escape(str(item.get("ticker", "")))
        company = escape(str(item.get("company_name", "")))
        sector = escape(str(item.get("sector", "")))
        industry = escape(str(item.get("industry", "")))
        score = escape(str(item.get("screener_score", "")))
        quality = item.get("quality_review", {})
        quality_approved = quality.get("approved")
        quality_label = "approved" if quality_approved else "rejected"
        quality_css = "approved" if quality_approved else "blocked"

        reasons = quality.get("reasons", [])
        warnings = quality.get("warnings", [])

        reason_html = ""

        if reasons:
            reason_html += "<b>Reasons</b><ul>" + "".join(
                f"<li>{escape(str(reason))}</li>"
                for reason in reasons
            ) + "</ul>"

        if warnings:
            reason_html += "<b>Warnings</b><ul>" + "".join(
                f"<li>{escape(str(warning))}</li>"
                for warning in warnings
            ) + "</ul>"

        if not reason_html:
            reason_html = "<span class='empty'>No major quality issues.</span>"

        summary_data = item.get("technical_summary", {})
        close = escape(str(summary_data.get("close", "")))
        rsi = escape(str(summary_data.get("rsi", "")))
        volume_ratio = escape(str(summary_data.get("volume_ratio", "")))

        rows += f"""
        <tr>
            <td><b>{ticker}</b></td>
            <td>{company}</td>
            <td>{sector}</td>
            <td>{industry}</td>
            <td>{score}</td>
            <td>{close}</td>
            <td>{rsi}</td>
            <td>{volume_ratio}</td>
            <td>{badge(quality_label, quality_css)}<br>{reason_html}</td>
        </tr>
        """

    if not rows:
        rows = """
        <tr>
            <td colspan="9">No fast scan candidates yet.</td>
        </tr>
        """

    scan_status = escape(str(status.get("status", "not_started")))
    scan_message = escape(str(status.get("message", "")))

    refresh_tag = ""
    if status.get("status") == "running":
        refresh_tag = '<meta http-equiv="refresh" content="10">'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Fast Scan</title>
        {refresh_tag}
        {shared_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a></p>

        <div class="page-header">
            <h1>Fast Scan</h1>
            <p class="subtitle">
                Fast Scan uses sector, screener, and quality filters only. It does not run the full LLM review.
                Use this first, then send selected tickers to Deep AI Review.
            </p>
        </div>

        <div class="card">
            <h2>Run Fast Scan</h2>
            <p>{badge(scan_status, status_class(scan_status))}</p>
            <p>{scan_message}</p>

            <form method="post" action="/run-fast-scan">
                <label>Top candidates:</label>
                <input type="number" name="top_n" value="50" min="5" max="200">

                <label>Max symbols:</label>
                <input type="text" name="max_symbols" value="500">

                <label>Sector:</label>
                <select name="sector">
                    {sector_options}
                </select>

                <label>
                    <input type="checkbox" name="only_quality_approved">
                    Only quality approved
                </label>

                <button type="submit">Run Fast Scan</button>
            </form>
        </div>

        <div class="grid grid-4">
            <div class="card">
                <div class="metric-title">Candidates</div>
                <div class="big-number">{escape(str(summary.get("total_candidates", 0)))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Quality Approved</div>
                <div class="big-number">{escape(str(summary.get("quality_approved", 0)))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Quality Rejected</div>
                <div class="big-number">{escape(str(summary.get("quality_rejected", 0)))}</div>
            </div>

            <div class="card">
                <div class="metric-title">Generated</div>
                <div class="big-number" style="font-size:18px;">{escape(str(results.get("generated_at")))}</div>
            </div>
        </div>

        <div class="card">
            <h2>Fast Scan Candidates</h2>
            <p class="muted small">
                Copy tickers you like into Deep AI Review.
            </p>
            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Company</th>
                    <th>Sector</th>
                    <th>Industry</th>
                    <th>Score</th>
                    <th>Close</th>
                    <th>RSI</th>
                    <th>Volume Ratio</th>
                    <th>Quality</th>
                </tr>
                {rows}
            </table>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


def run_deep_review_background(tickers, allow_execution=False):
    if deep_review_lock.locked():
        return

    with deep_review_lock:
        run_deep_review(
            tickers=tickers,
            allow_execution=allow_execution,
        )


@app.post("/run-deep-review")
def run_deep_review_route(
    tickers: str = Form(...),
    allow_execution: str = Form("false"),
):
    if deep_review_lock.locked():
        return RedirectResponse(url="/deep-review", status_code=303)

    allow_exec = str(allow_execution).lower() in ["true", "on", "1", "yes"]

    thread = threading.Thread(
        target=run_deep_review_background,
        kwargs={
            "tickers": tickers,
            "allow_execution": allow_exec,
        },
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url="/deep-review", status_code=303)


@app.get("/deep-review", response_class=HTMLResponse)
def deep_review_page():
    status = get_deep_review_status()
    results = get_deep_review_results()

    rows = ""

    for item in results.get("results", []):
        ticker = escape(str(item.get("ticker", "")))
        final_status = escape(str(item.get("final_status", "")))
        sector = escape(str(item.get("sector", "")))
        industry = escape(str(item.get("industry", "")))
        error = escape(str(item.get("error", "")))

        research = item.get("research_brief", {})
        quant = item.get("quant_signal", {})
        pm = item.get("pm_decision", {})
        risk = item.get("risk_result", {})

        summary = escape(str(research.get("summary", "")))
        reasoning = escape(str(pm.get("reasoning", "")))
        quant_direction = escape(str(quant.get("direction", "")))
        quant_strength = escape(str(quant.get("strength", "")))
        risk_approved = escape(str(risk.get("approved", "")))

        rows += f"""
        <tr>
            <td><b>{ticker}</b></td>
            <td>{badge(final_status, status_class(final_status))}</td>
            <td>{sector}</td>
            <td>{industry}</td>
            <td>{quant_direction} / {quant_strength}</td>
            <td>{risk_approved}</td>
            <td>{summary}<br><br><b>PM:</b> {reasoning}<br><span class="error">{error}</span></td>
        </tr>
        """

    if not rows:
        rows = """
        <tr>
            <td colspan="7">No deep review results yet.</td>
        </tr>
        """

    review_status = escape(str(status.get("status", "not_started")))
    review_message = escape(str(status.get("message", "")))
    current = escape(str(status.get("current", 0)))
    total = escape(str(status.get("total", 0)))

    refresh_tag = ""
    if status.get("status") == "running":
        refresh_tag = '<meta http-equiv="refresh" content="10">'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Deep AI Review</title>
        {refresh_tag}
        {shared_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a></p>

        <div class="page-header">
            <h1>Deep AI Review</h1>
            <p class="subtitle">
                Run the full Olympus AI loop only on selected tickers. This is slower, but much more detailed.
            </p>
        </div>

        <div class="card">
            <h2>Run Deep AI Review</h2>
            <p>{badge(review_status, status_class(review_status))}</p>
            <p>{review_message}</p>
            <p class="muted small">Progress: {current} / {total}</p>

            <form method="post" action="/run-deep-review">
                <label>Tickers:</label>
                <input
                    type="text"
                    name="tickers"
                    placeholder="NVDA,AAPL,TSLA"
                    style="width: 420px;"
                    required
                >

                <label>
                    <input type="checkbox" name="allow_execution">
                    Allow paper execution
                </label>

                <button type="submit">Run Deep Review</button>
            </form>

            <div class="warning-callout" style="margin-top:16px;">
                Leave paper execution unchecked unless you intentionally want approved ideas to be eligible for paper orders.
            </div>
        </div>

        <div class="card">
            <h2>Deep Review Results</h2>
            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Status</th>
                    <th>Sector</th>
                    <th>Industry</th>
                    <th>Quant</th>
                    <th>Risk Approved</th>
                    <th>Reasoning</th>
                </tr>
                {rows}
            </table>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request):
    data = get_position_review()
    reviews = data.get("reviews", [])
    summary = data.get("summary", {})
    generated_at = data.get("generated_at")

    rows = ""

    if not reviews:
        rows = """
        <tr>
            <td colspan="10">No position review has been run yet.</td>
        </tr>
        """
    else:
        for item in reviews:
            ticker = escape(str(item.get("ticker", "")))
            position = item.get("position", {})
            decision = item.get("decision", {})
            technicals = item.get("technicals", {})
            news_summary = item.get("news_summary", {})

            decision_label = escape(str(decision.get("decision", "UNKNOWN")))
            css_class = status_class(decision_label)

            qty = position.get("qty", 0)
            market_value = position.get("market_value", 0)
            avg_entry = position.get("avg_entry_price", 0)
            current_price = position.get("current_price", 0)
            unrealized_pl = position.get("unrealized_pl", 0)
            unrealized_plpc = float(position.get("unrealized_plpc", 0) or 0) * 100

            rsi = technicals.get("rsi", "")
            volume_ratio = technicals.get("volume_ratio", "")
            confidence = decision.get("confidence", 0)
            sell_pct = decision.get("sell_pct", 0)
            qty_to_sell = decision.get("estimated_qty_to_sell", 0)

            reasons = decision.get("reasons", [])
            reasons_html = "<ul>" + "".join(
                f"<li>{escape(str(reason))}</li>"
                for reason in reasons
            ) + "</ul>"

            headlines = news_summary.get("headlines", [])
            headline_html = ""

            if headlines:
                headline_html = "<ul>" + "".join(
                    f"<li>{escape(str(h.get('headline', '')))}</li>"
                    for h in headlines[:3]
                ) + "</ul>"
            else:
                headline_html = "<span class='empty'>No headlines found.</span>"

            action_html = "<span class='muted'>No sell action</span>"

            if float(sell_pct or 0) > 0:
                action_html = f"""
                <form method="post" action="/positions/sell">
                    <input type="hidden" name="ticker" value="{ticker}">
                    <input type="hidden" name="sell_pct" value="{sell_pct}">
                    <button type="submit">Paper sell {sell_pct}%</button>
                </form>
                <p class="muted small">Estimated qty: {float(qty_to_sell):.6f}</p>
                """

            rows += f"""
            <tr>
                <td><b>{ticker}</b></td>
                <td>{badge(decision_label, css_class)}</td>
                <td>{confidence}</td>
                <td>{qty}</td>
                <td>{money(market_value)}</td>
                <td>{money(avg_entry)} / {money(current_price)}</td>
                <td>{money(unrealized_pl)}<br>{unrealized_plpc:.2f}%</td>
                <td>RSI: {rsi}<br>Vol: {volume_ratio}</td>
                <td>{reasons_html}<br>{headline_html}</td>
                <td>{action_html}</td>
            </tr>
            """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Position Monitor</title>
        {shared_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a></p>

        <div class="page-header">
            <h1>Position Monitor</h1>
            <p class="subtitle">
                Reviews open Alpaca paper positions and recommends HOLD, WATCH_CLOSELY, TRIM,
                TAKE_PROFIT, SELL, or CUT_LOSS.
            </p>
        </div>

        <div class="grid grid-4">
            <div class="card">
                <div class="metric-title">Open Positions Reviewed</div>
                <div class="big-number">{summary.get("total_positions", 0)}</div>
            </div>

            <div class="card">
                <div class="metric-title">Take Profit / Trim</div>
                <div class="big-number">{summary.get("take_profit", 0)} / {summary.get("trim", 0)}</div>
            </div>

            <div class="card">
                <div class="metric-title">Sell / Cut Loss</div>
                <div class="big-number">{summary.get("sell", 0)} / {summary.get("cut_loss", 0)}</div>
            </div>

            <div class="card">
                <div class="metric-title">Hold / Watch</div>
                <div class="big-number">{summary.get("hold", 0)} / {summary.get("watch_closely", 0)}</div>
            </div>
        </div>

        <section class="card">
            <h2>Run Position Review</h2>
            <p class="muted small">Last generated: {escape(str(generated_at))}</p>

            <form method="post" action="/positions/review">
                <button type="submit">Review Open Positions</button>
            </form>

            <div class="warning-callout" style="margin-top:16px;">
                Sell buttons only work if ALPACA_PAPER=true and SELL_TRADING_ENABLED=true.
                Keep SELL_TRADING_ENABLED=false until you are ready to test paper sell orders.
            </div>
        </section>

        <section class="card">
            <h2>Open Position Sell Review</h2>
            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Decision</th>
                    <th>Confidence</th>
                    <th>Qty</th>
                    <th>Market Value</th>
                    <th>Entry / Current</th>
                    <th>Unrealized P/L</th>
                    <th>Technicals</th>
                    <th>Reasoning / News</th>
                    <th>Action</th>
                </tr>
                {rows}
            </table>
        </section>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.post("/positions/review")
def run_positions_review():
    review_all_positions()
    return RedirectResponse(url="/positions", status_code=303)


@app.post("/positions/sell")
def sell_position_from_dashboard(
    ticker: str = Form(...),
    sell_pct: float = Form(...),
):
    try:
        execute_position_sell(ticker=ticker, sell_pct=sell_pct)
    except Exception as e:
        error_path = Path("position_sell_error.json")
        error_path.write_text(json.dumps({
            "ticker": ticker,
            "sell_pct": sell_pct,
            "error": str(e),
            "timestamp": now_iso(),
        }, indent=2))

    review_all_positions()
    return RedirectResponse(url="/positions", status_code=303)


@app.get("/positions/ai", response_class=HTMLResponse)
def ai_positions_page(request: Request):
    data = get_ai_position_review()
    reviews = data.get("reviews", [])
    summary = data.get("summary", {})
    generated_at = data.get("generated_at")
    llm_mode = data.get("llm_mode")
    manual_prompt_file = data.get("manual_prompt_file")

    rows = ""

    if not reviews:
        rows = """
        <tr>
            <td colspan="11">No AI position review has been run yet.</td>
        </tr>
        """
    else:
        for item in reviews:
            ticker = escape(str(item.get("ticker", "")))
            position_review = item.get("position_review", {})
            position = position_review.get("position", {})
            rule_decision = position_review.get("decision", {})
            ai_decision = item.get("ai_decision", {})

            ai_label = escape(str(ai_decision.get("decision", "UNKNOWN")))
            rule_label = escape(str(rule_decision.get("decision", "UNKNOWN")))

            css_class = status_class(ai_label)

            qty = position.get("qty", 0)
            market_value = position.get("market_value", 0)
            avg_entry = position.get("avg_entry_price", 0)
            current_price = position.get("current_price", 0)
            unrealized_pl = position.get("unrealized_pl", 0)
            unrealized_plpc = float(position.get("unrealized_plpc", 0) or 0) * 100

            confidence = ai_decision.get("confidence", 0)
            sell_pct = ai_decision.get("sell_pct", 0)
            qty_to_sell = ai_decision.get("estimated_qty_to_sell", 0)

            reasoning = escape(str(ai_decision.get("reasoning", "")))
            thesis_status = escape(str(ai_decision.get("thesis_status", "")))
            rule_agreement = escape(str(ai_decision.get("rule_agreement", "")))
            profit_notes = escape(str(ai_decision.get("profit_protection_notes", "")))
            what_changes = escape(str(ai_decision.get("what_would_change_my_mind", "")))

            risk_flags = ai_decision.get("risk_flags", [])

            if isinstance(risk_flags, list) and risk_flags:
                risk_flags_html = "<ul>" + "".join(
                    f"<li>{escape(str(flag))}</li>"
                    for flag in risk_flags
                ) + "</ul>"
            else:
                risk_flags_html = "<span class='muted'>None</span>"

            action_html = "<span class='muted'>No AI sell action</span>"

            if ai_label in ["TRIM", "TAKE_PROFIT", "SELL", "CUT_LOSS"] and float(sell_pct or 0) > 0:
                action_html = f"""
                <form method="post" action="/positions/ai-sell">
                    <input type="hidden" name="ticker" value="{ticker}">
                    <input type="hidden" name="sell_pct" value="{sell_pct}">
                    <button type="submit">Paper sell {sell_pct}%</button>
                </form>
                <p class="muted small">Estimated qty: {float(qty_to_sell):.6f}</p>
                """

            rows += f"""
            <tr>
                <td><b>{ticker}</b></td>
                <td>{badge(ai_label, css_class)}</td>
                <td>{badge(rule_label, status_class(rule_label))}</td>
                <td>{confidence}</td>
                <td>{qty}</td>
                <td>{money(market_value)}</td>
                <td>{money(avg_entry)} / {money(current_price)}</td>
                <td>{money(unrealized_pl)}<br>{unrealized_plpc:.2f}%</td>
                <td>
                    <b>Thesis:</b> {thesis_status}<br>
                    <b>Rule agreement:</b> {rule_agreement}<br>
                    <b>Risks:</b> {risk_flags_html}
                </td>
                <td>
                    <b>Reasoning:</b> {reasoning}<br><br>
                    <b>Profit notes:</b> {profit_notes}<br><br>
                    <b>What changes its mind:</b> {what_changes}
                </td>
                <td>{action_html}</td>
            </tr>
            """

    manual_note = ""

    if manual_prompt_file:
        manual_note = f"""
        <div class="warning-callout" style="margin-top:16px;">
            LLM_MODE is manual, so the dashboard did not call the AI directly.
            Prompts were written to <b>{escape(str(manual_prompt_file))}</b>.
            Set LLM_MODE=codex in .env if you want the dashboard to run AI sell reviews automatically.
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus AI Position Review</title>
        {shared_css()}
    </head>
    <body>
        <p>
            <a href="/">← Back to Dashboard</a> |
            <a href="/positions">Rule-Based Position Monitor</a>
        </p>

        <div class="page-header">
            <h1>AI Position Review</h1>
            <p class="subtitle">
                Uses the Sell Analyst agent to review existing open positions after the rule-based monitor runs.
            </p>
        </div>

        <div class="grid grid-4">
            <div class="card">
                <div class="metric-title">Positions Reviewed</div>
                <div class="big-number">{summary.get("total_positions", 0)}</div>
            </div>

            <div class="card">
                <div class="metric-title">Take Profit / Trim</div>
                <div class="big-number">{summary.get("take_profit", 0)} / {summary.get("trim", 0)}</div>
            </div>

            <div class="card">
                <div class="metric-title">Sell / Cut Loss</div>
                <div class="big-number">{summary.get("sell", 0)} / {summary.get("cut_loss", 0)}</div>
            </div>

            <div class="card">
                <div class="metric-title">Hold / Watch</div>
                <div class="big-number">{summary.get("hold", 0)} / {summary.get("watch_closely", 0)}</div>
            </div>
        </div>

        <section class="card">
            <h2>Run AI Sell Review</h2>
            <p class="muted small">Last generated: {escape(str(generated_at))}</p>
            <p class="muted small">LLM mode: {escape(str(llm_mode))}</p>

            <form method="post" action="/positions/ai-review">
                <button type="submit">Run AI Review on Open Positions</button>
            </form>

            {manual_note}

            <div class="danger-callout" style="margin-top:16px;">
                Paper sell buttons still require ALPACA_PAPER=true and SELL_TRADING_ENABLED=true.
                Keep SELL_TRADING_ENABLED=false until you are ready to test paper sells.
            </div>
        </section>

        <section class="card">
            <h2>AI Sell Decisions</h2>
            <table>
                <tr>
                    <th>Ticker</th>
                    <th>AI Decision</th>
                    <th>Rule Decision</th>
                    <th>Confidence</th>
                    <th>Qty</th>
                    <th>Market Value</th>
                    <th>Entry / Current</th>
                    <th>Unrealized P/L</th>
                    <th>Status / Risks</th>
                    <th>AI Reasoning</th>
                    <th>Action</th>
                </tr>
                {rows}
            </table>
        </section>
    </body>
    </html>
    """

    return HTMLResponse(content=html)


@app.post("/positions/ai-review")
def run_ai_positions_review():
    run_ai_position_review(force_refresh_positions=True)
    return RedirectResponse(url="/positions/ai", status_code=303)


@app.post("/positions/ai-sell")
def sell_ai_position_from_dashboard(
    ticker: str = Form(...),
    sell_pct: float = Form(...),
):
    try:
        execute_ai_sell(ticker=ticker, sell_pct=sell_pct)
    except Exception as e:
        error_path = Path("ai_position_sell_error.json")
        error_path.write_text(json.dumps({
            "ticker": ticker,
            "sell_pct": sell_pct,
            "error": str(e),
            "timestamp": now_iso(),
        }, indent=2))

    run_ai_position_review(force_refresh_positions=True)
    return RedirectResponse(url="/positions/ai", status_code=303)


@app.get("/theses", response_class=HTMLResponse)
def trade_theses_page(request: Request):
    data = get_all_trade_theses()
    theses = data.get("theses", {})

    rows = ""

    if not theses:
        rows = """
        <tr>
            <td colspan="8">No trade theses have been saved yet. Run Deep Review first.</td>
        </tr>
        """
    else:
        for ticker, thesis in sorted(theses.items()):
            company = thesis.get("company", {})
            buy = thesis.get("buy_thesis", {})

            ticker_html = escape(str(ticker))
            final_status = escape(str(thesis.get("final_status", "UNKNOWN")))
            css_class = status_class(final_status)

            research_conf = buy.get("research_confidence", "")
            quant_strength = buy.get("quant_strength", "")
            pm_size = buy.get("pm_size_pct", "")

            research_reasoning = escape(str(buy.get("research_reasoning", "")))
            quant_reasoning = escape(str(buy.get("quant_reasoning", "")))
            pm_reasoning = escape(str(buy.get("pm_reasoning", "")))

            risk_reasons = buy.get("risk_reasons", [])
            if isinstance(risk_reasons, list) and risk_reasons:
                risk_html = "<ul>" + "".join(
                    f"<li>{escape(str(reason))}</li>"
                    for reason in risk_reasons[:5]
                ) + "</ul>"
            else:
                risk_html = "<span class='muted'>None</span>"

            rows += f"""
            <tr>
                <td><b>{ticker_html}</b></td>
                <td>{escape(str(company.get("name", ticker_html)))}</td>
                <td>{escape(str(company.get("sector", "Unknown")))}</td>
                <td>{badge(final_status, css_class)}</td>
                <td>Research: {research_conf}<br>Quant: {quant_strength}<br>Size: {pm_size}%</td>
                <td>
                    <b>Research:</b> {research_reasoning}<br><br>
                    <b>Quant:</b> {quant_reasoning}
                </td>
                <td>{pm_reasoning}</td>
                <td>{risk_html}</td>
            </tr>
            """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Trade Theses</title>
        {shared_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a> | <a href="/positions/ai">AI Position Review</a></p>

        <div class="page-header">
            <h1>Trade Thesis Memory</h1>
            <p class="subtitle">
                Saved original buy theses from Deep Review. The Sell Analyst uses this to decide
                whether a current holding still deserves to be held.
            </p>
        </div>

        <section class="card">
            <h2>Saved Theses</h2>
            <p class="muted small">Last updated: {escape(str(data.get("updated_at")))}</p>

            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Company</th>
                    <th>Sector</th>
                    <th>Status</th>
                    <th>Scores</th>
                    <th>Research / Quant Thesis</th>
                    <th>PM Reasoning</th>
                    <th>Risk Notes</th>
                </tr>
                {rows}
            </table>
        </section>
    </body>
    </html>
    """

    return HTMLResponse(content=html)
