import json
from pathlib import Path
from datetime import datetime, timezone

from tools.broker import get_positions, submit_paper_sell_order
from tools.market_data import get_bars
from tools.technicals import add_technicals, get_latest_technical_summary
from tools.news_fetcher import get_news


POSITION_REVIEW_PATH = Path("position_review_results.json")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def read_json(path: Path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def get_recent_technicals(ticker: str):
    try:
        bars = get_bars(ticker, days=120)
        bars_with_technicals = add_technicals(bars)
        return get_latest_technical_summary(bars_with_technicals)
    except Exception as e:
        return {
            "error": str(e),
            "close": None,
            "rsi": None,
            "macd": None,
            "macd_signal": None,
            "bollinger_upper": None,
            "bollinger_lower": None,
            "volume_ratio": None,
        }


def summarize_news(ticker: str, limit=5):
    try:
        headlines = get_news(ticker, limit=limit)
    except Exception as e:
        return {
            "headlines": [],
            "sentiment_hint": "unknown",
            "error": str(e),
        }

    positive_terms = [
        "beats", "beat", "raises", "raised", "upgrade", "upgraded",
        "surge", "rally", "record", "strong", "growth", "profit",
        "approval", "partnership", "contract"
    ]

    negative_terms = [
        "misses", "miss", "cuts", "cut", "downgrade", "downgraded",
        "falls", "plunge", "slump", "weak", "loss", "lawsuit",
        "probe", "investigation", "warning", "recall"
    ]

    positive = 0
    negative = 0

    for item in headlines:
        text = str(item.get("headline", "")).lower()

        if any(term in text for term in positive_terms):
            positive += 1

        if any(term in text for term in negative_terms):
            negative += 1

    if positive > negative:
        sentiment_hint = "positive"
    elif negative > positive:
        sentiment_hint = "negative"
    else:
        sentiment_hint = "mixed"

    return {
        "headlines": headlines,
        "sentiment_hint": sentiment_hint,
        "positive_headlines": positive,
        "negative_headlines": negative,
    }


def make_sell_decision(position, technicals, news_summary):
    ticker = position.get("symbol", "UNKNOWN")

    qty = safe_float(position.get("qty"))
    market_value = safe_float(position.get("market_value"))
    unrealized_pl = safe_float(position.get("unrealized_pl"))
    unrealized_plpc = safe_float(position.get("unrealized_plpc")) * 100

    if unrealized_plpc == 0 and market_value > 0:
        cost_basis = safe_float(position.get("cost_basis"))
        if cost_basis > 0:
            unrealized_plpc = ((market_value - cost_basis) / cost_basis) * 100

    rsi = safe_float(technicals.get("rsi"), default=None)
    macd = safe_float(technicals.get("macd"), default=None)
    macd_signal = safe_float(technicals.get("macd_signal"), default=None)
    volume_ratio = safe_float(technicals.get("volume_ratio"), default=None)
    news_sentiment = news_summary.get("sentiment_hint", "mixed")

    bearish_macd = False
    if macd is not None and macd_signal is not None:
        bearish_macd = macd < macd_signal

    overbought = rsi is not None and rsi >= 72
    very_overbought = rsi is not None and rsi >= 78
    weak_volume = volume_ratio is not None and volume_ratio < 0.8

    reasons = []
    decision = "HOLD"
    sell_pct = 0
    confidence = 55

    # Hard downside protection
    if unrealized_plpc <= -10:
        decision = "CUT_LOSS"
        sell_pct = 100
        confidence = 88
        reasons.append(f"{ticker} is down {unrealized_plpc:.2f}%, which passes the hard stop-loss zone.")

    elif unrealized_plpc <= -7 and bearish_macd:
        decision = "SELL"
        sell_pct = 100
        confidence = 78
        reasons.append(f"{ticker} is down {unrealized_plpc:.2f}% and MACD is bearish.")

    elif unrealized_plpc <= -5 and bearish_macd and news_sentiment == "negative":
        decision = "SELL"
        sell_pct = 100
        confidence = 82
        reasons.append(f"{ticker} is down {unrealized_plpc:.2f}%, technicals are weakening, and news looks negative.")

    # Profit protection
    elif unrealized_plpc >= 25:
        decision = "TAKE_PROFIT"
        sell_pct = 50
        confidence = 86
        reasons.append(f"{ticker} is up {unrealized_plpc:.2f}%, so the system recommends locking in part of the gain.")

    elif unrealized_plpc >= 18 and overbought:
        decision = "TAKE_PROFIT"
        sell_pct = 50
        confidence = 84
        reasons.append(f"{ticker} is up {unrealized_plpc:.2f}% and RSI is overbought at {rsi:.2f}.")

    elif unrealized_plpc >= 12 and very_overbought:
        decision = "TRIM"
        sell_pct = 25
        confidence = 76
        reasons.append(f"{ticker} is up {unrealized_plpc:.2f}% and RSI is very elevated at {rsi:.2f}.")

    elif unrealized_plpc >= 10 and bearish_macd:
        decision = "TRIM"
        sell_pct = 25
        confidence = 72
        reasons.append(f"{ticker} has a solid gain of {unrealized_plpc:.2f}%, but MACD is turning bearish.")

    # Watchlist behavior
    elif unrealized_plpc >= 8 and news_sentiment == "negative":
        decision = "WATCH_CLOSELY"
        sell_pct = 0
        confidence = 65
        reasons.append(f"{ticker} is profitable, but recent news sentiment looks negative.")

    elif unrealized_plpc <= -4 and bearish_macd:
        decision = "WATCH_CLOSELY"
        sell_pct = 0
        confidence = 64
        reasons.append(f"{ticker} is slightly down and technicals are weakening.")

    else:
        decision = "HOLD"
        sell_pct = 0
        confidence = 58
        reasons.append(f"{ticker} does not currently hit a take-profit or stop-loss rule.")

    if weak_volume:
        reasons.append("Volume is below normal, so conviction is lower.")

    if news_sentiment == "positive":
        reasons.append("Recent headline sentiment looks positive.")
    elif news_sentiment == "negative":
        reasons.append("Recent headline sentiment looks negative.")
    else:
        reasons.append("Recent headline sentiment is mixed or unclear.")

    estimated_qty_to_sell = 0
    if sell_pct > 0 and qty > 0:
        estimated_qty_to_sell = qty * (sell_pct / 100)

    return {
        "ticker": ticker,
        "decision": decision,
        "confidence": confidence,
        "sell_pct": sell_pct,
        "estimated_qty_to_sell": estimated_qty_to_sell,
        "reasons": reasons,
        "inputs": {
            "qty": qty,
            "market_value": market_value,
            "unrealized_pl": unrealized_pl,
            "unrealized_plpc": unrealized_plpc,
            "rsi": rsi,
            "macd": macd,
            "macd_signal": macd_signal,
            "volume_ratio": volume_ratio,
            "news_sentiment": news_sentiment,
        },
    }


def review_position(position):
    ticker = position.get("symbol", "").upper().strip()

    technicals = get_recent_technicals(ticker)
    news_summary = summarize_news(ticker)
    decision = make_sell_decision(position, technicals, news_summary)

    return {
        "ticker": ticker,
        "reviewed_at": now_iso(),
        "position": position,
        "technicals": technicals,
        "news_summary": news_summary,
        "decision": decision,
    }


def review_all_positions():
    positions = get_positions()

    reviews = []

    for position in positions:
        try:
            reviews.append(review_position(position))
        except Exception as e:
            reviews.append({
                "ticker": position.get("symbol", "UNKNOWN"),
                "reviewed_at": now_iso(),
                "position": position,
                "error": str(e),
                "decision": {
                    "decision": "ERROR",
                    "confidence": 0,
                    "sell_pct": 0,
                    "estimated_qty_to_sell": 0,
                    "reasons": [str(e)],
                },
            })

    summary = {
        "total_positions": len(reviews),
        "take_profit": len([r for r in reviews if r.get("decision", {}).get("decision") == "TAKE_PROFIT"]),
        "trim": len([r for r in reviews if r.get("decision", {}).get("decision") == "TRIM"]),
        "sell": len([r for r in reviews if r.get("decision", {}).get("decision") == "SELL"]),
        "cut_loss": len([r for r in reviews if r.get("decision", {}).get("decision") == "CUT_LOSS"]),
        "hold": len([r for r in reviews if r.get("decision", {}).get("decision") == "HOLD"]),
        "watch_closely": len([r for r in reviews if r.get("decision", {}).get("decision") == "WATCH_CLOSELY"]),
        "errors": len([r for r in reviews if r.get("decision", {}).get("decision") == "ERROR"]),
    }

    payload = {
        "generated_at": now_iso(),
        "summary": summary,
        "reviews": reviews,
    }

    write_json(POSITION_REVIEW_PATH, payload)
    return payload


def get_position_review():
    return read_json(POSITION_REVIEW_PATH, {
        "generated_at": None,
        "summary": {},
        "reviews": [],
    })


def execute_position_sell(ticker: str, sell_pct: float):
    ticker = ticker.upper().strip()
    sell_pct = float(sell_pct)

    if sell_pct <= 0 or sell_pct > 100:
        raise ValueError("sell_pct must be greater than 0 and less than or equal to 100.")

    positions = get_positions()

    matching = None
    for position in positions:
        if position.get("symbol", "").upper() == ticker:
            matching = position
            break

    if not matching:
        raise ValueError(f"No open position found for {ticker}.")

    qty = safe_float(matching.get("qty"))

    if qty <= 0:
        raise ValueError(f"Position quantity for {ticker} is not greater than zero.")

    qty_to_sell = qty * (sell_pct / 100)

    return submit_paper_sell_order(
        ticker=ticker,
        qty=qty_to_sell,
    )


if __name__ == "__main__":
    result = review_all_positions()
    print(json.dumps(result["summary"], indent=2))
