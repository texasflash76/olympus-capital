import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

from orchestrator import run_one_ticker
from fast_scan import (
    get_fast_scan_results,
    run_fast_scan,
)
from trade_thesis_store import record_trade_thesis_from_result


DEEP_REVIEW_RESULTS_PATH = Path("deep_review_results.json")
DEEP_REVIEW_STATUS_PATH = Path("deep_review_status.json")


BUY_STATUSES = {
    "RECOMMENDED_NOT_EXECUTED",
    "PAPER_TRADE_SUBMITTED",
    "TRADE_APPROVED_BUT_EXECUTION_BLOCKED",
}


GOOD_REVIEW_STATUSES = BUY_STATUSES | {
    "TRADE_VETOED_BY_PM",
    "TRADE_REJECTED_BY_RISK",
    "EXECUTION_SKIPPED",
}


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


def parse_tickers(tickers):
    if tickers is None:
        return []

    if isinstance(tickers, str):
        raw = tickers.replace("\n", ",").replace(" ", ",").split(",")
    else:
        raw = tickers

    clean = []
    seen = set()

    for ticker in raw:
        ticker = str(ticker).upper().strip()

        if not ticker:
            continue

        if not ticker.isalnum() or len(ticker) > 10:
            continue

        if ticker in seen:
            continue

        seen.add(ticker)
        clean.append(ticker)

    return clean


def extract_fast_scan_candidates():
    """
    Reads the latest fast_scan_results.json and returns candidates.

    Expected shape:
    {
      "candidates": [...]
    }

    But this is defensive in case your file uses "results" or "reviewed".
    """
    data = get_fast_scan_results()

    candidates = (
        data.get("candidates")
        or data.get("results")
        or data.get("reviewed")
        or []
    )

    if not isinstance(candidates, list):
        candidates = []

    return data, candidates


def candidate_quality_score(candidate):
    """
    Ranking score for choosing which Fast Scan names deserve expensive AI review.

    This is intentionally stricter than the raw screener score.
    It tries to pick names that are:
    - quality approved
    - liquid enough
    - technically bullish
    - not insanely overbought
    - showing some volume confirmation
    """
    quality = candidate.get("quality_review", {}) or {}
    metrics = quality.get("metrics", {}) or {}
    technicals = candidate.get("technical_summary", {}) or {}

    score = 0

    # 1. Quality approval matters most
    if quality.get("approved"):
        score += 1000
    else:
        score -= 500

    # 2. Raw screener score
    screener_score = safe_float(
        candidate.get("screener_score", candidate.get("score", 0))
    )
    score += screener_score * 10

    # 3. Liquidity
    dollar_volume = safe_float(
        metrics.get("avg_dollar_volume")
        or metrics.get("dollar_volume")
        or metrics.get("20d_avg_dollar_volume")
        or 0
    )

    avg_volume = safe_float(
        metrics.get("avg_volume")
        or metrics.get("volume_20d_avg")
        or metrics.get("20d_avg_volume")
        or 0
    )

    if dollar_volume >= 500_000_000:
        score += 80
    elif dollar_volume >= 100_000_000:
        score += 60
    elif dollar_volume >= 50_000_000:
        score += 40
    elif dollar_volume >= 10_000_000:
        score += 20
    else:
        score -= 50

    if avg_volume >= 5_000_000:
        score += 40
    elif avg_volume >= 1_000_000:
        score += 25
    elif avg_volume >= 500_000:
        score += 10
    else:
        score -= 40

    # 4. Technical setup
    rsi = safe_float(technicals.get("rsi"), 50)
    macd = safe_float(technicals.get("macd"), 0)
    macd_signal = safe_float(technicals.get("macd_signal"), 0)
    close = safe_float(technicals.get("close"), 0)
    bollinger_upper = safe_float(technicals.get("bollinger_upper"), 0)
    bollinger_lower = safe_float(technicals.get("bollinger_lower"), 0)
    volume_ratio = safe_float(technicals.get("volume_ratio"), 1)

    # RSI sweet spot: bullish but not overheated
    if 45 <= rsi <= 68:
        score += 60
    elif 35 <= rsi < 45:
        score += 20
    elif 68 < rsi <= 74:
        score -= 10
    elif rsi > 74:
        score -= 120
    elif rsi < 30:
        score -= 40

    # MACD confirmation
    if macd > macd_signal:
        score += 50
    else:
        score -= 30

    # Volume confirmation
    if volume_ratio >= 2:
        score += 40
    elif volume_ratio >= 1.25:
        score += 25
    elif volume_ratio >= 1:
        score += 10
    elif volume_ratio < 0.75:
        score -= 30

    # Avoid names already stretched to upper Bollinger
    if close > 0 and bollinger_upper > 0 and close >= bollinger_upper * 0.98:
        score -= 50

    # Avoid names near lower band unless volume/MACD confirm
    if close > 0 and bollinger_lower > 0 and close <= bollinger_lower * 1.02 and macd <= macd_signal:
        score -= 40

    candidate["_ranking_debug"] = {
        "screener_score": screener_score,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "volume_ratio": volume_ratio,
        "avg_volume": avg_volume,
        "avg_dollar_volume": dollar_volume,
        "final_selection_score": round(score, 2),
    }

    return round(score, 2)


def select_candidates_for_deep_review(
    max_candidates=8,
    only_quality_approved=True,
    include_previous_errors=False,
):
    """
    Selects top Fast Scan candidates for Deep Review.

    Simple rule:
    - Read fast_scan_results.json
    - Keep quality-approved candidates
    - Skip names already completed in Deep Review
    - Sort by the visible Fast Scan score
    - Return the top max_candidates

    This keeps the dashboard easy to understand:
    Fast Scan ranking = Deep Review input ranking.
    """
    fast_scan_data, candidates = extract_fast_scan_candidates()

    if not candidates:
        raise ValueError(
            "No Fast Scan candidates found. Run Fast Scan first with: python3 fast_scan.py"
        )

    old_deep = get_deep_review_results()
    previous_results = old_deep.get("results", [])

    previously_completed = set()
    previously_errored = set()

    for item in previous_results:
        ticker = str(item.get("ticker", "")).upper().strip()
        status = str(item.get("final_status", ""))

        if not ticker:
            continue

        if status == "ERROR":
            previously_errored.add(ticker)
        else:
            previously_completed.add(ticker)

    ranked = []

    for candidate in candidates:
        ticker = str(candidate.get("ticker", "")).upper().strip()

        if not ticker:
            continue

        quality = candidate.get("quality_review", {}) or {}

        if only_quality_approved and not quality.get("approved"):
            continue

        if ticker in previously_completed:
            continue

        if ticker in previously_errored and not include_previous_errors:
            continue

        enriched = dict(candidate)

        fast_scan_score = (
            candidate.get("score")
            or candidate.get("fast_scan_score")
            or candidate.get("candidate_score")
            or candidate.get("quality_score")
            or candidate.get("technical_score")
            or candidate.get("ranking_score")
            or candidate.get("total_score")
            or candidate.get("scan_score")
            or 0
        )

        enriched["fast_scan_score"] = safe_float(fast_scan_score)
        enriched["_deep_review_selection_score"] = safe_float(fast_scan_score)
        enriched["_selection_method"] = "visible_fast_scan_score"

        ranked.append(enriched)

    ranked.sort(
        key=lambda item: safe_float(item.get("fast_scan_score")),
        reverse=True,
    )

    selected = ranked[:max_candidates]

    return {
        "fast_scan_generated_at": fast_scan_data.get("generated_at"),
        "available_candidates": len(candidates),
        "eligible_candidates": len(ranked),
        "selected": selected,
        "selected_tickers": [item.get("ticker") for item in selected],
        "selection_method": "top_visible_fast_scan_score",
    }


def score_deep_review_result(result):
    """
    Final ranking score after AI review.

    This lets the output clearly tell you which ideas are best.
    """
    status = str(result.get("final_status", ""))

    research = result.get("research_brief", {}) or {}
    quant = result.get("quant_signal", {}) or {}
    pm = result.get("pm_decision", {}) or {}
    risk = result.get("risk_result", {}) or {}

    score = 0

    if status in BUY_STATUSES:
        score += 1000
    elif "VETO" in status:
        score -= 100
    elif "ERROR" in status:
        score -= 500

    score += safe_float(research.get("confidence")) * 1.5
    score += safe_float(quant.get("strength")) * 1.5

    pm_decision = str(pm.get("decision", "")).upper()

    if pm_decision in ["EXECUTE", "BUY", "APPROVE"]:
        score += 150
    elif "VETO" in pm_decision or "REJECT" in pm_decision:
        score -= 150

    if risk.get("approved") is True:
        score += 100
    elif risk.get("approved") is False:
        score -= 100

    size_pct = safe_float(pm.get("size_pct"))
    score += size_pct * 5

    return round(score, 2)


def summarize_deep_review_results(results):
    recommended = [
        item for item in results
        if item.get("final_status") in BUY_STATUSES
    ]

    vetoed = [
        item for item in results
        if "VETO" in str(item.get("final_status", ""))
    ]

    risk_rejected = [
        item for item in results
        if "RISK" in str(item.get("final_status", ""))
        or str(item.get("risk_result", {}).get("approved")) == "False"
    ]

    errors = [
        item for item in results
        if item.get("final_status") == "ERROR"
        or item.get("deep_review_status") == "error"
    ]

    return {
        "total": len(results),
        "recommended": len(recommended),
        "vetoed": len(vetoed),
        "risk_rejected": len(risk_rejected),
        "errors": len(errors),
        "best_ideas": [
            {
                "ticker": item.get("ticker"),
                "final_status": item.get("final_status"),
                "deep_review_score": item.get("deep_review_score"),
            }
            for item in results[:5]
        ],
    }


def run_deep_review(
    tickers=None,
    allow_execution=False,
    max_candidates=8,
    only_quality_approved=True,
    include_previous_errors=False,
    auto_fast_scan=False,
    fast_scan_top_n=75,
    fast_scan_max_symbols=500,
):
    """
    Runs full Olympus AI review.

    If tickers are provided:
        Deep Review behaves like batch ticker investigation.

    If no tickers are provided:
        Deep Review pulls the best names from fast_scan_results.json.

    This is the intended lifecycle:
        Fast Scan -> Deep Review -> Recommendation/Buy -> Position Monitor
    """

    manual_tickers = parse_tickers(tickers)

    selection_info = {
        "mode": "manual_tickers" if manual_tickers else "fast_scan_candidates",
        "selected": [],
        "selected_tickers": manual_tickers,
    }

    if manual_tickers:
        review_tickers = manual_tickers
    else:
        try:
            selection_info = select_candidates_for_deep_review(
                max_candidates=max_candidates,
                only_quality_approved=only_quality_approved,
                include_previous_errors=include_previous_errors,
            )
        except ValueError:
            if not auto_fast_scan:
                raise

            print("No usable Fast Scan results found. Running Fast Scan first...")

            run_fast_scan(
                top_n=fast_scan_top_n,
                max_symbols=fast_scan_max_symbols,
                only_quality_approved=False,
            )

            selection_info = select_candidates_for_deep_review(
                max_candidates=max_candidates,
                only_quality_approved=only_quality_approved,
                include_previous_errors=include_previous_errors,
            )

        review_tickers = parse_tickers(selection_info.get("selected_tickers", []))

    if not review_tickers:
        payload = {
            "generated_at": now_iso(),
            "allow_execution": allow_execution,
            "selection": selection_info,
            "tickers": [],
            "results": [],
            "summary": {
                "total": 0,
                "recommended": 0,
                "vetoed": 0,
                "risk_rejected": 0,
                "errors": 0,
                "message": (
                    "No eligible tickers selected. Run Fast Scan, loosen quality filters, "
                    "or pass tickers manually with --tickers AAPL,MSFT."
                ),
            },
        }

        write_json(DEEP_REVIEW_RESULTS_PATH, payload)
        return payload

    write_json(DEEP_REVIEW_STATUS_PATH, {
        "status": "running",
        "message": "Deep AI review running.",
        "current": 0,
        "total": len(review_tickers),
        "current_ticker": None,
        "started_at": now_iso(),
        "selection_mode": selection_info.get("mode", "fast_scan_candidates"),
        "tickers": review_tickers,
    })

    print("\n=== DEEP REVIEW STARTED ===")
    print(f"Mode: {selection_info.get('mode', 'fast_scan_candidates')}")
    print(f"Tickers: {', '.join(review_tickers)}")
    print(f"Allow execution: {allow_execution}")
    print("=" * 80)

    results = []

    for i, ticker in enumerate(review_tickers, start=1):
        write_json(DEEP_REVIEW_STATUS_PATH, {
            "status": "running",
            "message": f"Running deep AI review for {ticker}.",
            "current": i - 1,
            "total": len(review_tickers),
            "current_ticker": ticker,
            "updated_at": now_iso(),
            "tickers": review_tickers,
        })

        try:
            result = run_one_ticker(
                ticker=ticker,
                allow_execution=allow_execution,
            )

            result["deep_review_status"] = "complete"
            result["deep_review_score"] = score_deep_review_result(result)

            try:
                thesis = record_trade_thesis_from_result(
                    result,
                    source="deep_review",
                )
                result["trade_thesis_saved"] = bool(thesis)
            except Exception as thesis_error:
                result["trade_thesis_saved"] = False
                result["trade_thesis_error"] = str(thesis_error)

            results.append(result)

        except Exception as e:
            error_result = {
                "ticker": ticker,
                "deep_review_status": "error",
                "final_status": "ERROR",
                "error": str(e),
                "deep_review_score": -500,
            }
            results.append(error_result)

    results.sort(
        key=lambda item: safe_float(item.get("deep_review_score"), -9999),
        reverse=True,
    )

    payload = {
        "generated_at": now_iso(),
        "allow_execution": allow_execution,
        "selection": selection_info,
        "tickers": review_tickers,
        "results": results,
        "summary": summarize_deep_review_results(results),
    }

    write_json(DEEP_REVIEW_RESULTS_PATH, payload)

    write_json(DEEP_REVIEW_STATUS_PATH, {
        "status": "complete",
        "message": f"Deep review complete for {len(results)} ticker(s).",
        "current": len(review_tickers),
        "total": len(review_tickers),
        "current_ticker": None,
        "finished_at": now_iso(),
        "selection_mode": selection_info.get("mode", "fast_scan_candidates"),
        "tickers": review_tickers,
    })

    print("\n=== DEEP REVIEW COMPLETE ===")
    print(json.dumps(payload["summary"], indent=2))

    return payload


def get_deep_review_results():
    return read_json(DEEP_REVIEW_RESULTS_PATH, {
        "generated_at": None,
        "results": [],
        "summary": {},
        "selection": {},
    })


def get_deep_review_status():
    return read_json(DEEP_REVIEW_STATUS_PATH, {
        "status": "not_started",
        "message": "No deep review has been run yet.",
    })


def main():
    parser = argparse.ArgumentParser(description="Run Olympus Deep Review.")

    parser.add_argument(
        "--tickers",
        default=None,
        help="Optional comma-separated tickers. If omitted, uses latest Fast Scan winners.",
    )

    parser.add_argument(
        "--max-candidates",
        type=int,
        default=8,
        help="Max Fast Scan candidates to deep review when --tickers is omitted.",
    )

    parser.add_argument(
        "--allow-execution",
        action="store_true",
        help="Allow paper execution if the orchestrator approves it.",
    )

    parser.add_argument(
        "--include-rejected-quality",
        action="store_true",
        help="Allow Deep Review to include Fast Scan names that failed quality filter.",
    )

    parser.add_argument(
        "--include-previous-errors",
        action="store_true",
        help="Retry tickers that previously errored in Deep Review.",
    )

    parser.add_argument(
        "--auto-fast-scan",
        action="store_true",
        help="Run Fast Scan automatically if no Fast Scan results exist.",
    )

    parser.add_argument(
        "--fast-scan-top-n",
        type=int,
        default=75,
        help="Top N for auto Fast Scan.",
    )

    parser.add_argument(
        "--fast-scan-max-symbols",
        type=int,
        default=500,
        help="Max symbols for auto Fast Scan.",
    )

    args = parser.parse_args()

    result = run_deep_review(
        tickers=args.tickers,
        allow_execution=args.allow_execution,
        max_candidates=args.max_candidates,
        only_quality_approved=not args.include_rejected_quality,
        include_previous_errors=args.include_previous_errors,
        auto_fast_scan=args.auto_fast_scan,
        fast_scan_top_n=args.fast_scan_top_n,
        fast_scan_max_symbols=args.fast_scan_max_symbols,
    )

    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
