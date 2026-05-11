import os
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

load_dotenv()

api_key = os.getenv("ALPACA_API_KEY")
secret_key = os.getenv("ALPACA_SECRET_KEY")

if not api_key or not secret_key:
    raise ValueError("Missing Alpaca API keys. Check your .env file.")

client = StockHistoricalDataClient(api_key, secret_key)

request = StockLatestTradeRequest(symbol_or_symbols="AAPL")
latest_trade = client.get_stock_latest_trade(request)

aapl_trade = latest_trade["AAPL"]

print("AAPL latest trade:")
print(f"Price: ${aapl_trade.price}")
print(f"Time: {aapl_trade.timestamp}")
