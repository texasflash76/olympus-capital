from orchestrator import run_one_ticker


WATCHLIST = [
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "META",
    "AMZN",
    "TSLA",
    "AMD",
    "AVGO",
    "QQQ",
]


def safe_get(dictionary, *keys, default=None):
    current = dictionary

    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)

    return current if current is not None else default


def score_result(result):
    pm_decision = result.get("pm_decision", {})
    research_brief = result.get("research_brief", {})
    quant_signal = result.get("quant_signal", {})
    risk_result = result.get("risk_result", {})

    score = 0

    if pm_decision.get("decision") == "EXECUTE":
        score += 50
    else:
        score -= 40

    if risk_result.get("approved") is True:
        score += 30
    else:
        score -= 30

    score += research_brief.get("confidence", 0) * 0.5
    score += quant_signal.get("strength", 0) * 0.5

    if pm_decision.get("direction") == "flat":
        score -= 30

    summary = research_brief.get("summary", "").lower()
    if "no recent news" in summary or "no catalysts" in summary:
        score -= 20

    return round(score, 2)


def run_recommender(watchlist=None, top_n=5):
    if watchlist is None:
        watchlist = WATCHLIST

    results = []

    for ticker in watchlist:
        print(f"\n=== Investigating {ticker} ===")

        try:
            result = run_one_ticker(ticker)
            score = score_result(result)

            results.append({
                "ticker": ticker,
                "score": score,
                "final_status": result.get("final_status"),
                "pm_decision": result.get("pm_decision", {}),
                "research_brief": result.get("research_brief", {}),
                "quant_signal": result.get("quant_signal", {}),
                "risk_result": result.get("risk_result", {}),
            })

        except Exception as e:
            results.append({
                "ticker": ticker,
                "score": -999,
                "error": str(e),
            })

    results.sort(key=lambda x: x["score"], reverse=True)

    return results[:top_n]


def print_recommendations(recommendations):
    print("\n==============================")
    print("TOP STOCK RECOMMENDATIONS")
    print("==============================")

    for index, item in enumerate(recommendations, start=1):
        ticker = item.get("ticker")
        score = item.get("score")
        status = item.get("final_status", "ERROR")

        pm = item.get("pm_decision", {})
        research = item.get("research_brief", {})
        quant = item.get("quant_signal", {})
        risk = item.get("risk_result", {})

        print(f"\n{index}. {ticker}")
        print(f"Score: {score}")
        print(f"Status: {status}")
        print(f"PM Decision: {pm.get('decision')}")
        print(f"Direction: {pm.get('direction')}")
        print(f"Suggested Size: {pm.get('size_pct')}%")
        print(f"Research Confidence: {research.get('confidence')}")
        print(f"Quant Strength: {quant.get('strength')}")
        print(f"Risk Approved: {risk.get('approved')}")
        print(f"Reasoning: {pm.get('reasoning')}")

        if item.get("error"):
            print(f"Error: {item.get('error')}")


if __name__ == "__main__":
    recommendations = run_recommender(top_n=5)
    print_recommendations(recommendations)
