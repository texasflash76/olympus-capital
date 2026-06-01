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




# ===================== RELATIVE FAST SCAN RANKING =====================

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def percentile_score(value, values, higher_is_better=True):
    clean = sorted([safe_float(v) for v in values if v is not None])

    if len(clean) <= 1:
        return 50.0

    value = safe_float(value)
    below_or_equal = len([v for v in clean if v <= value])
    pct = ((below_or_equal - 1) / (len(clean) - 1)) * 100

    if not higher_is_better:
        pct = 100 - pct

    return clamp(pct)


def sweet_spot_score(value, ideal_low, ideal_high, hard_low, hard_high):
    value = safe_float(value)

    if ideal_low <= value <= ideal_high:
        return 100.0

    if value < ideal_low:
        if value <= hard_low:
            return 0.0
        return clamp(((value - hard_low) / max(ideal_low - hard_low, 1)) * 100)

    if value > ideal_high:
        if value >= hard_high:
            return 0.0
        return clamp(((hard_high - value) / max(hard_high - ideal_high, 1)) * 100)

    return 50.0


def calculate_relative_fast_scan_scores(candidates):
    """
    Takes all Fast Scan candidates that made it through the basic criteria,
    then ranks them relative to each other.

    This makes the final score answer:
    'How optimal is this candidate compared with the other candidates today?'
    """
    if not candidates:
        return []

    screener_scores = []
    dollar_volumes = []
    volume_ratios = []
    macd_spreads = []
    upper_band_rooms = []

    for item in candidates:
        tech = item.get("technical_summary", {}) or {}

        close = safe_float(
            tech.get("close")
            or tech.get("latest_close")
            or tech.get("price")
        )

        avg_volume = safe_float(
            tech.get("avg_volume")
            or tech.get("average_volume")
            or tech.get("volume_sma")
            or tech.get("volume")
        )

        volume_ratio = safe_float(tech.get("volume_ratio"), 1)
        macd = safe_float(tech.get("macd"))
        macd_signal = safe_float(tech.get("macd_signal"))
        bollinger_upper = safe_float(
            tech.get("bollinger_upper")
            or tech.get("bb_upper")
            or tech.get("upper_band")
        )

        screener_score = safe_float(item.get("screener_score"))
        dollar_volume = close * avg_volume if close and avg_volume else 0
        macd_spread = macd - macd_signal

        if close > 0 and bollinger_upper > 0:
            upper_band_room_pct = ((bollinger_upper - close) / close) * 100
        else:
            upper_band_room_pct = 0

        screener_scores.append(screener_score)
        dollar_volumes.append(dollar_volume)
        volume_ratios.append(volume_ratio)
        macd_spreads.append(macd_spread)
        upper_band_rooms.append(upper_band_room_pct)

    ranked = []

    for item in candidates:
        tech = item.get("technical_summary", {}) or {}

        close = safe_float(
            tech.get("close")
            or tech.get("latest_close")
            or tech.get("price")
        )

        rsi = safe_float(tech.get("rsi"), 50)

        avg_volume = safe_float(
            tech.get("avg_volume")
            or tech.get("average_volume")
            or tech.get("volume_sma")
            or tech.get("volume")
        )

        volume_ratio = safe_float(tech.get("volume_ratio"), 1)
        macd = safe_float(tech.get("macd"))
        macd_signal = safe_float(tech.get("macd_signal"))
        bollinger_upper = safe_float(
            tech.get("bollinger_upper")
            or tech.get("bb_upper")
            or tech.get("upper_band")
        )

        screener_score = safe_float(item.get("screener_score"))
        dollar_volume = close * avg_volume if close and avg_volume else 0
        macd_spread = macd - macd_signal

        if close > 0 and bollinger_upper > 0:
            upper_band_room_pct = ((bollinger_upper - close) / close) * 100
        else:
            upper_band_room_pct = 0

        components = {
            "screener_relative": percentile_score(screener_score, screener_scores, True),
            "liquidity_relative": percentile_score(dollar_volume, dollar_volumes, True),
            "volume_confirmation_relative": percentile_score(volume_ratio, volume_ratios, True),
            "macd_confirmation_relative": percentile_score(macd_spread, macd_spreads, True),
            "rsi_setup_quality": sweet_spot_score(
                rsi,
                ideal_low=45,
                ideal_high=68,
                hard_low=25,
                hard_high=78,
            ),
            "not_overextended_relative": percentile_score(upper_band_room_pct, upper_band_rooms, True),
        }

        fast_scan_score = (
            components["screener_relative"] * 0.25
            + components["liquidity_relative"] * 0.18
            + components["volume_confirmation_relative"] * 0.18
            + components["macd_confirmation_relative"] * 0.16
            + components["rsi_setup_quality"] * 0.15
            + components["not_overextended_relative"] * 0.08
        )

        enriched = dict(item)
        enriched["score"] = round(fast_scan_score, 2)
        enriched["fast_scan_score"] = round(fast_scan_score, 2)
        enriched["ranking_method"] = "relative_to_current_fast_scan_pool"
        enriched["relative_rank_components"] = {
            key: round(value, 2)
            for key, value in components.items()
        }
        enriched["relative_rank_metrics"] = {
            "screener_score": round(screener_score, 2),
            "close": round(close, 2),
            "rsi": round(rsi, 2),
            "avg_volume": round(avg_volume, 2),
            "dollar_volume": round(dollar_volume, 2),
            "volume_ratio": round(volume_ratio, 2),
            "macd": round(macd, 4),
            "macd_signal": round(macd_signal, 4),
            "macd_spread": round(macd_spread, 4),
            "upper_band_room_pct": round(upper_band_room_pct, 2),
        }

        ranked.append(enriched)

    ranked.sort(key=lambda x: safe_float(x.get("fast_scan_score")), reverse=True)

    for idx, item in enumerate(ranked, start=1):
        item["fast_scan_rank"] = idx

    return ranked


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

    # Pull a larger internal pool first, then rank the survivors relative to each other.
    # top_n controls final displayed/saved results, not the size of the scan universe.
    internal_top_n = max(int(top_n or 50) * 4, 100)
    candidates = screen_market(top_n=internal_top_n, max_symbols=max_symbols)

    reviewed = []
    total_candidates = len(candidates)

    for i, candidate in enumerate(candidates, start=1):
        ticker = str(candidate.get("ticker", "")).upper().strip()

        write_json(FAST_SCAN_STATUS_PATH, {
            "status": "running",
            "message": f"Enriching {ticker} with company sector and quality review.",
            "current": i - 1,
            "total": total_candidates,
            "percent": round(25 + (((i - 1) / max(total_candidates, 1)) * 60), 1),
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

    # Keep all candidates that match the scan criteria, then rank them by relative quality.
    # Quality-approved names are ranked first. Rejected names remain visible below them
    # unless only_quality_approved=True.
    approved_reviewed = [
        item for item in reviewed
        if item.get("quality_review", {}).get("approved")
    ]

    rejected_reviewed = [
        item for item in reviewed
        if not item.get("quality_review", {}).get("approved")
    ]

    ranked_approved = calculate_relative_fast_scan_scores(approved_reviewed)
    ranked_rejected = calculate_relative_fast_scan_scores(rejected_reviewed)

    ranked_all = ranked_approved if only_quality_approved else ranked_approved + ranked_rejected

    display_candidates = ranked_all[:int(top_n or len(ranked_all))]

    write_json(FAST_SCAN_STATUS_PATH, {
        "status": "running",
        "message": f"Phase 3/3: ranking {len(ranked_all)} eligible candidates relative to each other.",
        "current": 3,
        "total": 3,
        "percent": 90,
        "phase": "relative_ranking",
        "current_ticker": None,
        "updated_at": now_iso(),
        "top_n": top_n,
        "max_symbols": max_symbols,
        "sector": sector or None,
    })

    payload = {
        "generated_at": now_iso(),
        "top_n": top_n,
        "internal_top_n": internal_top_n,
        "max_symbols": max_symbols,
        "sector": sector or None,
        "only_quality_approved": only_quality_approved,
        "ranking_method": "criteria_match_then_relative_ranking",
        "ranking_weights": {
            "screener_relative": 0.25,
            "liquidity_relative": 0.18,
            "volume_confirmation_relative": 0.18,
            "macd_confirmation_relative": 0.16,
            "rsi_setup_quality": 0.15,
            "not_overextended_relative": 0.08
        },
        "candidates": display_candidates,
        "summary": {
            "total_candidates": len(reviewed),
            "eligible_ranked_candidates": len(ranked_all),
            "displayed_candidates": len(display_candidates),
            "quality_approved": len(approved_reviewed),
            "quality_rejected": len(rejected_reviewed),
        },
    }

    write_json(FAST_SCAN_RESULTS_PATH, payload)

    write_json(FAST_SCAN_STATUS_PATH, {
        "status": "complete",
        "message": f"Fast scan complete. Ranked {len(ranked_all)} eligible candidate(s), displaying {len(display_candidates)}.",
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
