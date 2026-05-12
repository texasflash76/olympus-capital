import os
from dotenv import load_dotenv

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest


load_dotenv()


def get_news(ticker: str, limit: int = 5):
    """
    Fetches recent Alpaca news headlines for a ticker.
    """

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise ValueError("Missing Alpaca API keys. Check your .env file.")

    client = NewsClient(api_key, secret_key)

    request = NewsRequest(
        symbols=ticker,
        limit=limit
    )

    news_response = client.get_news(request)

    # Convert Alpaca's NewsSet object into a normal Python dictionary
    news_dict = news_response.model_dump()

    articles = []

    # Find the list of article objects inside the response
    for value in news_dict.values():
        if isinstance(value, list):
            articles = value
            break

    results = []

    for article in articles:
        results.append({
            "headline": article.get("headline", "No headline"),
            "source": article.get("source", "Unknown source"),
            "created_at": article.get("created_at", "Unknown time"),
        })

    return results