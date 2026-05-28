import json
from pathlib import Path
from datetime import datetime, timezone


TRADE_THESIS_PATH = Path("trade_thesis_log.json")


ACTIVE_BUY_STATUSES = {
    "RECOMMENDED_NOT_EXECUTED",
    "PAPER_TRADE_SUBMITTED",
    "TRADE_APPROVED_BUT_EXECUTION_BLOCKED",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def safe_get(data, key, default=None):
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def build_trade_thesis_from_result(result: dict, source: str = "unknown"):
    ticker = str(result.get("ticker", "")).upper().strip()

    if not ticker:
        return None

    research = result.get("research_brief", {}) or {}
    quant = result.get("quant_signal", {}) or {}
    pm = result.get("pm_decision", {}) or {}
    risk = result.get("risk_result", {}) or {}
    profile = result.get("company_profile", {}) or {}
    web_research = result.get("web_research", {}) or {}

    final_status = result.get("final_status", "UNKNOWN")

    thesis = {
        "ticker": ticker,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "source": source,
        "final_status": final_status,
        "is_active_buy_thesis": final_status in ACTIVE_BUY_STATUSES,

        "company": {
            "name": (
                profile.get("company_name")
                or web_research.get("company_profile", {}).get("company_name")
                or ticker
            ),
            "sector": (
                profile.get("sector")
                or web_research.get("sector")
                or pm.get("sector")
                or "Unknown"
            ),
            "industry": (
                profile.get("industry")
                or web_research.get("industry")
                or "Unknown"
            ),
        },

        "buy_thesis": {
            "research_sentiment": research.get("sentiment"),
            "research_confidence": research.get("confidence"),
            "research_reasoning": research.get("reasoning"),

            "quant_direction": quant.get("direction"),
            "quant_strength": quant.get("strength"),
            "quant_entry_price": quant.get("entry_price"),
            "quant_stop_loss": quant.get("stop_loss"),
            "quant_take_profit": quant.get("take_profit"),
            "quant_reasoning": quant.get("reasoning"),

            "pm_decision": pm.get("decision"),
            "pm_direction": pm.get("direction"),
            "pm_size_pct": pm.get("size_pct"),
            "pm_reasoning": pm.get("reasoning"),
            "pm_risk_flags": pm.get("risk_flags", []),

            "risk_approved": risk.get("approved"),
            "risk_reasons": risk.get("reasons", []),
        },

        "raw_snapshot": {
            "research_brief": research,
            "quant_signal": quant,
            "pm_decision": pm,
            "risk_result": risk,
            "technical_summary": result.get("technical_summary", {}),
            "final_status": final_status,
        },
    }

    return thesis


def load_trade_theses():
    return read_json(TRADE_THESIS_PATH, {
        "updated_at": None,
        "theses": {},
        "history": [],
    })


def save_trade_thesis(thesis: dict):
    if not thesis:
        return None

    ticker = thesis["ticker"]
    store = load_trade_theses()

    previous = store.get("theses", {}).get(ticker)

    if previous:
        thesis["created_at"] = previous.get("created_at", thesis["created_at"])

    store.setdefault("theses", {})
    store.setdefault("history", [])

    store["theses"][ticker] = thesis
    store["history"].insert(0, thesis)
    store["history"] = store["history"][:250]
    store["updated_at"] = now_iso()

    write_json(TRADE_THESIS_PATH, store)
    return thesis


def record_trade_thesis_from_result(result: dict, source: str = "unknown"):
    thesis = build_trade_thesis_from_result(result, source=source)

    if not thesis:
        return None

    return save_trade_thesis(thesis)


def get_trade_thesis(ticker: str):
    ticker = str(ticker).upper().strip()
    store = load_trade_theses()
    return store.get("theses", {}).get(ticker)


def get_all_trade_theses():
    return load_trade_theses()


if __name__ == "__main__":
    store = load_trade_theses()
    print(json.dumps({
        "updated_at": store.get("updated_at"),
        "active_tickers": list(store.get("theses", {}).keys()),
        "history_count": len(store.get("history", [])),
    }, indent=2))
