from trade_thesis_store import get_all_trade_theses
import os
import json
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
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
    submit_paper_market_order,
)
from tools.web_research import get_web_research, get_company_profile
from fast_scan import run_fast_scan, get_fast_scan_results, get_fast_scan_status
from deep_review import run_deep_review, get_deep_review_results, get_deep_review_status, select_candidates_for_deep_review
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

TRADE_TEST_STATUS_PATH = Path("trade_test_status.json")
POSITION_REVIEW_STATUS_PATH = Path("position_review_status.json")
AI_POSITION_REVIEW_STATUS_PATH = Path("ai_position_review_status.json")

trade_test_lock = threading.Lock()
position_review_lock = threading.Lock()
ai_position_review_lock = threading.Lock()


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


def is_running_status(status):
    return str(status or "").lower() in ["running", "starting", "queued"]


def task_percent(status):
    try:
        if "percent" in status:
            return max(0, min(100, float(status.get("percent") or 0)))

        current = float(status.get("current") or 0)
        total = float(status.get("total") or 0)

        if total > 0:
            return max(0, min(100, (current / total) * 100))

    except Exception:
        pass

    return 0


def render_task_status(title, status):
    status = status or {}
    state = str(status.get("status", "not_started"))
    message = str(status.get("message", "No task has started yet."))
    current = status.get("current", 0)
    total = status.get("total", 0)
    current_ticker = status.get("current_ticker")
    error = status.get("error")
    percent = task_percent(status)
    running = is_running_status(state)

    spinner = '<span class="spinner"></span>' if running else ""
    refresh_note = "<p class='muted small'>This page auto-refreshes while the task is running.</p>" if running else ""

    ticker_line = ""
    if current_ticker:
        ticker_line = f"<p class='muted small'>Now reviewing: <b>{escape(str(current_ticker))}</b></p>"

    progress_line = ""
    if total:
        progress_line = f"<p class='muted small'>Progress: {escape(str(current))} / {escape(str(total))}</p>"

    error_line = ""
    if error:
        error_line = f"<p class='danger-callout'>Error: {escape(str(error))}</p>"

    return f"""
        <div class="task-status">
            <div class="task-head">
                <div>
                    <b>{spinner}{escape(str(title))}</b>
                    <p class="muted small" style="margin:6px 0 0 0;">{escape(message)}</p>
                </div>
                <div>{badge(state, status_class(state))}</div>
            </div>

            <div class="progress-shell">
                <div class="progress-bar" style="width: {percent:.1f}%;"></div>
            </div>

            <p><b>{percent:.1f}%</b> complete</p>
            {progress_line}
            {ticker_line}
            {error_line}
            {refresh_note}
        </div>
    """


def term(label, definition):
    return f'<span class="term" title="{escape(definition)}">{escape(label)}</span>'


def render_stock_glossary():
    terms = [
        ("RSI", "Relative Strength Index. A momentum reading from 0 to 100. Above 70 can mean overbought; below 30 can mean oversold."),
        ("MACD", "Moving Average Convergence Divergence. A trend/momentum indicator comparing short-term and long-term moving averages."),
        ("Volume Ratio", "Today/recent volume compared with normal volume. Higher means more market participation."),
        ("Bollinger Bands", "Price bands around a moving average. Price near the outer bands can mean stretched movement."),
        ("PM", "Portfolio Manager agent. This is the final AI decision layer before risk checks."),
        ("Risk Approved", "Whether the risk engine allowed the idea based on position size, exposure, drawdown, and safety rules."),
        ("Unrealized P/L", "Profit or loss on an open position that has not been sold yet."),
        ("Conviction", "How strong the combined research and technical evidence is."),
    ]

    cards = ""

    for label, definition in terms:
        cards += f"""
            <div class="glossary-item">
                <b>{escape(label)}</b>
                <p class="muted small">{escape(definition)}</p>
            </div>
        """

    return f"""
        <section class="card">
            <h2>Plain-English Stock Term Guide</h2>
            <p class="muted small">
                Hover over dotted terms in tables, or use these quick definitions.
            </p>
            <div class="glossary-grid">
                {cards}
            </div>
        </section>
    """


def run_ticker_background(ticker, notional_value=100):
    with trade_test_lock:
        write_json(TRADE_TEST_STATUS_PATH, {
            "status": "running",
            "message": f"Submitting user-directed paper trade for {ticker}.",
            "current": 0,
            "total": 1,
            "percent": 0,
            "current_ticker": ticker,
            "started_at": now_iso(),
            "finished_at": None,
            "error": None,
        })

        try:
            order = submit_paper_market_order(
                ticker=ticker,
                direction="long",
                notional_value=float(notional_value),
            )

            write_json(TRADE_TEST_STATUS_PATH, {
                "status": "complete",
                "message": f"Paper buy order submitted for {ticker}.",
                "current": 1,
                "total": 1,
                "percent": 100,
                "current_ticker": None,
                "started_at": None,
                "finished_at": now_iso(),
                "error": None,
                "order": order,
            })

        except Exception as e:
            write_json(TRADE_TEST_STATUS_PATH, {
                "status": "error",
                "message": f"Paper trade failed for {ticker}.",
                "current": 1,
                "total": 1,
                "percent": 100,
                "current_ticker": None,
                "finished_at": now_iso(),
                "error": str(e),
            })


def run_position_review_background():
    with position_review_lock:
        write_json(POSITION_REVIEW_STATUS_PATH, {
            "status": "running",
            "message": "Reviewing open positions.",
            "current": 0,
            "total": 1,
            "percent": 0,
            "started_at": now_iso(),
            "error": None,
        })

        try:
            review_all_positions()
            write_json(POSITION_REVIEW_STATUS_PATH, {
                "status": "complete",
                "message": "Position review complete.",
                "current": 1,
                "total": 1,
                "percent": 100,
                "finished_at": now_iso(),
                "error": None,
            })
        except Exception as e:
            write_json(POSITION_REVIEW_STATUS_PATH, {
                "status": "error",
                "message": "Position review failed.",
                "current": 1,
                "total": 1,
                "percent": 100,
                "finished_at": now_iso(),
                "error": str(e),
            })


def run_ai_position_review_background():
    with ai_position_review_lock:
        write_json(AI_POSITION_REVIEW_STATUS_PATH, {
            "status": "running",
            "message": "Running AI sell/hold review for open positions.",
            "current": 0,
            "total": 1,
            "percent": 0,
            "started_at": now_iso(),
            "error": None,
        })

        try:
            run_ai_position_review(force_refresh_positions=True)
            write_json(AI_POSITION_REVIEW_STATUS_PATH, {
                "status": "complete",
                "message": "AI position review complete.",
                "current": 1,
                "total": 1,
                "percent": 100,
                "finished_at": now_iso(),
                "error": None,
            })
        except Exception as e:
            write_json(AI_POSITION_REVIEW_STATUS_PATH, {
                "status": "error",
                "message": "AI position review failed.",
                "current": 1,
                "total": 1,
                "percent": 100,
                "finished_at": now_iso(),
                "error": str(e),
            })





def premium_ui_css():
    return """
    <style>
        :root {
            --premium-bg: #030712;
            --premium-panel: rgba(8, 13, 28, 0.82);
            --premium-panel-2: rgba(15, 23, 42, 0.72);
            --premium-border: rgba(148, 163, 184, 0.16);
            --premium-border-strong: rgba(96, 165, 250, 0.34);
            --premium-text: #f8fafc;
            --premium-muted: #9ca3af;
            --premium-blue: #60a5fa;
            --premium-cyan: #22d3ee;
            --premium-green: #34d399;
            --premium-purple: #a78bfa;
        }

        body {
            background:
                radial-gradient(circle at 18% 8%, rgba(96, 165, 250, 0.18), transparent 28%),
                radial-gradient(circle at 82% 14%, rgba(34, 211, 238, 0.13), transparent 28%),
                radial-gradient(circle at 48% 95%, rgba(167, 139, 250, 0.10), transparent 30%),
                linear-gradient(180deg, #030712 0%, #07111f 45%, #020617 100%) !important;
            color: var(--premium-text) !important;
            letter-spacing: -0.01em;
        }

        body::before {
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            background-image:
                linear-gradient(rgba(148, 163, 184, 0.035) 1px, transparent 1px),
                linear-gradient(90deg, rgba(148, 163, 184, 0.035) 1px, transparent 1px);
            background-size: 44px 44px;
            mask-image: linear-gradient(to bottom, rgba(0,0,0,0.55), transparent 75%);
            z-index: -1;
        }

        .page-header {
            position: relative;
        }

        .page-header::after {
            content: "";
            display: block;
            height: 1px;
            width: 100%;
            margin-top: 22px;
            background: linear-gradient(90deg, transparent, rgba(96,165,250,0.55), rgba(52,211,153,0.40), transparent);
        }

        h1 {
            background: linear-gradient(135deg, #ffffff 0%, #bfdbfe 35%, #67e8f9 72%, #ffffff 100%);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent !important;
            text-shadow: 0 0 34px rgba(96, 165, 250, 0.14);
        }

        h2, h3 {
            color: #f8fafc !important;
        }

        .subtitle, .muted, .small {
            color: #a7b3c7 !important;
        }

        .card,
        .card-soft,
        .allocation-card,
        .chart-card {
            background:
                linear-gradient(180deg, rgba(15, 23, 42, 0.82), rgba(2, 6, 23, 0.76)) !important;
            border: 1px solid var(--premium-border) !important;
            box-shadow:
                0 24px 70px rgba(0, 0, 0, 0.38),
                inset 0 1px 0 rgba(255, 255, 255, 0.035) !important;
            backdrop-filter: blur(22px) saturate(135%) !important;
            -webkit-backdrop-filter: blur(22px) saturate(135%) !important;
            position: relative;
            overflow: hidden;
        }

        .card::before,
        .allocation-card::before,
        .chart-card::before {
            content: "";
            position: absolute;
            inset: 0;
            pointer-events: none;
            background:
                linear-gradient(120deg, rgba(96,165,250,0.10), transparent 24%),
                radial-gradient(circle at top right, rgba(34,211,238,0.08), transparent 30%);
            opacity: 0.9;
        }

        .card > *,
        .allocation-card > *,
        .chart-card > * {
            position: relative;
            z-index: 1;
        }

        .card:hover,
        .allocation-card:hover,
        .chart-card:hover,
        .card-soft:hover {
            border-color: var(--premium-border-strong) !important;
            box-shadow:
                0 28px 80px rgba(0, 0, 0, 0.45),
                0 0 38px rgba(96, 165, 250, 0.08),
                inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
        }

        .metric-title {
            color: #93c5fd !important;
            letter-spacing: 0.12em !important;
            font-weight: 850 !important;
        }

        .big-number {
            color: #ffffff !important;
            text-shadow: 0 0 24px rgba(96, 165, 250, 0.18);
        }

        .btn,
        button {
            border-radius: 999px !important;
            border: 1px solid rgba(147, 197, 253, 0.20) !important;
            background:
                linear-gradient(135deg, rgba(37, 99, 235, 0.92), rgba(14, 165, 233, 0.82)) !important;
            box-shadow:
                0 14px 32px rgba(37, 99, 235, 0.22),
                inset 0 1px 0 rgba(255,255,255,0.12) !important;
            transition: transform 0.16s ease, box-shadow 0.16s ease, filter 0.16s ease !important;
        }

        .btn:hover,
        button:hover {
            transform: translateY(-2px) scale(1.01) !important;
            box-shadow:
                0 20px 42px rgba(37, 99, 235, 0.34),
                0 0 24px rgba(34, 211, 238, 0.14),
                inset 0 1px 0 rgba(255,255,255,0.18) !important;
            filter: brightness(1.08) !important;
        }

        .btn-dark {
            background:
                linear-gradient(135deg, rgba(30, 41, 59, 0.94), rgba(15, 23, 42, 0.94)) !important;
            border-color: rgba(148, 163, 184, 0.20) !important;
        }

        .btn-red {
            background:
                linear-gradient(135deg, rgba(220, 38, 38, 0.92), rgba(127, 29, 29, 0.92)) !important;
        }

        input, select {
            background: rgba(2, 6, 23, 0.72) !important;
            border: 1px solid rgba(148, 163, 184, 0.18) !important;
            border-radius: 999px !important;
            color: #f8fafc !important;
        }

        input:focus, select:focus {
            border-color: rgba(96, 165, 250, 0.75) !important;
            box-shadow: 0 0 0 4px rgba(96, 165, 250, 0.13) !important;
        }

        table {
            background: rgba(2, 6, 23, 0.58) !important;
            border: 1px solid rgba(148, 163, 184, 0.14) !important;
            border-radius: 18px !important;
            overflow: hidden !important;
        }

        th {
            background: rgba(15, 23, 42, 0.92) !important;
            color: #bfdbfe !important;
            font-weight: 900 !important;
            letter-spacing: 0.09em !important;
        }

        td {
            border-bottom: 1px solid rgba(148, 163, 184, 0.10) !important;
        }

        tr:hover td {
            background: rgba(37, 99, 235, 0.10) !important;
        }

        .badge {
            border-radius: 999px !important;
            font-weight: 900 !important;
            letter-spacing: 0.02em;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
        }

        .approved {
            background: rgba(16, 185, 129, 0.16) !important;
            color: #bbf7d0 !important;
            border-color: rgba(52, 211, 153, 0.32) !important;
        }

        .recommended {
            background: rgba(34, 211, 238, 0.14) !important;
            color: #a5f3fc !important;
            border-color: rgba(34, 211, 238, 0.32) !important;
        }

        .blocked {
            background: rgba(239, 68, 68, 0.15) !important;
            color: #fecaca !important;
            border-color: rgba(239, 68, 68, 0.34) !important;
        }

        .vetoed {
            background: rgba(245, 158, 11, 0.14) !important;
            color: #fde68a !important;
            border-color: rgba(245, 158, 11, 0.32) !important;
        }

        .neutral {
            background: rgba(96, 165, 250, 0.13) !important;
            color: #dbeafe !important;
            border-color: rgba(96, 165, 250, 0.28) !important;
        }

        .progress-shell {
            height: 18px !important;
            background: rgba(2, 6, 23, 0.82) !important;
            border: 1px solid rgba(148, 163, 184, 0.16) !important;
        }

        .progress-bar {
            background:
                linear-gradient(90deg, #2563eb, #22d3ee, #34d399) !important;
            box-shadow: 0 0 24px rgba(34, 211, 238, 0.32);
        }

        .line-svg polyline {
            filter: drop-shadow(0 0 10px rgba(96, 165, 250, 0.55));
        }

        .portfolio-point {
            filter: drop-shadow(0 0 8px rgba(147, 197, 253, 0.65));
        }

        .pie-wrap, .pie {
            filter: drop-shadow(0 0 26px rgba(96, 165, 250, 0.12));
        }

        .success-callout,
        .warning-callout,
        .danger-callout,
        .callout {
            border-radius: 18px !important;
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
        }

        ::selection {
            background: rgba(96, 165, 250, 0.38);
            color: white;
        }

        @media (max-width: 900px) {
            body {
                padding: 16px !important;
            }

            .card, .allocation-card {
                padding: 18px !important;
            }
        }
    
        .ai-review-card {
            background: rgba(2, 6, 23, 0.72);
            border: 1px solid rgba(148, 163, 184, 0.16);
            border-radius: 18px;
            padding: 18px;
            margin: 16px 0;
        }

        .ai-review-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 14px;
            margin-top: 12px;
        }

        .ai-review-box {
            background: rgba(15, 23, 42, 0.62);
            border: 1px solid rgba(148, 163, 184, 0.12);
            border-radius: 14px;
            padding: 14px;
            line-height: 1.55;
            max-height: none;
            overflow: visible;
            word-break: normal;
            overflow-wrap: anywhere;
        }

        .ai-review-box h4 {
            margin: 0 0 8px 0;
            color: #bfdbfe;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }

        details.ai-details {
            margin-top: 12px;
        }

        details.ai-details summary {
            cursor: pointer;
            color: #93c5fd;
            font-weight: 900;
            padding: 10px 0;
        }

        details.ai-details[open] summary {
            margin-bottom: 10px;
        }

        .wide-readable {
            white-space: normal !important;
            max-width: 760px;
            min-width: 320px;
            line-height: 1.55;
            overflow-wrap: anywhere;
        }

    </style>
    """


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


        .spinner {
            width: 18px;
            height: 18px;
            border: 3px solid rgba(148, 163, 184, 0.25);
            border-top-color: #60a5fa;
            border-radius: 999px;
            display: inline-block;
            animation: spin 0.8s linear infinite;
            vertical-align: middle;
            margin-right: 8px;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .task-status {
            margin-top: 14px;
            padding: 14px 16px;
            border-radius: 16px;
            border: 1px solid var(--border);
            background: rgba(2, 6, 23, 0.62);
        }

        .task-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
        }

        .term {
            border-bottom: 1px dotted rgba(147, 197, 253, 0.75);
            cursor: help;
            color: #bfdbfe;
            font-weight: 800;
        }

        .glossary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 12px;
            margin-top: 12px;
        }

        .glossary-item {
            background: rgba(2, 6, 23, 0.58);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 12px;
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
        {premium_ui_css()}
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
    logs = get_recent_trade_logs(limit=5)

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



# ===================== PORTFOLIO HOME UPGRADE HELPERS =====================

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def short_time(value):
    raw = str(value or "")
    if "T" in raw:
        return raw.split("T")[0]
    if " " in raw:
        return raw.split(" ")[0]
    return raw[:10]


def get_position_symbol(position):
    return str(
        position.get("symbol")
        or position.get("ticker")
        or position.get("asset")
        or ""
    ).upper().strip()


def position_pl_pct(position):
    market_value = safe_float(position.get("market_value"))
    unrealized_pl = safe_float(position.get("unrealized_pl"))
    cost_basis = market_value - unrealized_pl

    if cost_basis <= 0:
        return 0

    return (unrealized_pl / cost_basis) * 100


def get_company_profile_cache():
    return read_json(Path("company_profile_cache.json"), {})


def get_profile_for_ticker(ticker, cache):
    ticker = str(ticker or "").upper().strip()
    item = cache.get(ticker, {})

    if not isinstance(item, dict):
        return {}

    # tools/web_research.py stores cache entries as:
    # {"fetched_at": "...", "profile": {...}}
    if isinstance(item.get("profile"), dict):
        return item.get("profile", {})

    # Older/direct cache format fallback.
    return item


def get_position_sector(ticker, cache):
    ticker = str(ticker or "").upper().strip()
    profile = get_profile_for_ticker(ticker, cache)

    sector = (
        profile.get("sector")
        or profile.get("company_profile", {}).get("sector")
        or ""
    )

    sector = str(sector or "").strip()

    if sector and sector.lower() != "unknown":
        return sector

    # If the cache is missing or stale, do a live profile lookup.
    # This uses the existing SEC submissions API first, then Yahoo fallback.
    try:
        live_profile = get_company_profile(ticker, use_cache=True)
        live_sector = str(live_profile.get("sector") or "").strip()

        if live_sector and live_sector.lower() != "unknown":
            return live_sector

    except Exception as e:
        print(f"Could not resolve sector for {ticker}: {e}")

    # Do not show "Unknown" on the allocation chart.
    return "Other / Unclassified"


def save_portfolio_snapshot(account):
    try:
        init_db()

        portfolio_value = safe_float(account.get("portfolio_value"))
        cash = safe_float(account.get("cash"))
        buying_power = safe_float(account.get("buying_power"))

        if portfolio_value <= 0:
            return

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                portfolio_value REAL,
                cash REAL,
                buying_power REAL
            )
        """)

        cur.execute("""
            INSERT INTO portfolio_snapshots (
                timestamp,
                portfolio_value,
                cash,
                buying_power
            )
            VALUES (?, ?, ?, ?)
        """, (
            now_iso(),
            portfolio_value,
            cash,
            buying_power,
        ))

        conn.commit()
        conn.close()

    except Exception as e:
        print(f"Could not save portfolio snapshot: {e}")


def get_portfolio_history(limit=60):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                portfolio_value REAL,
                cash REAL,
                buying_power REAL
            )
        """)

        cur.execute("""
            SELECT timestamp, portfolio_value, cash, buying_power
            FROM portfolio_snapshots
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))

        rows = [dict(row) for row in cur.fetchall()]
        conn.close()

        return list(reversed(rows))

    except Exception as e:
        print(f"Could not load portfolio history: {e}")
        return []




def parse_iso_timestamp(value):
    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        raw = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def format_chart_timestamp(value):
    dt = parse_iso_timestamp(value)
    if not dt:
        return str(value or "")

    try:
        return dt.strftime("%Y-%m-%d %I:%M %p UTC")
    except Exception:
        return str(value or "")


def filter_history_by_range(history, chart_range="1M"):
    if not history:
        return history

    chart_range = str(chart_range or "1M").upper().strip()
    now = datetime.now(timezone.utc)

    range_map = {
        "1D": timedelta(days=1),
        "1W": timedelta(days=7),
        "1M": timedelta(days=30),
        "3M": timedelta(days=90),
        "ALL": None,
    }

    delta = range_map.get(chart_range, timedelta(days=30))

    if delta is None:
        return history

    cutoff = now - delta
    filtered = []

    for item in history:
        dt = parse_iso_timestamp(item.get("timestamp"))
        if dt and dt >= cutoff:
            filtered.append(item)

    # If the selected range has no points, show recent history instead of a blank chart.
    if filtered:
        return filtered

    return history[-min(len(history), 30):]


def build_chart_range_buttons(selected_range="1M"):
    selected_range = str(selected_range or "1M").upper().strip()
    options = ["1D", "1W", "1M", "3M", "ALL"]
    buttons = ""

    for option in options:
        btn_class = "btn btn-blue" if option == selected_range else "btn btn-dark"
        buttons += f'<a class="{btn_class}" href="/?chart_range={option}">{option}</a>'

    return f"""
        <div class="button-row" style="margin-bottom: 14px;">
            {buttons}
        </div>
    """

def build_portfolio_line_chart(history, selected_range="1M"):
    if not history:
        return "<p class='empty'>No portfolio snapshots yet. Reload the dashboard over time to build the graph.</p>"

    points = []

    for item in history:
        points.append({
            "timestamp": item.get("timestamp", ""),
            "value": safe_float(item.get("portfolio_value")),
        })

    if len(points) == 1:
        points = points * 2

    values = [p["value"] for p in points]
    min_v = min(values)
    max_v = max(values)
    spread = max(max_v - min_v, 1)

    width = 760
    height = 260
    pad_x = 38
    pad_y = 28

    coords = []

    for i, point in enumerate(points):
        x = pad_x + (i / max(len(points) - 1, 1)) * (width - pad_x * 2)
        y = height - pad_y - ((point["value"] - min_v) / spread) * (height - pad_y * 2)
        coords.append((x, y, point))

    polyline = " ".join([f"{x:.1f},{y:.1f}" for x, y, point in coords])
    area_points = f"{pad_x},{height-pad_y} " + polyline + f" {width-pad_x},{height-pad_y}"

    hover_zones = ""
    point_markers = ""

    for i, (x, y, point) in enumerate(coords):
        ts = str(point["timestamp"])
        value = point["value"]
        tooltip = escape(f"{format_chart_timestamp(ts)} | Portfolio value: {money(value)}")

        if i == 0:
            left_x = pad_x
        else:
            left_x = (coords[i - 1][0] + x) / 2

        if i == len(coords) - 1:
            right_x = width - pad_x
        else:
            right_x = (x + coords[i + 1][0]) / 2

        zone_width = max(right_x - left_x, 8)

        hover_zones += f"""
            <rect x="{left_x:.1f}" y="{pad_y}" width="{zone_width:.1f}" height="{height - pad_y * 2}"
                  fill="transparent">
                <title>{tooltip}</title>
            </rect>
        """

        point_markers += f"""
            <circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#93c5fd">
                <title>{tooltip}</title>
            </circle>
        """

    start_value = points[0]["value"]
    end_value = points[-1]["value"]
    change = end_value - start_value
    change_pct = (change / start_value * 100) if start_value else 0
    change_class = "approved" if change >= 0 else "blocked"

    selected_range = escape(str(selected_range or "1M").upper())

    return f"""
        <div class="chart-card">
            <div class="chart-header" style="display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap; align-items:flex-start;">
                <div>
                    <div class="metric-title">Portfolio Value Over Time · {selected_range}</div>
                    <div class="big-number">{money(end_value)}</div>
                    <div class="muted small">From {money(start_value)} to {money(end_value)}</div>
                </div>
                <div>{badge(f"{change:+,.2f} / {change_pct:+.1f}%", change_class)}</div>
            </div>

            <svg class="line-svg" viewBox="0 0 {width} {height}" preserveAspectRatio="none" style="width:100%; height:260px;">
                <defs>
                    <linearGradient id="portfolioFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stop-color="#3b82f6" stop-opacity="0.35"/>
                        <stop offset="100%" stop-color="#3b82f6" stop-opacity="0.02"/>
                    </linearGradient>
                </defs>

                <line x1="{pad_x}" y1="{height-pad_y}" x2="{width-pad_x}" y2="{height-pad_y}" stroke="rgba(148,163,184,0.35)" />
                <line x1="{pad_x}" y1="{pad_y}" x2="{pad_x}" y2="{height-pad_y}" stroke="rgba(148,163,184,0.20)" />

                <polygon points="{area_points}" fill="url(#portfolioFill)" />
                <polyline points="{polyline}" fill="none" stroke="#60a5fa" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" />

                {point_markers}
                {hover_zones}
            </svg>

            <p class="muted small">
                Hover over any dot or vertical area of the chart to see the exact timestamp and portfolio value.
            </p>
        </div>
    """

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
        {premium_ui_css()}
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

            {render_task_status("Market Screener", status)}
            <p class="muted">Generated At: {escape(str(results.get("generated_at")))}</p>

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
                    <th><span class="term" title="Relative Strength Index. Momentum from 0 to 100. Above 70 can be overbought; below 30 can be oversold.">RSI</span></th>
                    <th><span class="term" title="Moving Average Convergence Divergence. Trend/momentum signal based on moving averages.">MACD</span></th>
                    <th><span class="term" title="Current volume compared with normal volume. Higher means more participation.">Volume Ratio</span></th>
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
        {premium_ui_css()}
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
                    <th><span class="term" title="Whether the risk engine allowed the trade after safety checks.">Risk Approved</span></th>
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
        {premium_ui_css()}
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
                    <th><span class="term" title="Relative Strength Index. Momentum from 0 to 100. Above 70 can be overbought; below 30 can be oversold.">RSI</span></th>
                    <th><span class="term" title="Moving Average Convergence Divergence. Trend/momentum signal based on moving averages.">MACD</span></th>
                    <th><span class="term" title="Current volume compared with normal volume. Higher means more participation.">Volume Ratio</span></th>
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
        {premium_ui_css()}
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




def get_deep_review_candidate_map(max_candidates=8):
    """
    Returns tickers selected for Deep AI Review from the latest Fast Scan results.

    Example:
    {
        "AAPL": 1,
        "NVDA": 2
    }
    """
    try:
        selection = select_candidates_for_deep_review(
            max_candidates=max_candidates,
            only_quality_approved=True,
            include_previous_errors=True,
        )

        selected = selection.get("selected", []) or []
        output = {}

        for idx, item in enumerate(selected, start=1):
            ticker = str(item.get("ticker", "")).upper().strip()
            if ticker:
                output[ticker] = idx

        return output

    except Exception as e:
        print(f"Could not build deep review candidate map: {e}")
        return {}


def get_deep_review_suggestions_html(max_candidates=8):
    """
    Builds the green candidate box on /deep-review.
    Safe fallback if Fast Scan has not been run yet.
    """
    try:
        selection = select_candidates_for_deep_review(
            max_candidates=max_candidates,
            only_quality_approved=True,
            include_previous_errors=True,
        )

        tickers = [
            str(t).upper().strip()
            for t in selection.get("selected_tickers", [])
            if t
        ]

        if not tickers:
            return """
                <div class="warning-callout">
                    No eligible Fast Scan candidates found yet. Run Fast Scan first.
                </div>
            """

        chips = " ".join([
            f"<span class='badge approved'>{escape(ticker)}</span>"
            for ticker in tickers
        ])

        return f"""
            <div class="success-callout">
                <b>Auto-selected from Fast Scan:</b>
                <div style="margin-top:10px;">{chips}</div>
                <p class="small muted" style="margin-bottom:0;">
                    Leave the manual ticker box blank and Deep AI Review will use these automatically.
                </p>
            </div>
        """

    except Exception as e:
        return f"""
            <div class="warning-callout">
                Could not load Fast Scan candidates yet: {escape(str(e))}
            </div>
        """

@app.get("/fast-scan", response_class=HTMLResponse)
def fast_scan_page():
    results = get_fast_scan_results()
    status = get_fast_scan_status()
    candidates = results.get("candidates", []) or []

    deep_review_candidate_map = get_deep_review_candidate_map(max_candidates=8)

    rows = ""

    for item in candidates:
        ticker = escape(str(item.get("ticker", "")))
        ticker_raw = str(item.get("ticker", "")).upper().strip()
        company = escape(str(item.get("company_name", item.get("name", ""))))
        sector = escape(str(item.get("sector", "")))
        industry = escape(str(item.get("industry", "")))

        score = item.get("score")
        if score is None:
            score = item.get("fast_scan_score", item.get("candidate_score", ""))
        score = escape(str(score))

        technicals = item.get("technical_summary", {}) or {}
        close = escape(str(
            item.get("close")
            or technicals.get("close")
            or technicals.get("latest_close")
            or ""
        ))
        rsi = escape(str(technicals.get("rsi", item.get("rsi", ""))))
        volume_ratio = escape(str(technicals.get("volume_ratio", item.get("volume_ratio", ""))))

        quality = item.get("quality_review", {}) or {}
        approved = bool(quality.get("approved"))
        quality_label = "approved" if approved else "rejected"
        quality_css = "approved" if approved else "blocked"

        reasons = quality.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = [str(reasons)]

        reason_html = "<br>".join([escape(str(r)) for r in reasons[:3]]) if reasons else ""

        deep_rank = deep_review_candidate_map.get(ticker_raw)
        if deep_rank:
            deep_scan_html = f"{badge('validated for deep scan', 'approved')}<div class='muted small'>Deep scan rank #{deep_rank}</div>"
            row_style = " style='box-shadow: inset 4px 0 0 rgba(34, 197, 94, 0.85);'"
        else:
            deep_scan_html = "<span class='muted small'>not moving on</span>"
            row_style = ""

        rows += f"""
        <tr{row_style}>
            <td><b>{ticker}</b></td>
            <td>{deep_scan_html}</td>
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
            <td colspan="10">No fast scan candidates yet.</td>
        </tr>
        """

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
        {premium_ui_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a></p>

        <div class="page-header">
            <h1>Fast Scan</h1>
            <p class="subtitle">
                Fast Scan ranks stocks quantitatively first. Rows with the green
                <b>validated for deep scan</b> badge are automatically moved into Deep AI Review
                if you leave the Deep Review ticker box blank.
            </p>
        </div>

        <section class="card">
            <h2>Run Fast Scan</h2>

            {render_task_status("Fast Scan", status)}

            <form method="post" action="/run-fast-scan" class="loading-form">
                <input type="number" name="top_n" value="25" min="1" max="200">
                <input type="text" name="max_symbols" placeholder="max symbols, or all">
                <button type="submit">Run Fast Scan</button>
            </form>
        </section>

        <section class="card">
            <h2>Fast Scan Candidates</h2>
            <table>
                <tr>
                    <th>Ticker</th>
                    <th>Deep Scan</th>
                    <th>Company</th>
                    <th>Sector</th>
                    <th>Industry</th>
                    <th>Fast Scan Score</th>
                    <th>Close</th>
                    <th>RSI</th>
                    <th>Volume Ratio</th>
                    <th>Quality</th>
                </tr>
                {rows}
            </table>
        </section>
    </body>
    </html>
    """

    return HTMLResponse(html)

@app.post("/run-deep-review")
def run_deep_review_route(
    tickers: str = Form(""),
    allow_execution: str = Form(None),
):
    if deep_review_lock.locked():
        return RedirectResponse(url="/deep-review", status_code=303)

    parsed_tickers = [t.strip().upper() for t in str(tickers or "").replace("\n", ",").split(",") if t.strip()]
    execute = bool(allow_execution)

    # Blank ticker input means: automatically use best Fast Scan candidates.
    tickers_for_review = parsed_tickers if parsed_tickers else None

    thread = threading.Thread(
        target=run_deep_review_background,
        args=(tickers_for_review, execute),
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url="/deep-review", status_code=303)


@app.get("/deep-review", response_class=HTMLResponse)
def deep_review_page():
    deep_review_suggestions_html = get_deep_review_suggestions_html(max_candidates=8)
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
        {premium_ui_css()}
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
            {render_task_status("Deep AI Review", status)}

            {deep_review_suggestions_html}

            <form method="post" action="/run-deep-review" class="loading-form">
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
                    <th><span class="term" title="Whether the risk engine allowed the trade after safety checks.">Risk Approved</span></th>
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

    status = read_json(POSITION_REVIEW_STATUS_PATH, {
        "status": "not_started",
        "message": "No position review is running.",
    })

    refresh_tag = ""
    if is_running_status(status.get("status")):
        refresh_tag = '<meta http-equiv="refresh" content="5">'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Position Monitor</title>
        {refresh_tag}
        {shared_css()}
        {premium_ui_css()}
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
                    <th><span class="term" title="Open profit or loss before selling the position.">Unrealized P/L</span></th>
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
    if position_review_lock.locked():
        return RedirectResponse(url="/positions", status_code=303)

    thread = threading.Thread(
        target=run_position_review_background,
        daemon=True,
    )
    thread.start()

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
def ai_position_review_page():
    data = get_ai_position_review()
    summary = data.get("summary", {}) or {}
    reviews = data.get("reviews", []) or []
    generated_at = data.get("generated_at")
    llm_mode = data.get("llm_mode", "unknown")

    status = read_json(AI_POSITION_REVIEW_STATUS_PATH, {
        "status": "not_started",
        "message": "No AI position review is running.",
    })

    refresh_tag = ""
    if is_running_status(status.get("status")):
        refresh_tag = '<meta http-equiv="refresh" content="5">'

    cards = ""

    for item in reviews:
        ticker = escape(str(item.get("ticker", "")))
        position_review = item.get("position_review", {}) or {}
        position = position_review.get("position", {}) or {}
        rule_decision = position_review.get("decision", {}) or {}
        ai = item.get("ai_decision", {}) or {}

        ai_decision = escape(str(ai.get("decision", "UNKNOWN")).replace("_", " "))
        ai_css = status_class(ai_decision)
        confidence = safe_float(ai.get("confidence"))
        sell_pct = safe_float(ai.get("sell_pct"))
        estimated_qty_to_sell = safe_float(ai.get("estimated_qty_to_sell"))
        reasoning = escape(str(ai.get("reasoning", "")))
        rule_agreement = escape(str(ai.get("rule_agreement", "unknown")).replace("_", " "))
        thesis_status = escape(str(ai.get("thesis_status", "unknown")).replace("_", " "))
        profit_notes = escape(str(ai.get("profit_protection_notes", "")))
        change_mind = escape(str(ai.get("what_would_change_my_mind", "")))

        risk_flags = ai.get("risk_flags", [])
        if not isinstance(risk_flags, list):
            risk_flags = [str(risk_flags)]
        risk_html = "".join([f"<li>{escape(str(flag))}</li>" for flag in risk_flags]) or "<li>No major AI risk flags listed.</li>"

        rule_action = escape(str(rule_decision.get("action", rule_decision.get("decision", "unknown"))).replace("_", " "))
        rule_reason = escape(str(rule_decision.get("reasoning", rule_decision.get("reason", ""))))

        qty = escape(str(position.get("qty", "")))
        market_value = money(position.get("market_value", 0))
        unrealized_pl = money(position.get("unrealized_pl", 0))
        unrealized_plpc = pct(safe_float(position.get("unrealized_plpc", 0)) * 100)
        avg_entry = money(position.get("avg_entry_price", 0))
        current_price = money(position.get("current_price", position.get("market_price", 0)))

        sell_form = ""
        if sell_pct > 0:
            sell_form = f"""
                <form method="post" action="/positions/ai-sell" style="margin-top:12px;">
                    <input type="hidden" name="ticker" value="{ticker}">
                    <input type="hidden" name="sell_pct" value="{sell_pct}">
                    <button type="submit" class="btn btn-red">
                        Submit Suggested Paper Sell {sell_pct:.0f}%
                    </button>
                </form>
            """
        else:
            sell_form = "<p class='muted small'>No sell order suggested by AI.</p>"

        cards += f"""
            <div class="ai-review-card">
                <div style="display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap; align-items:flex-start;">
                    <div>
                        <h2 style="margin-bottom:6px;">{ticker}</h2>
                        <div class="button-row" style="margin-top:0;">
                            {badge(ai_decision, ai_css)}
                            {badge(f"Confidence {confidence:.0f}/100", "neutral")}
                            {badge(f"Sell {sell_pct:.0f}%", "recommended" if sell_pct > 0 else "neutral")}
                            {badge(f"Thesis {thesis_status}", "neutral")}
                        </div>
                    </div>
                    <div>
                        {sell_form}
                    </div>
                </div>

                <div class="ai-review-grid">
                    <div class="ai-review-box">
                        <h4>Position</h4>
                        <p><b>Qty:</b> {qty}</p>
                        <p><b>Market value:</b> {market_value}</p>
                        <p><b>Entry / current:</b> {avg_entry} → {current_price}</p>
                        <p><b>Unrealized P/L:</b> {unrealized_pl} / {unrealized_plpc}</p>
                    </div>

                    <div class="ai-review-box">
                        <h4>Rule-based decision</h4>
                        <p><b>Action:</b> {rule_action}</p>
                        <p>{rule_reason}</p>
                    </div>

                    <div class="ai-review-box">
                        <h4>AI decision</h4>
                        <p><b>Rule agreement:</b> {rule_agreement}</p>
                        <p><b>Estimated qty to sell:</b> {estimated_qty_to_sell:.4f}</p>
                        <p><b>Profit protection:</b> {profit_notes or "No special profit protection notes."}</p>
                    </div>
                </div>

                <details class="ai-details" open>
                    <summary>Full AI reasoning and risks</summary>

                    <div class="ai-review-grid">
                        <div class="ai-review-box">
                            <h4>AI reasoning</h4>
                            <p>{reasoning or "No AI reasoning found."}</p>
                        </div>

                        <div class="ai-review-box">
                            <h4>Risk flags</h4>
                            <ul>{risk_html}</ul>
                        </div>

                        <div class="ai-review-box">
                            <h4>What would change the AI's mind</h4>
                            <p>{change_mind or "No condition listed."}</p>
                        </div>
                    </div>
                </details>
            </div>
        """

    if not cards:
        cards = "<p class='empty'>No AI position reviews yet. Run the AI sell review first.</p>"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus AI Position Review</title>
        {refresh_tag}
        {shared_css()}
        {premium_ui_css()}
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
                This page now uses readable cards instead of a cramped table.
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

            {render_task_status("AI Position Review", status)}

            <form method="post" action="/positions/ai-review" class="loading-form">
                <button type="submit">Run AI Review on Open Positions</button>
            </form>

            <div class="danger-callout" style="margin-top:14px;">
                Paper sell buttons still require ALPACA_PAPER=true and SELL_TRADING_ENABLED=true.
                Keep SELL_TRADING_ENABLED=false until you are ready to test paper sells.
            </div>
        </section>

        <section class="card">
            <h2>AI Sell Decisions</h2>
            {cards}
        </section>
    </body>
    </html>
    """

    return HTMLResponse(html)

@app.post("/positions/ai-review")
def run_ai_positions_review():
    if ai_position_review_lock.locked():
        return RedirectResponse(url="/positions/ai", status_code=303)

    thread = threading.Thread(
        target=run_ai_position_review_background,
        daemon=True,
    )
    thread.start()

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
        {premium_ui_css()}
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
                    <th><span class="term" title="Portfolio Manager agent explanation for approve, veto, or block decision.">PM Reasoning</span></th>
                    <th>Risk Notes</th>
                </tr>
                {rows}
            </table>
        </section>
    </body>
    </html>
    """

    return HTMLResponse(content=html)

@app.get("/screening-info", response_class=HTMLResponse)
def screening_info_page():
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Stock Screening Process</title>
        {shared_css()}
        {premium_ui_css()}
    </head>
    <body>
        <div class="page-header">
            <h1>Stock Screening Process Info</h1>
            <p class="subtitle">
                This page explains how Olympus Capital picks potential stocks in simple language.
                The goal is not to blindly buy a stock. The system narrows the market down,
                checks quality, reviews technical signals, looks at news, then lets the AI agents
                and risk engine decide whether the idea is strong enough.
            </p>
            <a class="btn btn-dark" href="/">Back to Dashboard</a>
        </div>

        <section class="card">
            <h2>Simple Version</h2>
            <div class="grid grid-4">
                <div class="card-soft">
                    <div class="tool-number">1</div>
                    <h3>Find Active Stocks</h3>
                    <p class="muted">
                        The screener looks for stocks with enough price movement and volume to be worth reviewing.
                    </p>
                </div>
                <div class="card-soft">
                    <div class="tool-number">2</div>
                    <h3>Remove Weak Names</h3>
                    <p class="muted">
                        Low-quality, illiquid, or risky candidates are filtered out before wasting AI review time.
                    </p>
                </div>
                <div class="card-soft">
                    <div class="tool-number">3</div>
                    <h3>Score Technicals</h3>
                    <p class="muted">
                        Olympus checks momentum, trend, volume, and price behavior using indicators like RSI and MACD.
                    </p>
                </div>
                <div class="card-soft">
                    <div class="tool-number">4</div>
                    <h3>AI + Risk Review</h3>
                    <p class="muted">
                        Research, quant, portfolio, and risk agents decide whether the idea is good enough to recommend or trade.
                    </p>
                </div>
            </div>
        </section>

        <section class="card">
            <h2>Quant Criteria: What Makes a Stock Look Good?</h2>
            <table>
                <tr>
                    <th>Signal</th>
                    <th>What It Means</th>
                    <th>Good Sign</th>
                    <th>Bad / Caution Sign</th>
                </tr>
                <tr>
                    <td><b>Price Momentum</b></td>
                    <td>Whether the stock is moving strongly instead of sitting flat.</td>
                    <td>Price is trending upward with strength.</td>
                    <td>Price is choppy, flat, or breaking down.</td>
                </tr>
                <tr>
                    <td><b>Volume</b></td>
                    <td>How much trading activity the stock has.</td>
                    <td>Higher-than-normal volume confirms real interest.</td>
                    <td>Low volume can mean the move is weak or hard to trade.</td>
                </tr>
                <tr>
                    <td><b>RSI</b></td>
                    <td>Momentum score from 0 to 100.</td>
                    <td>Strong but not overheated. Often 50-70 is healthier than extreme levels.</td>
                    <td>Above 70 may be overbought. Below 30 may mean weak or oversold.</td>
                </tr>
                <tr>
                    <td><b>MACD</b></td>
                    <td>Trend and momentum indicator based on moving averages.</td>
                    <td>MACD above signal line can support a long setup.</td>
                    <td>MACD below signal line can show weakening momentum.</td>
                </tr>
                <tr>
                    <td><b>Bollinger Bands</b></td>
                    <td>Shows whether price is stretched compared with its recent average.</td>
                    <td>Price near support with improving momentum can be interesting.</td>
                    <td>Price stretched too far upward may be risky to chase.</td>
                </tr>
                <tr>
                    <td><b>Liquidity</b></td>
                    <td>Whether the stock trades enough shares/dollars per day.</td>
                    <td>Easy to enter and exit without weird fills.</td>
                    <td>Thinly traded stocks are riskier and can move unpredictably.</td>
                </tr>
            </table>
        </section>

        <section class="card">
            <h2>Quality Filters</h2>
            <p class="muted">
                Before a ticker reaches deep AI review, Olympus should prefer stocks that are tradable,
                liquid, and not obviously low-quality.
            </p>

            <div class="grid grid-3">
                <div class="card-soft">
                    <h3>Minimum Price</h3>
                    <p>
                        Very low-priced stocks are avoided because they can be more volatile,
                        easier to manipulate, and less reliable for a small systematic strategy.
                    </p>
                </div>
                <div class="card-soft">
                    <h3>Minimum Volume</h3>
                    <p>
                        The stock should trade enough shares so entries and exits are realistic.
                        This helps avoid names that barely trade.
                    </p>
                </div>
                <div class="card-soft">
                    <h3>Minimum Dollar Volume</h3>
                    <p>
                        Dollar volume checks whether meaningful money is actually trading in the stock,
                        not just a lot of cheap shares.
                    </p>
                </div>
            </div>
        </section>

        <section class="card">
            <h2>How the AI Agents Use the Screened Stocks</h2>
            <table>
                <tr>
                    <th>Stage</th>
                    <th>Role</th>
                    <th>Plain-English Meaning</th>
                </tr>
                <tr>
                    <td><b>Research Analyst</b></td>
                    <td>Reviews company news, sector context, and market sentiment.</td>
                    <td>Is there a real reason this stock could move?</td>
                </tr>
                <tr>
                    <td><b>Quant Analyst</b></td>
                    <td>Reviews chart-based signals like RSI, MACD, volume, and trend.</td>
                    <td>Does the price action support the trade idea?</td>
                </tr>
                <tr>
                    <td><b>Portfolio Manager</b></td>
                    <td>Combines research and technical evidence into a final trade opinion.</td>
                    <td>Are the story and the chart strong enough together?</td>
                </tr>
                <tr>
                    <td><b>Risk Engine</b></td>
                    <td>Checks position sizing, exposure, sector concentration, and safety rules.</td>
                    <td>Even if the idea looks good, is it safe to place?</td>
                </tr>
            </table>
        </section>

        <section class="card">
            <h2>Current Ideal Candidate</h2>
            <div class="success-callout">
                A strong candidate is liquid, actively moving, supported by volume,
                has technical momentum, has understandable news or sector support,
                and passes risk checks without overconcentrating the portfolio.
            </div>

            <div class="warning-callout" style="margin-top: 14px;">
                A stock should not be picked just because it has one good indicator.
                Olympus should prefer agreement between price action, volume, news, sector context,
                and risk controls.
            </div>
        </section>

        <section class="card">
            <h2>Term Definitions</h2>
            {render_stock_glossary()}
        </section>
    </body>
    </html>
    """

    return HTMLResponse(html)

@app.get("/agent-decisions", response_class=HTMLResponse)
def all_agent_decisions_page():
    logs = get_recent_trade_logs(limit=500)

    rows = ""
    for log in logs:
        research = log.get("research_brief", {}) or {}
        quant = log.get("quant_signal", {}) or {}
        pm = log.get("pm_decision", {}) or {}
        risk = log.get("risk_result", {}) or {}

        status = str(log.get("final_status", ""))
        css = status_class(status)

        rows += f"""
            <tr>
                <td>{escape(str(log.get("timestamp", "")))}</td>
                <td><b>{escape(str(log.get("ticker", "")))}</b></td>
                <td>{badge(status, css)}</td>
                <td>{escape(str(research.get("sentiment", "")))} / {escape(str(research.get("confidence", "")))}</td>
                <td>{escape(str(quant.get("direction", "")))} / {escape(str(quant.get("strength", "")))}</td>
                <td>{escape(str(pm.get("decision", "")))} / {escape(str(pm.get("direction", "")))}</td>
                <td>{escape(str(risk.get("approved", "")))}</td>
                <td>{escape(str(pm.get("reasoning", "")))}</td>
            </tr>
        """

    if not rows:
        rows = """
            <tr>
                <td colspan="8" class="empty">No agent decisions found yet.</td>
            </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>All Agent Decisions</title>
        {shared_css()}
        {premium_ui_css()}
    </head>
    <body>
        <p><a href="/">← Back to Dashboard</a></p>

        <div class="page-header">
            <h1>All Agent Decisions</h1>
            <p class="subtitle">
                Chronological history of agent decisions, ordered from most recent to oldest.
            </p>
        </div>

        <section class="card">
            <table>
                <tr>
                    <th>Time</th>
                    <th>Ticker</th>
                    <th>Status</th>
                    <th>Research</th>
                    <th>Quant</th>
                    <th>PM</th>
                    <th>Risk Approved</th>
                    <th>Reasoning</th>
                </tr>
                {rows}
            </table>
        </section>
    </body>
    </html>
    """

    return HTMLResponse(html)





def build_pie_chart(items, title):
    """
    Safe fallback allocation card.
    Shows allocation as a simple table if the pie helper was lost during patches.
    """
    clean = []
    total = 0.0

    for label, value in items or []:
        value = safe_float(value)
        if value > 0:
            clean.append((str(label), value))
            total += value

    if not clean or total <= 0:
        return f"""
            <div class="card">
                <h2>{escape(str(title))}</h2>
                <p class="empty">No allocation data available.</p>
            </div>
        """

    rows = ""
    for label, value in clean[:12]:
        pct_value = (value / total) * 100 if total else 0
        rows += f"""
            <tr>
                <td><b>{escape(str(label))}</b></td>
                <td>{money(value)}</td>
                <td>{pct_value:.1f}%</td>
            </tr>
        """

    return f"""
        <div class="card">
            <h2>{escape(str(title))}</h2>
            <table>
                <tr>
                    <th>Name</th>
                    <th>Value</th>
                    <th>Allocation</th>
                </tr>
                {rows}
            </table>
        </div>
    """

def build_portfolio_pies(positions):
    """
    Safe fallback portfolio allocation renderer.
    Builds company and sector allocation cards from current open positions.
    """
    if not positions:
        empty = """
            <div class="card">
                <h2>Allocation</h2>
                <p class="empty">No open positions yet.</p>
            </div>
        """
        return empty, empty

    cache = get_company_profile_cache() if "get_company_profile_cache" in globals() else {}

    by_company = []
    by_sector = {}

    for position in positions:
        ticker = get_position_symbol(position)
        market_value = safe_float(position.get("market_value"))

        if not ticker or market_value <= 0:
            continue

        sector = "Other / Unclassified"
        try:
            sector = get_position_sector(ticker, cache)
        except Exception:
            sector = "Other / Unclassified"

        by_company.append((ticker, market_value))
        by_sector[sector] = by_sector.get(sector, 0) + market_value

    by_company = sorted(by_company, key=lambda x: x[1], reverse=True)
    by_sector = sorted(by_sector.items(), key=lambda x: x[1], reverse=True)

    company_html = build_pie_chart(by_company, "Company Allocation")
    sector_html = build_pie_chart(by_sector, "Sector Allocation")

    return company_html, sector_html



def build_position_intelligence_table(positions):
    """
    Safe fallback position table.
    Shows current holdings and simple P/L status if the richer AI position helper was lost.
    """
    if not positions:
        return "<p class='empty'>No open positions yet.</p>"

    rows = ""

    for position in sorted(positions, key=lambda p: safe_float(p.get("market_value")), reverse=True):
        ticker = get_position_symbol(position)
        market_value = safe_float(position.get("market_value"))
        unrealized_pl = safe_float(position.get("unrealized_pl"))
        pl_pct = position_pl_pct(position)

        pl_class = "approved" if unrealized_pl >= 0 else "blocked"

        if pl_pct <= -8:
            action = badge("review / cut loss", "blocked")
            reasoning = "Position is down heavily. Review whether the thesis is still valid."
        elif pl_pct >= 15:
            action = badge("consider taking profit", "recommended")
            reasoning = "Position is up strongly. Consider protecting gains."
        elif pl_pct >= 0:
            action = badge("hold", "approved")
            reasoning = "Position is profitable. Holding may be reasonable unless thesis weakens."
        else:
            action = badge("watch closely", "vetoed")
            reasoning = "Position is down but not at automatic cut-loss level."

        rows += f"""
            <tr>
                <td><b>{escape(str(ticker))}</b></td>
                <td>{money(market_value)}</td>
                <td>{badge(f"{money(unrealized_pl)} / {pl_pct:+.1f}%", pl_class)}</td>
                <td>{action}</td>
                <td>{escape(reasoning)}</td>
            </tr>
        """

    return f"""
        <table>
            <tr>
                <th>Ticker</th>
                <th>Market Value</th>
                <th>Unrealized P/L</th>
                <th>Suggested Action</th>
                <th>Reasoning</th>
            </tr>
            {rows}
        </table>
    """

@app.get("/", response_class=HTMLResponse)
def home(chart_range: str = "1M"):
    try:
        account, positions = get_cached_account_and_positions()
        save_portfolio_snapshot(account)

        portfolio_history = get_portfolio_history(limit=500)
        filtered_portfolio_history = filter_history_by_range(portfolio_history, chart_range)
        chart_range_buttons = build_chart_range_buttons(chart_range)
        portfolio_chart = build_portfolio_line_chart(filtered_portfolio_history, chart_range)

        company_pie, sector_pie = build_portfolio_pies(positions)
        intelligence_table = build_position_intelligence_table(positions)

        logs = get_recent_trade_logs(limit=5)
        summary = summarize_logs(get_today_trade_logs())

        log_rows = ""
        for log in logs:
            ticker = escape(str(log.get("ticker", "")))
            status = escape(str(log.get("final_status", "")))
            css = status_class(status)
            timestamp = escape(str(log.get("timestamp", "")))

            pm = log.get("pm_decision", {}) or {}
            reasoning = escape(str(pm.get("reasoning", "")))

            log_rows += f"""
                <tr>
                    <td>{timestamp}</td>
                    <td><b>{ticker}</b></td>
                    <td>{badge(status, css)}</td>
                    <td>{reasoning}</td>
                </tr>
            """

        if not log_rows:
            log_rows = "<tr><td colspan='4' class='empty'>No recent decisions yet.</td></tr>"

        trade_test_status = read_json(TRADE_TEST_STATUS_PATH, {
            "status": "not_started",
            "message": "No paper trade submission is running.",
        })

        refresh_tag = ""
        if is_running_status(trade_test_status.get("status")):
            refresh_tag = '<meta http-equiv="refresh" content="5">'

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Olympus Capital Dashboard</title>
            {refresh_tag}
            {shared_css()}
        {premium_ui_css()}
        </head>
        <body>
            <div class="page-header">
                <h1>Olympus Capital</h1>
                <p class="subtitle">Portfolio dashboard, paper trading controls, scans, reviews, and agent decisions.</p>

                <div class="button-row">
                    <a class="btn btn-dark" href="/fast-scan">Fast Scan</a>
                    <a class="btn btn-dark" href="/deep-review">Deep AI Review</a>
                    <a class="btn btn-dark" href="/positions">Positions</a>
                    <a class="btn btn-dark" href="/agent-decisions">All Agent Decisions</a>
                    <a class="btn btn-dark" href="/screening-info">Screening Info</a>
                </div>
            </div>

            <section class="card">
                <h2>Portfolio Value Over Time</h2>
                {chart_range_buttons}
                {portfolio_chart}
            </section>

            <div class="grid grid-2">
                {company_pie}
                {sector_pie}
            </div>

            <section class="card" id="run-ticker">
                <h2>Submit Paper Trade on Ticker</h2>
                <div class="warning-callout">
                    User-directed paper trade submitter. This bypasses AI veto/risk approval, but still uses broker safety checks.
                </div>

                {render_task_status("Submit Paper Trade on Ticker", trade_test_status)}

                <form method="post" action="/run-ticker" class="loading-form" style="margin-top:16px;">
                    <input type="text" name="ticker" placeholder="Enter ticker, e.g. TSLA" required>
                    <input type="number" name="notional_value" value="100" min="1" step="1" placeholder="Paper trade dollars">
                    <button type="submit">Submit Paper Trade</button>
                </form>
            </section>

            <h2 class="section-title">Today's Decision Summary</h2>
            <div class="grid grid-4">
                <div class="card"><div class="metric-title">Recommended</div><div class="big-number">{summary.get("recommended", 0)}</div></div>
                <div class="card"><div class="metric-title">Submitted</div><div class="big-number">{summary.get("submitted", 0)}</div></div>
                <div class="card"><div class="metric-title">Vetoed</div><div class="big-number">{summary.get("vetoed", 0)}</div></div>
                <div class="card"><div class="metric-title">Blocked / Errors</div><div class="big-number">{summary.get("blocked", 0) + summary.get("errors", 0)}</div></div>
            </div>

            <section class="card">
                <h2>Position Intelligence</h2>
                {intelligence_table}
            </section>

            <section class="card">
                <h2>Recent Agent Decisions</h2>
                <div class="button-row">
                    <a class="btn btn-dark" href="/agent-decisions">View All Agent Decisions</a>
                </div>
                <table>
                    <tr>
                        <th>Time</th>
                        <th>Ticker</th>
                        <th>Status</th>
                        <th>Reasoning</th>
                    </tr>
                    {log_rows}
                </table>
            </section>
        </body>
        </html>
        """

        return HTMLResponse(html)

    except Exception as e:
        return HTMLResponse(
            f"""
            <h1>Dashboard failed to load</h1>
            <pre>{escape(str(e))}</pre>
            <p><a href="/fast-scan">Fast Scan</a></p>
            <p><a href="/deep-review">Deep AI Review</a></p>
            <p><a href="/positions">Positions</a></p>
            """,
            status_code=500,
        )


# ============================================================


def fallback_position_action(position):
    """
    Safe fallback when no AI position review exists yet.
    Uses simple unrealized P/L rules.
    """
    pl_pct = position_pl_pct(position)

    if pl_pct <= -8:
        return (
            "SELL / CUT LOSS",
            "blocked",
            f"Down {pl_pct:.1f}%. Review the thesis and consider cutting if news or momentum is weak.",
        )

    if pl_pct >= 15:
        return (
            "TRIM / TAKE PROFIT",
            "recommended",
            f"Up {pl_pct:.1f}%. Consider trimming unless the thesis is still clearly improving.",
        )

    if pl_pct >= 0:
        return (
            "HOLD",
            "approved",
            f"Up {pl_pct:.1f}%. Holding is reasonable unless news or technicals weaken.",
        )

    return (
        "WATCH CLOSELY",
        "vetoed",
        f"Down {pl_pct:.1f}%. Not an automatic sell, but monitor it closely.",
    )

# RESTORED DASHBOARD UI HELPERS
# Added as late overrides so they replace emergency fallbacks.
# ============================================================

def build_pie_chart(items, title):
    clean = []
    total = 0.0

    for label, value in items or []:
        value = safe_float(value)
        if value > 0:
            clean.append((str(label), value))
            total += value

    if not clean or total <= 0:
        return f"""
            <div class="allocation-card">
                <h3>{escape(str(title))}</h3>
                <p class="empty">No allocation data available.</p>
            </div>
        """

    colors = [
        "#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa",
        "#22d3ee", "#fb7185", "#c084fc", "#4ade80", "#f97316"
    ]

    current = 0.0
    stops = []
    legend_rows = ""

    for idx, pair in enumerate(clean[:10]):
        label, value = pair
        pct_value = (value / total) * 100
        start = current
        end = current + pct_value
        color = colors[idx % len(colors)]
        stops.append(f"{color} {start:.2f}% {end:.2f}%")
        current = end

        legend_rows += f"""
            <div class="legend-row">
                <span class="legend-left">
                    <span class="legend-dot" style="background:{color};"></span>
                    {escape(label)}
                </span>
                <span>{pct_value:.1f}%</span>
            </div>
        """

    gradient = ", ".join(stops)

    return f"""
        <div class="allocation-card">
            <h3>{escape(str(title))}</h3>
            <div class="pie-wrap">
                <div class="pie" style="background: conic-gradient({gradient});"></div>
                <div class="pie-center">
                    <strong>{len(clean)}</strong>
                    <span>items</span>
                </div>
            </div>
            <div class="legend">
                {legend_rows}
            </div>
        </div>
    """


def build_portfolio_pies(positions):
    if not positions:
        empty = """
            <div class="allocation-card">
                <h3>Allocation</h3>
                <p class="empty">No open positions yet.</p>
            </div>
        """
        return empty, empty

    try:
        cache = get_company_profile_cache()
    except Exception:
        cache = {}

    by_company = []
    by_sector = {}

    for position in positions:
        ticker = get_position_symbol(position)
        market_value = safe_float(position.get("market_value"))

        if not ticker or market_value <= 0:
            continue

        try:
            sector = get_position_sector(ticker, cache)
        except Exception:
            sector = "Other / Unclassified"

        if not sector or str(sector).lower() == "unknown":
            sector = "Other / Unclassified"

        by_company.append((ticker, market_value))
        by_sector[sector] = by_sector.get(sector, 0) + market_value

    by_company = sorted(by_company, key=lambda x: x[1], reverse=True)
    by_sector = sorted(by_sector.items(), key=lambda x: x[1], reverse=True)

    return (
        build_pie_chart(by_company, "Company Allocation"),
        build_pie_chart(by_sector, "Sector Allocation"),
    )


def build_position_intelligence_table(positions):
    if not positions:
        return "<p class='empty'>No open positions yet.</p>"

    try:
        ai_map, ai_data = get_ai_decision_map()
    except Exception:
        ai_map, ai_data = {}, {}

    rows = ""

    for position in sorted(positions, key=lambda p: safe_float(p.get("market_value")), reverse=True):
        ticker = get_position_symbol(position)
        market_value = safe_float(position.get("market_value"))
        unrealized_pl = safe_float(position.get("unrealized_pl"))
        pl_pct = position_pl_pct(position)

        ai_decision = ai_map.get(ticker)

        if ai_decision:
            decision = str(ai_decision.get("decision", "HOLD")).replace("_", " ")
            reasoning = str(ai_decision.get("reasoning", "No AI reasoning found."))
            sell_pct = safe_float(ai_decision.get("sell_pct"))
            confidence = safe_float(ai_decision.get("confidence"))
            thesis_status = str(ai_decision.get("thesis_status", "unknown")).replace("_", " ")
            css = status_class(decision)

            action_html = f"""
                {badge(decision, css)}
                <div class="muted small">Confidence: {confidence:.0f}/100 · Suggested sell: {sell_pct:.0f}%</div>
                <div class="muted small">Thesis: {escape(thesis_status)}</div>
            """

            sell_button = ""
            if sell_pct > 0:
                sell_button = f"""
                    <form method="post" action="/positions/ai-sell" style="margin-top:8px;">
                        <input type="hidden" name="ticker" value="{escape(ticker)}">
                        <input type="hidden" name="sell_pct" value="{sell_pct}">
                        <button type="submit" class="btn btn-red" style="padding:8px 10px; font-size:12px;">
                            Submit Suggested Sell {sell_pct:.0f}%
                        </button>
                    </form>
                """
            else:
                sell_button = "<span class='muted small'>No sell action suggested.</span>"

        else:
            decision, css, reasoning = fallback_position_action(position)
            action_html = badge(decision, css)
            sell_button = "<span class='muted small'>Run AI review for sell/hold decision.</span>"

        pl_class = "approved" if unrealized_pl >= 0 else "blocked"

        rows += f"""
            <tr>
                <td><b>{escape(ticker)}</b></td>
                <td>{money(market_value)}</td>
                <td>{badge(f"{money(unrealized_pl)} / {pl_pct:+.1f}%", pl_class)}</td>
                <td>{action_html}</td>
                <td>{escape(reasoning)}</td>
                <td>{sell_button}</td>
            </tr>
        """

    generated_at = ai_data.get("generated_at")
    if generated_at:
        ai_note = f"<p class='muted small'>AI position review last generated: {escape(str(generated_at))}</p>"
    else:
        ai_note = "<p class='muted small'>No cached AI position review yet. Run AI review to get sell/hold recommendations.</p>"

    return f"""
        {ai_note}

        <form method="post" action="/positions/ai-review" style="margin-bottom: 16px;">
            <button type="submit">Refresh AI Hold / Sell Review</button>
            <a href="/positions/ai" class="btn btn-dark">Open Full AI Position Review</a>
        </form>

        <table>
            <tr>
                <th>Ticker</th>
                <th>Market Value</th>
                <th>Unrealized P/L</th>
                <th>AI Suggested Action</th>
                <th>AI Reasoning</th>
                <th>Trade Action</th>
            </tr>
            {rows}
        </table>
    """


def build_portfolio_line_chart(history, selected_range="1M"):
    if not history:
        return "<p class='empty'>No portfolio snapshots yet. Reload the dashboard over time to build the graph.</p>"

    points = []

    for item in history:
        points.append({
            "timestamp": item.get("timestamp", ""),
            "value": safe_float(item.get("portfolio_value")),
        })

    if len(points) == 1:
        points = points * 2

    values = [p["value"] for p in points]
    min_v = min(values)
    max_v = max(values)
    spread = max(max_v - min_v, 1)

    width = 760
    height = 260
    pad_x = 38
    pad_y = 28

    coords = []

    for i, point in enumerate(points):
        x = pad_x + (i / max(len(points) - 1, 1)) * (width - pad_x * 2)
        y = height - pad_y - ((point["value"] - min_v) / spread) * (height - pad_y * 2)
        coords.append((x, y, point))

    polyline = " ".join([f"{x:.1f},{y:.1f}" for x, y, point in coords])
    area_points = f"{pad_x},{height-pad_y} " + polyline + f" {width-pad_x},{height-pad_y}"

    hover_zones = ""
    point_markers = ""

    for i, (x, y, point) in enumerate(coords):
        ts = str(point["timestamp"])
        value = point["value"]

        try:
            display_time = format_chart_timestamp(ts)
        except Exception:
            display_time = ts

        tooltip = escape(f"{display_time} | {money(value)}")

        if i == 0:
            left_x = pad_x
        else:
            left_x = (coords[i - 1][0] + x) / 2

        if i == len(coords) - 1:
            right_x = width - pad_x
        else:
            right_x = (x + coords[i + 1][0]) / 2

        zone_width = max(right_x - left_x, 12)

        # Wide invisible vertical hover zone. This makes hover MUCH easier than tracing the exact line.
        hover_zones += f"""
            <rect
                class="chart-hover-zone"
                x="{left_x:.1f}"
                y="0"
                width="{zone_width:.1f}"
                height="{height}"
                fill="transparent"
                data-tooltip="{tooltip}"
            ></rect>
        """

        point_markers += f"""
            <circle
                class="portfolio-point"
                cx="{x:.1f}"
                cy="{y:.1f}"
                r="4"
                fill="#93c5fd"
                data-tooltip="{tooltip}"
            ></circle>
        """

    start_value = points[0]["value"]
    end_value = points[-1]["value"]
    change = end_value - start_value
    change_pct = (change / start_value * 100) if start_value else 0
    change_class = "approved" if change >= 0 else "blocked"

    selected_range = escape(str(selected_range or "1M").upper())

    return f"""
        <div class="chart-card portfolio-chart-shell" style="position:relative;">
            <div class="chart-header" style="display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap; align-items:flex-start;">
                <div>
                    <div class="metric-title">Portfolio Value Over Time · {selected_range}</div>
                    <div class="big-number">{money(end_value)}</div>
                    <div class="muted small">From {money(start_value)} to {money(end_value)}</div>
                </div>
                <div>{badge(f"{change:+,.2f} / {change_pct:+.1f}%", change_class)}</div>
            </div>

            <div id="portfolioChartTooltip"
                 style="display:none; position:absolute; z-index:50; pointer-events:none;
                        background:rgba(2,6,23,0.96); border:1px solid rgba(147,197,253,0.45);
                        color:#f8fafc; padding:8px 10px; border-radius:10px;
                        font-size:13px; box-shadow:0 12px 30px rgba(0,0,0,0.35);">
            </div>

            <svg class="line-svg" viewBox="0 0 {width} {height}" preserveAspectRatio="none" style="width:100%; height:260px;">
                <defs>
                    <linearGradient id="portfolioFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stop-color="#3b82f6" stop-opacity="0.35"/>
                        <stop offset="100%" stop-color="#3b82f6" stop-opacity="0.02"/>
                    </linearGradient>
                </defs>

                <line x1="{pad_x}" y1="{height-pad_y}" x2="{width-pad_x}" y2="{height-pad_y}" stroke="rgba(148,163,184,0.35)" />
                <line x1="{pad_x}" y1="{pad_y}" x2="{pad_x}" y2="{height-pad_y}" stroke="rgba(148,163,184,0.20)" />

                <polygon points="{area_points}" fill="url(#portfolioFill)" />
                <polyline points="{polyline}" fill="none" stroke="#60a5fa" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" />

                {point_markers}
                {hover_zones}
            </svg>

            <p class="muted small">
                Hover anywhere vertically above a point to see timestamp and portfolio value.
            </p>

            <script>
                (function() {{
                    const shell = document.currentScript.closest(".portfolio-chart-shell");
                    if (!shell) return;

                    const tip = shell.querySelector("#portfolioChartTooltip");
                    const targets = shell.querySelectorAll(".chart-hover-zone, .portfolio-point");

                    targets.forEach(function(el) {{
                        el.addEventListener("mousemove", function(e) {{
                            const text = el.getAttribute("data-tooltip");
                            if (!text) return;

                            const rect = shell.getBoundingClientRect();
                            tip.textContent = text;
                            tip.style.display = "block";
                            tip.style.left = Math.min(e.clientX - rect.left + 14, rect.width - 260) + "px";
                            tip.style.top = Math.max(e.clientY - rect.top - 36, 8) + "px";
                        }});

                        el.addEventListener("mouseleave", function() {{
                            tip.style.display = "none";
                        }});
                    }});
                }})();
            </script>
        </div>
    """


@app.post("/positions/ai-sell")
def submit_ai_position_sell(ticker: str = Form(...), sell_pct: float = Form(...)):
    ticker = ticker.strip().upper()

    try:
        execute_ai_sell(ticker=ticker, sell_pct=sell_pct)
    except Exception as e:
        return HTMLResponse(
            f"""
            <h2>AI sell failed for {escape(ticker)}</h2>
            <pre>{escape(str(e))}</pre>
            <p><a href="/">Back to dashboard</a></p>
            <p><a href="/positions/ai">Open full AI position review</a></p>
            """,
            status_code=500,
        )

    return RedirectResponse(url="/", status_code=303)


# ============================================================
# FORCE RESTORE PIE CHARTS
# Self-contained allocation cards with inline styles.
# ============================================================

def build_pie_chart(items, title):
    clean = []
    total = 0.0

    for label, value in items or []:
        value = safe_float(value)
        if value > 0:
            clean.append((str(label), value))
            total += value

    if not clean or total <= 0:
        return f"""
            <div style="
                background: rgba(17,24,39,0.86);
                border: 1px solid rgba(148,163,184,0.18);
                border-radius: 20px;
                padding: 24px;
                margin-bottom: 22px;
                color: #f8fafc;
            ">
                <h2>{escape(str(title))}</h2>
                <p class="empty">No allocation data available.</p>
            </div>
        """

    colors = [
        "#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa",
        "#22d3ee", "#fb7185", "#c084fc", "#4ade80", "#f97316"
    ]

    current = 0.0
    stops = ""
    legend_rows = ""

    for idx, pair in enumerate(clean[:10]):
        label, value = pair
        pct_value = (value / total) * 100
        start = current
        end = current + pct_value
        color = colors[idx % len(colors)]
        current = end

        if stops:
            stops += ", "
        stops += f"{color} {start:.2f}% {end:.2f}%"

        legend_rows += f"""
            <div style="
                display:flex;
                justify-content:space-between;
                gap:16px;
                align-items:center;
                padding:8px 0;
                color:#f8fafc;
                font-size:15px;
            ">
                <span style="display:flex; align-items:center; gap:10px;">
                    <span style="
                        width:12px;
                        height:12px;
                        border-radius:999px;
                        background:{color};
                        display:inline-block;
                    "></span>
                    <b>{escape(str(label))}</b>
                </span>
                <span>{pct_value:.1f}%</span>
            </div>
        """

    return f"""
        <div style="
            background: rgba(17,24,39,0.86);
            border: 1px solid rgba(148,163,184,0.18);
            border-radius: 20px;
            padding: 24px;
            margin-bottom: 22px;
            color: #f8fafc;
            box-shadow: 0 24px 70px rgba(0,0,0,0.35);
        ">
            <h2 style="margin-top:0;">{escape(str(title))}</h2>

            <div style="
                display:flex;
                align-items:center;
                justify-content:center;
                margin:18px 0 24px 0;
            ">
                <div style="
                    width:260px;
                    height:260px;
                    border-radius:50%;
                    background: conic-gradient({stops});
                    position:relative;
                    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.04);
                ">
                    <div style="
                        position:absolute;
                        inset:70px;
                        background:#020617;
                        border-radius:50%;
                        display:flex;
                        flex-direction:column;
                        align-items:center;
                        justify-content:center;
                        color:#f8fafc;
                    ">
                        <strong style="font-size:38px; line-height:1;">{len(clean)}</strong>
                        <span style="color:#94a3b8; font-size:14px;">items</span>
                    </div>
                </div>
            </div>

            <div>
                {legend_rows}
            </div>
        </div>
    """


def build_portfolio_pies(positions):
    if not positions:
        empty = """
            <div style="
                background: rgba(17,24,39,0.86);
                border: 1px solid rgba(148,163,184,0.18);
                border-radius: 20px;
                padding: 24px;
                margin-bottom: 22px;
                color: #f8fafc;
            ">
                <h2>Allocation</h2>
                <p class="empty">No open positions yet.</p>
            </div>
        """
        return empty, empty

    try:
        cache = get_company_profile_cache()
    except Exception:
        cache = {}

    by_company = []
    by_sector = {}

    for position in positions:
        ticker = get_position_symbol(position)
        market_value = safe_float(position.get("market_value"))

        if not ticker or market_value <= 0:
            continue

        try:
            sector = get_position_sector(ticker, cache)
        except Exception:
            sector = "Other / Unclassified"

        if not sector or str(sector).strip().lower() == "unknown":
            sector = "Other / Unclassified"

        by_company.append((ticker, market_value))
        by_sector[sector] = by_sector.get(sector, 0) + market_value

    by_company = sorted(by_company, key=lambda x: x[1], reverse=True)
    by_sector = sorted(by_sector.items(), key=lambda x: x[1], reverse=True)

    return (
        build_pie_chart(by_company, "Company Allocation"),
        build_pie_chart(by_sector, "Sector Allocation"),
    )


