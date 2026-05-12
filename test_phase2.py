from tools.market_data import get_bars
from tools.technicals import add_technicals, get_latest_technical_summary
from tools.news_fetcher import get_news
from tools.broker import get_account_summary, get_positions


def main():
    ticker = "TSLA"

    print(f"\n=== {ticker} Technical Summary ===")

    bars = get_bars(ticker)
    bars_with_technicals = add_technicals(bars)
    summary = get_latest_technical_summary(bars_with_technicals)

    for key, value in summary.items():
        print(f"{key}: {value}")

    print(f"\n=== Recent {ticker} News ===")

    headlines = get_news(ticker)

    if not headlines:
        print("No recent news found.")
    else:
        for i, article in enumerate(headlines, start=1):
            print(f"{i}. {article['headline']} — {article['source']}")

    print("\n=== Alpaca Paper Account ===")

    account = get_account_summary()

    for key, value in account.items():
        print(f"{key}: {value}")

    print("\n=== Current Positions ===")

    positions = get_positions()

    if not positions:
        print("No open positions.")
    else:
        for position in positions:
            print(position)


if __name__ == "__main__":
    main()