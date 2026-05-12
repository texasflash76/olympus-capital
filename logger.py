import sqlite3
import json
from datetime import datetime


DB_PATH = "olympus_audit_log.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trade_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            ticker TEXT NOT NULL,
            research_brief TEXT,
            quant_signal TEXT,
            pm_decision TEXT,
            risk_result TEXT,
            final_status TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def log_trade_cycle(
    ticker,
    research_brief,
    quant_signal,
    pm_decision,
    risk_result,
    final_status
):
    init_db()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO trade_logs (
            timestamp,
            ticker,
            research_brief,
            quant_signal,
            pm_decision,
            risk_result,
            final_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        ticker,
        json.dumps(research_brief, indent=2),
        json.dumps(quant_signal, indent=2),
        json.dumps(pm_decision, indent=2),
        json.dumps(risk_result, indent=2),
        final_status
    ))

    conn.commit()
    conn.close()


def print_recent_logs(limit=5):
    init_db()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, timestamp, ticker, final_status, pm_decision, risk_result
        FROM trade_logs
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No logs found.")
        return

    for row in rows:
        log_id, timestamp, ticker, final_status, pm_decision, risk_result = row

        print("\n==============================")
        print(f"LOG ID: {log_id}")
        print(f"TIME: {timestamp}")
        print(f"TICKER: {ticker}")
        print(f"FINAL STATUS: {final_status}")
        print("\nPM DECISION:")
        print(pm_decision)
        print("\nRISK RESULT:")
        print(risk_result)
