import json
import sqlite3
from datetime import datetime
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from logger import DB_PATH, init_db
from tools.broker import get_account_summary, get_positions


app = FastAPI(title="Olympus Capital Dashboard")


def safe_json_loads(value):
    try:
        if value is None:
            return {}
        return json.loads(value)
    except Exception:
        return {"raw": value}


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
        float(account.get("buying_power", 0))
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
            final_status
        ) = row

        logs.append({
            "id": log_id,
            "timestamp": timestamp,
            "ticker": ticker,
            "research_brief": safe_json_loads(research_brief),
            "quant_signal": safe_json_loads(quant_signal),
            "pm_decision": safe_json_loads(pm_decision),
            "risk_result": safe_json_loads(risk_result),
            "final_status": final_status
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
            "portfolio_value": row[1]
        }
        for row in rows
    ]


def summarize_logs(logs):
    approved = 0
    vetoed = 0
    blocked = 0
    errors = 0

    for log in logs:
        status = str(log.get("final_status", ""))

        if "APPROVED" in status:
            approved += 1
        elif "VETOED" in status:
            vetoed += 1
        elif "BLOCKED" in status:
            blocked += 1
        elif "ERROR" in status:
            errors += 1

    return {
        "approved": approved,
        "vetoed": vetoed,
        "blocked": blocked,
        "errors": errors,
        "total": len(logs)
    }


def status_class(status):
    status = str(status)

    if "APPROVED" in status:
        return "approved"
    if "VETOED" in status:
        return "vetoed"
    if "BLOCKED" in status:
        return "blocked"
    return "error"


def money(value):
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "$0.00"


@app.get("/", response_class=HTMLResponse)
def dashboard_home():
    account = get_account_summary()
    positions = get_positions()

    save_portfolio_snapshot(account)

    recent_logs = get_recent_trade_logs(limit=20)
    today_logs = get_today_trade_logs()
    portfolio_history = get_portfolio_history(limit=30)
    summary = summarize_logs(today_logs)

    position_rows = ""

    if len(positions) == 0:
        position_rows = """
            <div class="card empty">No open positions.</div>
        """
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
        log_rows = """
            <div class="card empty">
                No trade logs yet. Run Phase 5 first with: python orchestrator.py
            </div>
        """
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

            rows += f"""
                <tr>
                    <td>{log.get("id", "")}</td>
                    <td>{escape(str(log.get("timestamp", "")))}</td>
                    <td>{escape(str(log.get("ticker", "")))}</td>
                    <td><span class="status {css_class}">{escape(str(status))}</span></td>
                    <td>{escape(str(pm_reasoning))}</td>
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
                    <th>Risk Result</th>
                </tr>
                {rows}
            </table>
        """

    chart_bars = ""

    if len(portfolio_history) == 0:
        chart_bars = "<p class='empty'>No portfolio snapshots yet.</p>"
    else:
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

            bars += f"""
                <div
                    class="bar"
                    title="{escape(str(point["timestamp"]))}: {money(value)}"
                    style="height: {height}px;"
                ></div>
            """

        latest_value = portfolio_history[-1]["portfolio_value"]

        chart_bars = f"""
            <div class="chart-row">
                {bars}
            </div>
            <p>Latest snapshot: {money(latest_value)}</p>
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

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Olympus Capital Dashboard</title>

        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #f4f6f8;
                margin: 0;
                padding: 24px;
                color: #111827;
            }}

            h1 {{
                margin-bottom: 4px;
            }}

            .subtitle {{
                color: #6b7280;
                margin-bottom: 24px;
            }}

            .grid {{
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 16px;
                margin-bottom: 24px;
            }}

            .card {{
                background: white;
                border-radius: 12px;
                padding: 18px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.08);
            }}

            .card h2 {{
                font-size: 16px;
                margin-top: 0;
                margin-bottom: 10px;
                color: #374151;
            }}

            .big-number {{
                font-size: 28px;
                font-weight: bold;
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 12px;
                overflow: hidden;
                margin-bottom: 24px;
            }}

            th, td {{
                padding: 12px;
                border-bottom: 1px solid #e5e7eb;
                text-align: left;
                vertical-align: top;
            }}

            th {{
                background: #111827;
                color: white;
                font-size: 13px;
            }}

            .status {{
                font-weight: bold;
                padding: 4px 8px;
                border-radius: 8px;
                display: inline-block;
            }}

            .approved {{
                background: #dcfce7;
                color: #166534;
            }}

            .blocked {{
                background: #fee2e2;
                color: #991b1b;
            }}

            .vetoed {{
                background: #fef3c7;
                color: #92400e;
            }}

            .error {{
                background: #e5e7eb;
                color: #374151;
            }}

            pre {{
                white-space: pre-wrap;
                font-size: 12px;
                background: #f9fafb;
                padding: 10px;
                border-radius: 8px;
                max-height: 220px;
                overflow: auto;
            }}

            .section-title {{
                margin-top: 32px;
                margin-bottom: 12px;
            }}

            .chart {{
                height: 220px;
                background: white;
                border-radius: 12px;
                padding: 18px;
                box-shadow: 0 1px 4px rgba(0,0,0,0.08);
                margin-bottom: 24px;
            }}

            .chart-row {{
                display: flex;
                align-items: end;
                gap: 6px;
                height: 170px;
                border-left: 1px solid #d1d5db;
                border-bottom: 1px solid #d1d5db;
                padding-left: 8px;
            }}

            .bar {{
                flex: 1;
                background: #111827;
                min-height: 4px;
                border-radius: 6px 6px 0 0;
            }}

            .empty {{
                color: #6b7280;
                font-style: italic;
            }}

            @media (max-width: 900px) {{
                .grid {{
                    grid-template-columns: repeat(2, 1fr);
                }}
            }}

            @media (max-width: 600px) {{
                .grid {{
                    grid-template-columns: 1fr;
                }}
            }}
        </style>
    </head>

    <body>
        <h1>Olympus Capital Dashboard</h1>
        <div class="subtitle">
            Paper trading dashboard for portfolio state, agent decisions, risk checks, and audit logs.
        </div>

        <div class="grid">
            <div class="card">
                <h2>Portfolio Value</h2>
                <div class="big-number">{money(account.get("portfolio_value", 0))}</div>
            </div>

            <div class="card">
                <h2>Cash</h2>
                <div class="big-number">{money(account.get("cash", 0))}</div>
            </div>

            <div class="card">
                <h2>Buying Power</h2>
                <div class="big-number">{money(account.get("buying_power", 0))}</div>
            </div>

            <div class="card">
                <h2>Account Status</h2>
                <div class="big-number">{escape(str(account.get("status", "Unknown")))}</div>
            </div>
        </div>

        <h2 class="section-title">Today's Decision Summary</h2>

        <div class="grid">
            <div class="card">
                <h2>Total Decisions</h2>
                <div class="big-number">{summary["total"]}</div>
            </div>

            <div class="card">
                <h2>Approved</h2>
                <div class="big-number">{summary["approved"]}</div>
            </div>

            <div class="card">
                <h2>Vetoed</h2>
                <div class="big-number">{summary["vetoed"]}</div>
            </div>

            <div class="card">
                <h2>Blocked / Errors</h2>
                <div class="big-number">{summary["blocked"] + summary["errors"]}</div>
            </div>
        </div>

        <h2 class="section-title">Portfolio Performance Snapshot</h2>

        <div class="chart">
            {chart_bars}
        </div>

        <h2 class="section-title">Current Positions</h2>
        {position_rows}

        <h2 class="section-title">Recent Agent Decisions</h2>
        {log_rows}

        {latest_raw}
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
