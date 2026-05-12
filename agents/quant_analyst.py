from tools.market_data import get_bars
from tools.technicals import add_technicals, get_latest_technical_summary
from schemas import validate_quant_signal
from agents.llm_client import call_llm_manual


QUANT_ANALYST_SYSTEM_PROMPT = """
You are a quantitative analyst at a small hedge fund.

Your job is to evaluate technical indicators for a given ticker.

Important rules:
- You have NO access to news, earnings, analyst opinions, or business fundamentals.
- Do NOT mention news or narrative.
- Focus only on OHLCV-based technical indicators.
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include explanations outside the JSON.

Evaluate:
- RSI above 70 may indicate overbought conditions.
- RSI below 30 may indicate oversold conditions.
- MACD above MACD signal may support a long signal.
- MACD below MACD signal may support a short signal.
- Price near the lower Bollinger Band may support mean reversion long.
- Price near the upper Bollinger Band may indicate caution or short setup.
- Volume ratio above 1.2 confirms stronger participation.
- If signals conflict, use direction: "flat".

Return JSON matching this exact schema:

{
  "ticker": "NVDA",
  "direction": "long" | "short" | "flat",
  "strength": integer from 0 to 100,
  "entry_price": float,
  "stop_loss": float,
  "size_pct": integer from 1 to 15,
  "primary_signal": "string",
  "confirming_signals": ["string"]
}

Rules:
- Never recommend size_pct above 15.
- If direction is flat, use a small size_pct like 1.
- Strength above 75 = strong technical signal.
- Strength 50 to 75 = moderate technical signal.
- Strength below 50 = weak signal, usually flat.
"""


def build_quant_prompt(ticker: str, technical_summary: dict) -> str:
    prompt = f"""
{QUANT_ANALYST_SYSTEM_PROMPT}

Ticker: {ticker}

Latest technical summary:
{technical_summary}

Now produce the Quant Signal JSON.
"""

    return prompt


def run_quant_analyst(ticker: str):
    """
    Runs the Quant Analyst agent.

    Current Phase 3 version:
    - Fetches market data using Phase 2 market_data
    - Adds technical indicators using Phase 2 technicals
    - Builds a strict JSON prompt
    - Lets you manually call ChatGPT/Codex
    - Validates the response using schemas.py
    """

    bars = get_bars(ticker)
    bars_with_technicals = add_technicals(bars)
    technical_summary = get_latest_technical_summary(bars_with_technicals)

    prompt = build_quant_prompt(ticker, technical_summary)

    raw_output = call_llm_manual(prompt)

    validated_output = validate_quant_signal(raw_output)

    return validated_output
