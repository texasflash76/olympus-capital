import ta


def add_technicals(df):
    """
    Adds RSI, MACD, Bollinger Bands, and volume ratio to a market data DataFrame.
    """

    df = df.copy()

    df["rsi"] = ta.momentum.RSIIndicator(
        close=df["close"],
        window=14
    ).rsi()

    macd = ta.trend.MACD(close=df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()

    bollinger = ta.volatility.BollingerBands(close=df["close"])
    df["bollinger_upper"] = bollinger.bollinger_hband()
    df["bollinger_lower"] = bollinger.bollinger_lband()

    df["volume_20d_avg"] = df["volume"].rolling(window=20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_20d_avg"]

    return df


def get_latest_technical_summary(df):
    """
    Returns the most recent row of technical indicators as a clean dictionary.
    """

    latest = df.dropna().iloc[-1]

    return {
        "close": round(latest["close"], 2),
        "rsi": round(latest["rsi"], 2),
        "macd": round(latest["macd"], 2),
        "macd_signal": round(latest["macd_signal"], 2),
        "bollinger_upper": round(latest["bollinger_upper"], 2),
        "bollinger_lower": round(latest["bollinger_lower"], 2),
        "volume_ratio": round(latest["volume_ratio"], 2),
    }