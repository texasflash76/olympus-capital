import os
import feedparser
from dotenv import load_dotenv
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest


load_dotenv()


def get_alpaca_news(ticker: str, limit: int = 5):
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        return []

    try:
        client = NewsClient(api_key, secret_key)
        request = NewsRequest(symbols=ticker, limit=limit)
        news_response = client.get_news(request)

        news_dict = news_response.model_dump()

        articles = []
        for value in news_dict.values():
            if isinstance(value, list):
                articles = value
                break

        results = []
        for article in articles[:limit]:
            results.append({
                "headline": article.get("headline", ""),
                "source": article.get("source", "Alpaca"),
                "created_at": str(article.get("created_at", "")),
                "url": article.get("url", ""),
            })

        return results

    except Exception as e:
        print(f"Alpaca news error for {ticker}: {e}")
        return []


def get_yahoo_rss_news(ticker: str, limit: int = 10):
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"

    try:
        feed = feedparser.parse(url)

        results = []
        for entry in feed.entries[:limit]:
            results.append({
                "headline": entry.get("title", ""),
                "source": "Yahoo Finance RSS",
                "created_at": entry.get("published", ""),
                "url": entry.get("link", ""),
            })

        return results

    except Exception as e:
        print(f"Yahoo RSS news error for {ticker}: {e}")
        return []


def get_news(ticker: str, limit: int = 10):
    ticker = ticker.upper().strip()

    alpaca_news = get_alpaca_news(ticker, limit=limit)

    if len(alpaca_news) >= 3:
        return alpaca_news[:limit]

    yahoo_news = get_yahoo_rss_news(ticker, limit=limit)

    combined = alpaca_news + yahoo_news

    seen = set()
    unique_results = []

    for item in combined:
        headline = item.get("headline", "").strip()
        if not headline:
            continue

        key = headline.lower()
        if key in seen:
            continue

        seen.add(key)
        unique_results.append(item)

    return unique_results[:limit]


if __name__ == "__main__":
    ticker = "META"
    news = get_news(ticker)

    print(f"\nRecent news for {ticker}:")
    for article in news:
        print(f"- {article['headline']} ({article['source']})")
