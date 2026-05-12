from tools.news_fetcher import get_news
from schemas import validate_research_brief
from agents.llm_client import call_llm_manual


RESEARCH_ANALYST_SYSTEM_PROMPT = """
You are a financial research analyst at a quantitative hedge fund.

Your job is to evaluate recent news for a given ticker.

Important rules:
- You have NO access to price data.
- Do NOT mention stock price, charts, RSI, MACD, Bollinger Bands, or volume.
- Focus only on news, earnings, business fundamentals, analyst sentiment, macro context, catalysts, and risks.
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include explanations outside the JSON.

Return JSON matching this exact schema:

{
  "ticker": "NVDA",
  "sentiment": "bullish" | "bearish" | "neutral",
  "confidence": integer from 0 to 100,
  "time_horizon": "1d" | "1w" | "1m",
  "catalysts": ["string"],
  "risks": ["string"],
  "summary": "string"
}

Confidence rules:
- Above 75 = strong conviction
- 50 to 75 = moderate conviction
- Below 50 = use neutral
- If the news is insufficient or mixed, use neutral
"""


def build_research_prompt(ticker: str, news_items: list[dict]) -> str:
    news_text = ""

    if not news_items:
        news_text = "No recent news was found."
    else:
        for i, article in enumerate(news_items, start=1):
            headline = article.get("headline", "No headline")
            source = article.get("source", "Unknown source")
            created_at = article.get("created_at", "Unknown date")

            news_text += f"{i}. Headline: {headline}\n"
            news_text += f"   Source: {source}\n"
            news_text += f"   Created At: {created_at}\n\n"

    prompt = f"""
{RESEARCH_ANALYST_SYSTEM_PROMPT}

Ticker: {ticker}

Recent news:
{news_text}

Now produce the Research Brief JSON.
"""

    return prompt


def run_research_analyst(ticker: str):
    """
    Runs the Research Analyst agent.

    Current Phase 3 version:
    - Fetches news using Phase 2 news_fetcher
    - Builds a strict JSON prompt
    - Lets you manually call ChatGPT/Codex
    - Validates the response using schemas.py
    """

    news_items = get_news(ticker)
    prompt = build_research_prompt(ticker, news_items)

    raw_output = call_llm_manual(prompt)

    validated_output = validate_research_brief(raw_output)

    return validated_output
