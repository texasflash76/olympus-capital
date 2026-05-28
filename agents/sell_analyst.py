import json

from agents.llm_client import call_llm


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
1. Whether profit should be protected
2. Whether losses should be cut
3. Whether technical momentum is improving or weakening
4. Whether recent news changes the risk/reward
5. Whether the position still deserves capital
6. Whether the rule-based decision is too aggressive, too passive, or reasonable

Important:
- You are reviewing an existing holding.
- Do not recommend buying more.
- Do not invent data.
- Respect the provided position data, technicals, news, and rule-based sell decision.
- Return only valid JSON.
- No markdown.
- No code fences.

Return JSON matching this exact schema:

{
  "decision": "HOLD | WATCH_CLOSELY | TRIM | TAKE_PROFIT | SELL | CUT_LOSS",
  "confidence": 0,
  "sell_pct": 0,
  "reasoning": "plain English explanation",
  "rule_agreement": "agree | disagree | partially_agree",
  "thesis_status": "intact | weakened | broken | unknown",
  "profit_protection_notes": "plain English explanation",
  "risk_flags": ["string"],
  "what_would_change_my_mind": "plain English explanation"
}

Decision rules:
- HOLD means sell_pct must be 0.
- WATCH_CLOSELY means sell_pct must be 0.
- TRIM usually means sell_pct between 10 and 35.
- TAKE_PROFIT usually means sell_pct between 25 and 60.
- SELL means sell_pct should usually be 100.
- CUT_LOSS means sell_pct should usually be 100.
"""


def build_sell_prompt(position_review):
    ticker = position_review.get("ticker", "UNKNOWN")
    position = position_review.get("position", {})
    technicals = position_review.get("technicals", {})
    news_summary = position_review.get("news_summary", {})
    rule_decision = position_review.get("decision", {})
    original_trade_thesis = position_review.get("original_trade_thesis", {})

    payload = {
        "ticker": ticker,
        "position": position,
        "technicals": technicals,
        "news_summary": news_summary,
        "rule_based_decision": rule_decision,
        "original_trade_thesis": original_trade_thesis,
        "task": (
            "Review this open position and decide whether to HOLD, WATCH_CLOSELY, "
            "TRIM, TAKE_PROFIT, SELL, or CUT_LOSS. Compare the current evidence "
            "against the original trade thesis. If the original thesis is broken, "
            "be more willing to SELL or CUT_LOSS. If the thesis is intact and the "
            "position is profitable, decide whether to HOLD, TRIM, or TAKE_PROFIT."
        ),
    }

    prompt = f"""
{SELL_ANALYST_SYSTEM_PROMPT}

POSITION REVIEW DATA:
{json.dumps(payload, indent=2, default=str)}

Now produce the Sell Analyst JSON.
"""

    return prompt


def normalize_sell_analyst_output(data):
    if not isinstance(data, dict):
        data = {}

    allowed = {
        "HOLD",
        "WATCH_CLOSELY",
        "TRIM",
        "TAKE_PROFIT",
        "SELL",
        "CUT_LOSS",
    }

    decision = str(data.get("decision", "HOLD")).upper().strip()

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

    risk_flags = data.get("risk_flags", [])

    if not isinstance(risk_flags, list):
        risk_flags = [str(risk_flags)]

    return {
        "decision": decision,
        "confidence": confidence,
        "sell_pct": sell_pct,
        "reasoning": str(data.get("reasoning", "")),
        "rule_agreement": str(data.get("rule_agreement", "unknown")),
        "thesis_status": str(data.get("thesis_status", "unknown")),
        "profit_protection_notes": str(data.get("profit_protection_notes", "")),
        "risk_flags": risk_flags,
        "what_would_change_my_mind": str(data.get("what_would_change_my_mind", "")),
    }


def run_sell_analyst(position_review):
    prompt = build_sell_prompt(position_review)
    raw_output = call_llm(prompt)
    return normalize_sell_analyst_output(raw_output)
