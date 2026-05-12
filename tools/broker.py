import os
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient


load_dotenv()


def get_trading_client():
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    if not api_key or not secret_key:
        raise ValueError("Missing Alpaca API keys. Check your .env file.")

    return TradingClient(api_key, secret_key, paper=paper)


def get_account_summary():
    """
    Reads Alpaca paper account status.
    Read-only. Does not place trades.
    """

    client = get_trading_client()
    account = client.get_account()

    return {
        "status": account.status,
        "portfolio_value": float(account.portfolio_value),
        "buying_power": float(account.buying_power),
        "cash": float(account.cash),
    }


def get_positions():
    """
    Reads current Alpaca paper positions.
    Read-only. Does not place trades.
    """

    client = get_trading_client()
    positions = client.get_all_positions()

    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
        }
        for p in positions
    ]