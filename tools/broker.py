import os
from math import floor
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from tools.market_data import get_bars


def get_trading_client():
    """
    Creates an Alpaca TradingClient using credentials from .env.
    """
    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    if not api_key or not secret_key:
        raise ValueError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env")

    return TradingClient(api_key, secret_key, paper=paper)


def get_account_summary():
    """
    Reads Alpaca paper account state.
    """
    client = get_trading_client()
    account = client.get_account()

    return {
        "status": str(account.status),
        "portfolio_value": float(account.portfolio_value),
        "buying_power": float(account.buying_power),
        "cash": float(account.cash),
    }


def get_positions():
    """
    Reads current Alpaca paper positions.

    Includes extra lifecycle fields used by the position monitor:
    - average entry price
    - current price
    - cost basis
    - unrealized P/L %
    """
    client = get_trading_client()
    positions = client.get_all_positions()

    output = []

    for position in positions:
        output.append({
            "symbol": position.symbol,
            "qty": float(position.qty),
            "market_value": float(position.market_value),
            "unrealized_pl": float(position.unrealized_pl),
            "unrealized_plpc": float(getattr(position, "unrealized_plpc", 0) or 0),
            "avg_entry_price": float(getattr(position, "avg_entry_price", 0) or 0),
            "current_price": float(getattr(position, "current_price", 0) or 0),
            "cost_basis": float(getattr(position, "cost_basis", 0) or 0),
            "side": str(getattr(position, "side", "long")),
        })

    return output


def get_latest_close_price(ticker: str) -> float:
    """
    Gets a rough latest close price using existing Alpaca market data.
    Used only to calculate whole-share quantity for non-fractionable assets.
    """
    ticker = ticker.upper().strip()
    bars = get_bars(ticker, days=10)

    if bars is None or len(bars) == 0:
        raise ValueError(f"Could not fetch recent price for {ticker}.")

    clean_bars = bars.dropna()

    if len(clean_bars) == 0:
        raise ValueError(f"No clean recent price data found for {ticker}.")

    latest = clean_bars.iloc[-1]
    return float(latest["close"])


def submit_paper_market_order(ticker: str, direction: str, notional_value: float):
    """
    Submits a paper market order through Alpaca.

    Safety rules:
    - Requires ALPACA_PAPER=true
    - Requires PAPER_TRADING_ENABLED=true
    - Caps order size using MAX_TRADE_NOTIONAL
    - Only supports long/buy orders for now
    - Uses notional orders for fractionable assets
    - Uses whole-share qty orders for non-fractionable assets
    """

    load_dotenv()

    ticker = ticker.upper().strip()
    direction = direction.lower().strip()
    notional_value = float(notional_value)

    alpaca_paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    paper_trading_enabled = os.getenv("PAPER_TRADING_ENABLED", "false").lower() == "true"
    max_trade_notional = float(os.getenv("MAX_TRADE_NOTIONAL", "100"))

    if not alpaca_paper:
        raise ValueError("Blocked: ALPACA_PAPER must be true. Live trading is disabled.")

    if not paper_trading_enabled:
        raise ValueError("Blocked: PAPER_TRADING_ENABLED is false.")

    if notional_value <= 0:
        raise ValueError("Blocked: notional_value must be greater than 0.")

    if max_trade_notional <= 0:
        raise ValueError("Blocked: MAX_TRADE_NOTIONAL must be greater than 0.")

    if direction != "long":
        raise ValueError("Blocked: only long/buy paper orders are supported right now.")

    original_notional_value = notional_value

    if notional_value > max_trade_notional:
        print(
            f"Safety cap: proposed trade ${notional_value:.2f} exceeds "
            f"MAX_TRADE_NOTIONAL ${max_trade_notional:.2f}. "
            f"Submitting capped paper order for ${max_trade_notional:.2f}."
        )
        notional_value = max_trade_notional

    client = get_trading_client()

    asset = client.get_asset(ticker)

    if not bool(getattr(asset, "tradable", False)):
        raise ValueError(f"Blocked: {ticker} is not tradable on Alpaca.")

    fractionable = bool(getattr(asset, "fractionable", False))

    if fractionable:
        order_request = MarketOrderRequest(
            symbol=ticker,
            notional=round(notional_value, 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )

        order_type_used = "notional"

    else:
        latest_price = get_latest_close_price(ticker)
        qty = floor(notional_value / latest_price)

        if qty < 1:
            raise ValueError(
                f"Blocked: {ticker} is not fractionable and capped trade size "
                f"${notional_value:.2f} is less than one share at estimated price "
                f"${latest_price:.2f}. Increase MAX_TRADE_NOTIONAL or use a fractionable ticker."
            )

        order_request = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )

        order_type_used = "whole_share_qty"

    order = client.submit_order(order_request)

    if hasattr(order, "model_dump"):
        order_data = order.model_dump()
    else:
        order_data = dict(order)

    order_data["_olympus_execution_notes"] = {
        "ticker": ticker,
        "direction": direction,
        "asset_fractionable": fractionable,
        "order_type_used": order_type_used,
        "original_proposed_notional": original_notional_value,
        "submitted_notional_cap": notional_value,
        "max_trade_notional": max_trade_notional,
    }

    return order_data


def submit_paper_sell_order(ticker: str, qty: float):
    """
    Submits a paper sell order through Alpaca.

    Safety rules:
    - Requires ALPACA_PAPER=true
    - Requires SELL_TRADING_ENABLED=true
    - Only sells existing long positions
    - Never sells more than the current owned quantity
    """

    load_dotenv()

    ticker = ticker.upper().strip()
    qty = float(qty)

    alpaca_paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    sell_trading_enabled = os.getenv("SELL_TRADING_ENABLED", "false").lower() == "true"

    if not alpaca_paper:
        raise ValueError("Blocked: ALPACA_PAPER must be true. Live trading is disabled.")

    if not sell_trading_enabled:
        raise ValueError("Blocked: SELL_TRADING_ENABLED is false.")

    if qty <= 0:
        raise ValueError("Blocked: sell quantity must be greater than 0.")

    client = get_trading_client()

    positions = client.get_all_positions()
    matching_position = None

    for position in positions:
        if str(position.symbol).upper().strip() == ticker:
            matching_position = position
            break

    if matching_position is None:
        raise ValueError(f"Blocked: no open position found for {ticker}.")

    owned_qty = float(matching_position.qty)

    if owned_qty <= 0:
        raise ValueError(f"Blocked: owned quantity for {ticker} is not greater than 0.")

    if qty > owned_qty:
        raise ValueError(
            f"Blocked: attempted to sell {qty} shares of {ticker}, "
            f"but only {owned_qty} shares are owned."
        )

    asset = client.get_asset(ticker)

    if not bool(getattr(asset, "tradable", False)):
        raise ValueError(f"Blocked: {ticker} is not tradable on Alpaca.")

    order_request = MarketOrderRequest(
        symbol=ticker,
        qty=round(qty, 6),
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )

    order = client.submit_order(order_request)

    if hasattr(order, "model_dump"):
        order_data = order.model_dump()
    else:
        order_data = dict(order)

    order_data["_olympus_execution_notes"] = {
        "ticker": ticker,
        "side": "sell",
        "requested_qty": qty,
        "owned_qty_before_order": owned_qty,
        "paper_only": True,
    }

    return order_data
