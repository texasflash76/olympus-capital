import os
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta


load_dotenv()


def get_bars(ticker: str, days: int = 100):
    """
    Fetch daily OHLCV stock bars from Alpaca.
    OHLCV = open, high, low, close, volume.
    """

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise ValueError("Missing Alpaca API keys. Check your .env file.")

    client = StockHistoricalDataClient(api_key, secret_key)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start_date,
        end=end_date
    )

    bars = client.get_stock_bars(request)

    df = bars.df

    if df.empty:
        raise ValueError(f"No market data returned for {ticker}.")

    # If Alpaca returns a multi-index, select the ticker's rows.
    if "symbol" in df.index.names:
        df = df.loc[ticker]

    return df