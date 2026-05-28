import json
import os
from pathlib import Path
from datetime import datetime, timezone

from position_monitor import (
    review_all_positions,
    get_position_review,
    execute_position_sell,
)
from trade_thesis_store import get_trade_thesis
from agents.sell_analyst import (
    build_sell_prompt,
    run_sell_analyst,
)


AI_POSITION_REVIEW_PATH = Path("ai_position_review_results.json")
SELL_ANALYST_PROMPTS_PATH = Path("sell_analyst_prompts.txt")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def read_json(path: Path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def estimate_qty_to_sell(position_review, ai_decision):
    position = position_review.get("position", {})
    qty = safe_float(position.get("qty"))
    sell_pct = safe_float(ai_decision.get("sell_pct"))

    if qty <= 0 or sell_pct <= 0:
        return 0

    return qty * (sell_pct / 100)


def get_ai_position_review():
    return read_json(AI_POSITION_REVIEW_PATH, {
        "generated_at": None,
        "llm_mode": None,
        "summary": {},
        "reviews": [],
    })


def run_ai_position_review(force_refresh_positions=True):
    """
    Runs AI Sell Analyst review on current open positions.

    Important:
    - If LLM_MODE=manual, this will NOT freeze the dashboard waiting for input.
    - Instead, it writes sell_analyst_prompts.txt so you can copy prompts manually.
    - If LLM_MODE=codex, it calls the same call_llm() path used by your other agents.
    """
    llm_mode = os.getenv("LLM_MODE", "manual").lower().strip()

    if force_refresh_positions:
        position_data = review_all_positions()
    else:
        position_data = get_position_review()

        if not position_data.get("reviews"):
            position_data = review_all_positions()

    reviews = position_data.get("reviews", [])

    ai_reviews = []
    manual_prompts = []

    for item in reviews:
        ticker = item.get("ticker", "UNKNOWN")

        original_thesis = get_trade_thesis(ticker)

        if original_thesis:
            item["original_trade_thesis"] = original_thesis
        else:
            item["original_trade_thesis"] = {
                "ticker": ticker,
                "status": "missing",
                "message": "No saved original trade thesis found for this ticker yet."
            }

        if llm_mode == "manual":
            prompt = build_sell_prompt(item)
            manual_prompts.append(f"\n\n{'=' * 90}\nTICKER: {ticker}\n{'=' * 90}\n\n{prompt}")

            ai_decision = {
                "decision": "MANUAL_PROMPT_CREATED",
                "confidence": 0,
                "sell_pct": 0,
                "reasoning": "LLM_MODE is manual, so no AI call was made. Prompt was written to sell_analyst_prompts.txt.",
                "rule_agreement": "unknown",
                "thesis_status": "unknown",
                "profit_protection_notes": "",
                "risk_flags": ["manual_mode"],
                "what_would_change_my_mind": "",
            }

        else:
            try:
                ai_decision = run_sell_analyst(item)
            except Exception as e:
                ai_decision = {
                    "decision": "ERROR",
                    "confidence": 0,
                    "sell_pct": 0,
                    "reasoning": f"AI sell review failed: {e}",
                    "rule_agreement": "unknown",
                    "thesis_status": "unknown",
                    "profit_protection_notes": "",
                    "risk_flags": ["ai_error"],
                    "what_would_change_my_mind": "",
                }

        ai_decision["estimated_qty_to_sell"] = estimate_qty_to_sell(item, ai_decision)

        ai_reviews.append({
            "ticker": ticker,
            "reviewed_at": now_iso(),
            "position_review": item,
            "ai_decision": ai_decision,
        })

    if manual_prompts:
        SELL_ANALYST_PROMPTS_PATH.write_text("\n".join(manual_prompts))

    summary = {
        "total_positions": len(ai_reviews),
        "hold": len([r for r in ai_reviews if r.get("ai_decision", {}).get("decision") == "HOLD"]),
        "watch_closely": len([r for r in ai_reviews if r.get("ai_decision", {}).get("decision") == "WATCH_CLOSELY"]),
        "trim": len([r for r in ai_reviews if r.get("ai_decision", {}).get("decision") == "TRIM"]),
        "take_profit": len([r for r in ai_reviews if r.get("ai_decision", {}).get("decision") == "TAKE_PROFIT"]),
        "sell": len([r for r in ai_reviews if r.get("ai_decision", {}).get("decision") == "SELL"]),
        "cut_loss": len([r for r in ai_reviews if r.get("ai_decision", {}).get("decision") == "CUT_LOSS"]),
        "errors": len([r for r in ai_reviews if r.get("ai_decision", {}).get("decision") == "ERROR"]),
        "manual_prompts_created": len([r for r in ai_reviews if r.get("ai_decision", {}).get("decision") == "MANUAL_PROMPT_CREATED"]),
    }

    payload = {
        "generated_at": now_iso(),
        "llm_mode": llm_mode,
        "summary": summary,
        "reviews": ai_reviews,
        "manual_prompt_file": str(SELL_ANALYST_PROMPTS_PATH) if manual_prompts else None,
    }

    write_json(AI_POSITION_REVIEW_PATH, payload)
    return payload


def execute_ai_sell(ticker: str, sell_pct: float):
    return execute_position_sell(ticker=ticker, sell_pct=sell_pct)


if __name__ == "__main__":
    result = run_ai_position_review(force_refresh_positions=True)
    print(json.dumps(result["summary"], indent=2))

    if result.get("manual_prompt_file"):
        print(f"\nManual prompts written to: {result['manual_prompt_file']}")
