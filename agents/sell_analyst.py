import json


SELL_ANALYST_SYSTEM_PROMPT = """
You are the Olympus Capital Sell Analyst.

Your job is to review an EXISTING open position, not a new buy idea.

You must decide whether the position should be:
- HOLD
- WATCH_CLOSELY
- TRIM
- TAKE_PROFIT
- SELL
- CUT_LOSS

Focus on:
1. Whether the original buy thesis is still valid
2. Whether profit should be protected
3. Whether losses should be cut
4. Whether technical momentum is improving or weakening
5. Whether recent news changes the risk/reward
6. Whether the position still deserves capital compared to better opportunities

Return only valid JSON.

Schema:
{
  "decision": "HOLD | WATCH_CLOSELY | TRIM | TAKE_PROFIT | SELL | CUT_LOSS",
  "confidence": 0,
  "sell_pct": 0,
  "reasoning": "...",
  "thesis_status": "intact | weakened | broken | unknown",
  "profit_protection_notes": "...",
  "risk_flags": [],
  "what_would_change_my_mind": "..."
}
"""


def build_sell_prompt(position_review):
    ticker = position_review.get("ticker", "UNKNOWN")
    position = position_review.get("position", {})
    technicals = position_review.get("technicals", {})
    news_summary = position_review.get("news_summary", {})
    rule_decision = position_review.get("decision", {})

    payload = {
        "ticker": ticker,
        "position": position,
        "technicals": technicals,
        "news_summary": news_summary,
        "rule_based_decision": rule_decision,
        "instruction": (
            "Review this open position. Decide whether to hold, watch closely, trim, "
            "take profit, sell, or cut loss. Respect the rule-based decision, but you may "
            "override it if the evidence supports a better decision."
        ),
    }

    return SELL_ANALYST_SYSTEM_PROMPT + "\n\nPOSITION REVIEW:\n" + json.dumps(payload, indent=2, default=str)


def parse_sell_analyst_response(raw_text):
    try:
        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1

        if start == -1 or end <= 0:
            raise ValueError("No JSON object found in response.")

        data = json.loads(raw_text[start:end])

    except Exception as e:
        data = {
            "decision": "HOLD",
            "confidence": 0,
            "sell_pct": 0,
            "reasoning": f"Sell Analyst JSON parse failed: {e}",
            "thesis_status": "unknown",
            "profit_protection_notes": "",
            "risk_flags": ["parse_error"],
            "what_would_change_my_mind": "",
        }

    decision = str(data.get("decision", "HOLD")).upper().strip()

    allowed = {
        "HOLD",
        "WATCH_CLOSELY",
        "TRIM",
        "TAKE_PROFIT",
        "SELL",
        "CUT_LOSS",
    }

    if decision not in allowed:
        decision = "HOLD"

    try:
        confidence = int(float(data.get("confidence", 0)))
    except Exception:
        confidence = 0

    confidence = max(0, min(100, confidence))

    try:
        sell_pct = float(data.get("sell_pct", 0))
    except Exception:
        sell_pct = 0

    sell_pct = max(0, min(100, sell_pct))

    if decision in ["HOLD", "WATCH_CLOSELY"]:
        sell_pct = 0

    if decision == "TRIM" and sell_pct <= 0:
        sell_pct = 25

    if decision == "TAKE_PROFIT" and sell_pct <= 0:
        sell_pct = 50

    if decision in ["SELL", "CUT_LOSS"] and sell_pct <= 0:
        sell_pct = 100

    data["decision"] = decision
    data["confidence"] = confidence
    data["sell_pct"] = sell_pct

    return data
