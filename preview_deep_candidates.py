from deep_review import select_candidates_for_deep_review
import json

selection = select_candidates_for_deep_review(
    max_candidates=15,
    only_quality_approved=True,
    include_previous_errors=True,
)

print("\nSELECTED TICKERS:")
print(selection.get("selected_tickers"))

print("\nDETAILS:")
for item in selection.get("selected", []):
    print(
        item.get("ticker"),
        "| score:",
        item.get("_deep_review_selection_score"),
        "| debug:",
        json.dumps(item.get("_ranking_debug", {}))
    )
