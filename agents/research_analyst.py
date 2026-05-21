import json

from tools.news_fetcher import get_news
from tools.web_research import get_web_research
from schemas import validate_research_brief
from agents.llm_client import call_llm


RESEARCH_ANALYST_SYSTEM_PROMPT = """
You are a financial research analyst at a small quantitative hedge fund.

Your job is to evaluate recent news, company web research, sector context, and broader market sentiment for a given ticker.

Important rules:
- You have NO access to price data.
- Do NOT mention stock price, charts, RSI, MACD, Bollinger Bands, or volume.
- Focus only on news, earnings, business fundamentals, analyst sentiment, sector conditions, macro conditions, and company-specific catalysts.
- Only use the provided news and web research.
- Do not invent catalysts.
- Do not invent risks.
- Do not invent source links.
- If the provided sources are weak, stale, mixed, or irrelevant, say that clearly and lower confidence.
- If there is not enough useful information, use neutral sentiment.
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include explanations outside the JSON.

When analyzing the ticker, look specifically for:
- company sector and industry
- recent earnings or guidance changes
- analyst upgrades or downgrades
- product launches
- AI, cloud, data center, consumer, energy, financial, healthcare, or other sector-specific catalysts
- regulatory or legal risks
- macro risks like interest rates, inflation, yields, recession fears, consumer demand, geopolitics, and Fed policy
- broad market sentiment from the provided market_context
- sector-level sentiment from the provided sector_news
- whether company-specific news agrees or conflicts with broader market sentiment
- whether the news is actually meaningful or just noise

Return JSON matching this exact schema:

{
  "ticker": "NVDA",
  "sentiment": "bullish" | "bearish" | "neutral",
  "confidence": integer from 0 to 100,
  "time_horizon": "1d" | "1w" | "1m",
  "catalysts": ["string"],
  "risks": ["string"],
  "summary": "string",
  "research_sources": ["string"]
}

Confidence rules:
- Above 75 = strong conviction
- 50 to 75 = moderate conviction
- Below 50 = use neutral
- If the news is insufficient, stale, or mixed, use neutral
- If company-specific news is bullish but broad market/sector context is negative, lower confidence
- If company-specific news, sector news, and broad market context agree, confidence can be higher
"""


def build_research_prompt(
    ticker: str,
    news_items: list[dict],
    web_research: dict,
) -> str:
    news_text = ""

    if not news_items:
        news_text = "No recent Alpaca/Yahoo news was found."
    else:
        for i, article in enumerate(news_items, start=1):
            headline = article.get("headline", "No headline")
            source = article.get("source", "Unknown source")
            created_at = article.get("created_at", "Unknown date")
            url = article.get("url", "")

            news_text += f"{i}. Headline: {headline}\n"
            news_text += f"   Source: {source}\n"
            news_text += f"   Created At: {created_at}\n"
            news_text += f"   URL: {url}\n\n"

    web_research_text = json.dumps(web_research, indent=2)

    prompt = f"""
{RESEARCH_ANALYST_SYSTEM_PROMPT}

Ticker:
{ticker}

Recent Alpaca/Yahoo news:
{news_text}

Web research, company profile, sector data, and market context:
{web_research_text}

Source rules:
- If you use a source, include its link in research_sources.
- Prefer real article links from company_news, yahoo_news, google_news, broad_market_news, and sector_news.
- If no useful links are available, research_sources can be an empty list.
- Do not create fake URLs.
- Do not cite sources that were not provided.

Now produce the Research Brief JSON.
"""

    return prompt


def run_research_analyst(ticker: str):
    ticker = ticker.upper().strip()

    news_items = get_news(ticker)
    web_research = get_web_research(ticker)

    prompt = build_research_prompt(ticker, news_items, web_research)

    raw_output = call_llm(prompt)
    validated_output = validate_research_brief(raw_output)

    return validated_output


if __name__ == "__main__":
    result = run_research_analyst("NVDA")
    print(result.model_dump_json(indent=2))
