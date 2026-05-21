import json
from pathlib import Path
from datetime import datetime, timezone
from tools.web_research import get_web_research
from screener import screen_market
from orchestrator import run_one_ticker


RECOMMENDER_RESULTS_PATH = Path("recommender_results.json")
RECOMMENDER_STATUS_PATH = Path("recommender_status.json")
RECOMMENDER_HISTORY_PATH = Path("recommender_history.json")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def make_batch_id():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2))


def read_json(path: Path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_progress(
    batch_id: str,
    status: str,
    current: int,
    total: int,
    current_ticker=None,
    message: str = "",
):
    percent = 0.0

    if total:
        percent = round((current / total) * 100, 1)

    write_json(
        RECOMMENDER_STATUS_PATH,
        {
            "batch_id": batch_id,
            "status": status,
            "current": current,
            "total": total,
            "percent": percent,
            "current_ticker": current_ticker,
            "message": message,
            "updated_at": now_iso(),
        },
    )


def save_batch_to_history(batch: dict):
    history = read_json(RECOMMENDER_HISTORY_PATH, {"batches": []})

    history["batches"].insert(0, batch)
    history["batches"] = history["batches"][:10]

    write_json(RECOMMENDER_HISTORY_PATH, history)


def score_result(result):
    score = 0

    research = result.get("research_brief", {})
    quant = result.get("quant_signal", {})
    pm = result.get("pm_decision", {})
    risk = result.get("risk_result", {})

    if pm.get("decision") == "EXECUTE":
        score += 50

    if risk.get("approved") is True:
        score += 30
    else:
        score -= 30

    score += float(research.get("confidence", 0) or 0) * 0.4
    score += float(quant.get("strength", 0) or 0) * 0.4

    if quant.get("direction") == "flat":
        score -= 25

    if result.get("final_status") == "RECOMMENDED_NOT_EXECUTED":
        score += 10

    if result.get("final_status") == "PAPER_TRADE_SUBMITTED":
        score -= 100

    return round(score, 2)


def classify_result(result):
    pm = result.get("pm_decision", {})
    risk = result.get("risk_result", {})

    if result.get("error"):
        return "ERROR"

    if (
        pm.get("decision") == "EXECUTE"
        and risk.get("approved") is True
        and result.get("final_status") == "RECOMMENDED_NOT_EXECUTED"
    ):
        return "RECOMMENDATION"

    if pm.get("decision") == "VETO":
        return "WATCHLIST_OR_REJECTED"

    if risk.get("approved") is False:
        return "RISK_BLOCKED"

    return "REVIEWED"


def run_recommender(top_screener_n=5, final_n=3, max_symbols=300):
    """
    Runs the recommendation pipeline.

    Important:
    - This does NOT submit trades.
    - It runs run_one_ticker(..., allow_execution=False).
    - If PM and risk approve, final_status should become RECOMMENDED_NOT_EXECUTED.
    """

    batch_id = make_batch_id()

    write_progress(
        batch_id=batch_id,
        status="running",
        current=0,
        total=0,
        current_ticker=None,
        message="Running market screener to find top candidates.",
    )

    print("\n=== RECOMMENDER STARTED ===")
    print(f"Batch ID: {batch_id}")
    print(f"Top screener candidates: {top_screener_n}")
    print(f"Final recommendations: {final_n}")
    print(f"Max symbols: {max_symbols}")
    print("\nRunning screener...")

    candidates = screen_market(top_n=top_screener_n, max_symbols=max_symbols)

    total_candidates = len(candidates)

    write_progress(
        batch_id=batch_id,
        status="running",
        current=0,
        total=total_candidates,
        current_ticker=None,
        message=f"Screener complete. Found {total_candidates} candidates. Starting full Olympus analysis.",
    )

    reviewed = []

    for i, candidate in enumerate(candidates, start=1):
        ticker = candidate.get("ticker")

        write_progress(
            batch_id=batch_id,
            status="running",
            current=i - 1,
            total=total_candidates,
            current_ticker=ticker,
            message=f"Running full Olympus analysis for {ticker}.",
        )

        print(f"\n[{i}/{total_candidates}] Running full Olympus loop for {ticker}...")

        try:
            result = run_one_ticker(ticker, allow_execution=False)
            web_research = get_web_research(ticker, limit=5)

            company_profile = web_research.get("company_profile", {})
            market_context = web_research.get("market_context", {})

            result["recent_headlines"] = web_research
            result["company_profile"] = company_profile
            result["sector"] = company_profile.get("sector", result.get("sector", "Unknown"))
            result["industry"] = company_profile.get("industry", result.get("industry", "Unknown"))
            result["market_sentiment"] = market_context.get("market_sentiment_heuristic", {})
            result["batch_id"] = batch_id
            result["screener_score"] = candidate.get("score")
            result["screener_reasoning"] = candidate.get("reasoning")
            result["recommendation_score"] = score_result(result)
            result["recommendation_type"] = classify_result(result)

            reviewed.append(result)

        except Exception as e:
            reviewed.append(
                {
                    "batch_id": batch_id,
                    "ticker": ticker,
                    "error": str(e),
                    "screener_score": candidate.get("score"),
                    "screener_reasoning": candidate.get("reasoning"),
                    "recommendation_score": -999,
                    "recommendation_type": "ERROR",
                    "final_status": "ERROR",
                }
            )

        write_progress(
            batch_id=batch_id,
            status="running",
            current=i,
            total=total_candidates,
            current_ticker=ticker,
            message=f"Finished analysis for {ticker}.",
        )

    reviewed = sorted(
        reviewed,
        key=lambda x: x.get("recommendation_score", -999),
        reverse=True,
    )

    executable_recommendations = [
        item
        for item in reviewed
        if item.get("recommendation_type") == "RECOMMENDATION"
    ]

    watchlist_candidates = [
        item
        for item in reviewed
        if item.get("recommendation_type") != "RECOMMENDATION"
    ]

    output = {
        "batch_id": batch_id,
        "generated_at": now_iso(),
        "top_screener_n": top_screener_n,
        "final_n": final_n,
        "max_symbols": max_symbols,
        "recommendations": executable_recommendations[:final_n],
        "watchlist_candidates": watchlist_candidates[:final_n],
        "all_reviewed": reviewed,
        "summary": {
            "total_reviewed": len(reviewed),
            "recommended_not_executed": len(executable_recommendations),
            "watchlist_or_rejected": len(watchlist_candidates),
            "errors": len([x for x in reviewed if x.get("recommendation_type") == "ERROR"]),
        },
    }

    write_json(RECOMMENDER_RESULTS_PATH, output)
    save_batch_to_history(output)

    write_progress(
        batch_id=batch_id,
        status="complete",
        current=total_candidates,
        total=total_candidates,
        current_ticker=None,
        message=f"Recommendation batch {batch_id} complete. Found {len(executable_recommendations)} recommendation(s).",
    )

    print("\n=== RECOMMENDER COMPLETE ===")
    print(f"Batch ID: {batch_id}")
    print(f"Reviewed: {len(reviewed)}")
    print(f"Recommendations: {len(executable_recommendations)}")
    print(f"Results saved to: {RECOMMENDER_RESULTS_PATH}")
    print(f"History saved to: {RECOMMENDER_HISTORY_PATH}")

    return output


if __name__ == "__main__":
    results = run_recommender(top_screener_n=5, final_n=3, max_symbols=300)

    print("\n=== RECOMMENDED, NOT EXECUTED ===")
    recommendations = results.get("recommendations", [])

    if not recommendations:
        print("No executable recommendations found.")
    else:
        for rec in recommendations:
            print(
                rec.get("ticker"),
                rec.get("recommendation_score"),
                rec.get("final_status"),
            )
