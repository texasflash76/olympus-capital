import json
from pathlib import Path
from datetime import datetime, timezone

from screener import screen_market
from quality_filter import evaluate_candidate_quality
from tools.web_research import get_company_profile


FAST_SCAN_RESULTS_PATH = Path("fast_scan_results.json")
FAST_SCAN_STATUS_PATH = Path("fast_scan_status.json")


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


def run_fast_scan(
    top_n=50,
    max_symbols=500,
    sector=None,
    only_quality_approved=False,
):
    """
    Fast scan does NOT call the LLM.

    It only:
    - runs market screener
    - enriches company profile/sector
    - runs quality filter
    - returns candidates for possible deep AI review
    """
    sector = str(sector or "").strip()

    write_json(FAST_SCAN_STATUS_PATH, {
        "status": "running",
        "message": "Running fast scan.",
        "started_at": now_iso(),
        "top_n": top_n,
        "max_symbols": max_symbols,
        "sector": sector or None,
    })

    candidates = screen_market(top_n=top_n, max_symbols=max_symbols)

    reviewed = []
    total_candidates = len(candidates)

    for i, candidate in enumerate(candidates, start=1):
        ticker = str(candidate.get("ticker", "")).upper().strip()

        write_json(FAST_SCAN_STATUS_PATH, {
            "status": "running",
            "message": f"Enriching {ticker} with company sector and quality review.",
            "current": i - 1,
            "total": total_candidates,
            "percent": round(((i - 1) / max(total_candidates, 1)) * 100, 1),
            "current_ticker": ticker,
            "updated_at": now_iso(),
            "top_n": top_n,
            "max_symbols": max_symbols,
            "sector": sector or None,
        })

        if not ticker:
            continue

        try:
            profile = get_company_profile(ticker)
        except Exception as e:
            profile = {
                "ticker": ticker,
                "company_name": ticker,
                "sector": "Unknown",
                "industry": "Unknown",
                "source": "Profile failed",
                "error": str(e),
            }

        candidate_sector = profile.get("sector", "Unknown")

        if sector and sector != "All" and candidate_sector != sector:
            continue

        technical_summary = candidate.get("technical_summary", {}) or {}

        try:
            quality_review = evaluate_candidate_quality(
                ticker=ticker,
                technical_summary=technical_summary,
            )
        except Exception as e:
            quality_review = {
                "approved": False,
                "reasons": [f"Quality review failed: {e}"],
                "warnings": [],
                "metrics": {},
            }

        if only_quality_approved and not quality_review.get("approved"):
            continue

        reviewed.append({
            "ticker": ticker,
            "company_name": profile.get("company_name", ticker),
            "sector": candidate_sector,
            "industry": profile.get("industry", "Unknown"),
            "profile_source": profile.get("source", "Unknown"),
            "screener_score": candidate.get("screener_score", candidate.get("score")),
            "screener_reasoning": candidate.get("reasoning"),
            "technical_summary": technical_summary,
            "quality_review": quality_review,
        })

    reviewed.sort(
        key=lambda item: (
            1 if item.get("quality_review", {}).get("approved") else 0,
            float(item.get("screener_score") or 0),
        ),
        reverse=True,
    )

    payload = {
        "generated_at": now_iso(),
        "top_n": top_n,
        "max_symbols": max_symbols,
        "sector": sector or None,
        "only_quality_approved": only_quality_approved,
        "candidates": reviewed,
        "summary": {
            "total_candidates": len(reviewed),
            "quality_approved": len([
                item for item in reviewed
                if item.get("quality_review", {}).get("approved")
            ]),
            "quality_rejected": len([
                item for item in reviewed
                if not item.get("quality_review", {}).get("approved")
            ]),
        },
    }

    write_json(FAST_SCAN_RESULTS_PATH, payload)

    write_json(FAST_SCAN_STATUS_PATH, {
        "status": "complete",
        "message": f"Fast scan complete. Found {len(reviewed)} candidate(s).",
        "current": total_candidates,
        "total": total_candidates,
        "percent": 100,
        "current_ticker": None,
        "finished_at": now_iso(),
        "top_n": top_n,
        "max_symbols": max_symbols,
        "sector": sector or None,
    })

    return payload


def get_fast_scan_results():
    return read_json(FAST_SCAN_RESULTS_PATH, {
        "generated_at": None,
        "candidates": [],
        "summary": {},
    })


def get_fast_scan_status():
    return read_json(FAST_SCAN_STATUS_PATH, {
        "status": "not_started",
        "message": "No fast scan has been run yet.",
    })


if __name__ == "__main__":
    result = run_fast_scan(top_n=25, max_symbols=300)
    print(json.dumps(result["summary"], indent=2))
