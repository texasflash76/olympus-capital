from typing import Literal
from pydantic import BaseModel, Field, ValidationError


class ResearchBrief(BaseModel):
    ticker: str
    sentiment: Literal["bullish", "bearish", "neutral"]
    confidence: int = Field(ge=0, le=100)
    time_horizon: Literal["1d", "1w", "1m"]
    catalysts: list[str]
    risks: list[str]
    summary: str = Field(max_length=600)
    research_sources: list[str] = []


class QuantSignal(BaseModel):
    ticker: str
    direction: Literal["long", "short", "flat"]
    strength: int = Field(ge=0, le=100)
    entry_price: float
    stop_loss: float
    # Ceiling lowered from 15 → 10 to match risk_engine.MAX_POSITION_SIZE_PCT
    size_pct: int = Field(ge=1, le=10)
    primary_signal: str
    confirming_signals: list[str]


class PMDecision(BaseModel):
    ticker: str
    decision: Literal["EXECUTE", "VETO"]
    direction: Literal["long", "short", "flat"]
    # Ceiling lowered from 15 → 10 to match risk_engine.MAX_POSITION_SIZE_PCT
    # Tiered sizing: 3% (speculative) / 5% (standard) / 7% (high-conviction)
    size_pct: int = Field(ge=1, le=10)
    reasoning: str = Field(min_length=30)
    risk_flags: list[str]


def truncate_text(value, max_chars):
    value = str(value or "").strip()

    if len(value) <= max_chars:
        return value

    return value[: max_chars - 3].rstrip() + "..."


def clean_string_list(value, max_items=6, max_chars=160):
    if value is None:
        return []

    if isinstance(value, str):
        value = [value]

    if not isinstance(value, list):
        value = [str(value)]

    cleaned = []

    for item in value:
        item = truncate_text(item, max_chars)

        if item:
            cleaned.append(item)

        if len(cleaned) >= max_items:
            break

    return cleaned


def sanitize_research_brief(data: dict) -> dict:
    data = dict(data or {})

    data["ticker"] = str(data.get("ticker", "")).upper().strip()
    data["summary"] = truncate_text(data.get("summary", ""), 600)
    data["catalysts"] = clean_string_list(data.get("catalysts"), max_items=6, max_chars=160)
    data["risks"] = clean_string_list(data.get("risks"), max_items=6, max_chars=160)
    data["research_sources"] = clean_string_list(data.get("research_sources", []), max_items=8, max_chars=220)

    sentiment = str(data.get("sentiment", "neutral")).lower().strip()
    if sentiment not in ["bullish", "bearish", "neutral"]:
        sentiment = "neutral"
    data["sentiment"] = sentiment

    horizon = str(data.get("time_horizon", "1w")).lower().strip()
    if horizon not in ["1d", "1w", "1m"]:
        horizon = "1w"
    data["time_horizon"] = horizon

    try:
        confidence = int(float(data.get("confidence", 50)))
    except Exception:
        confidence = 50

    data["confidence"] = max(0, min(100, confidence))

    return data


def validate_research_brief(data: dict) -> ResearchBrief:
    return ResearchBrief.model_validate(sanitize_research_brief(data))


def validate_quant_signal(data: dict) -> QuantSignal:
    return QuantSignal.model_validate(data)


def validate_pm_decision(data: dict) -> PMDecision:
    return PMDecision.model_validate(data)


if __name__ == "__main__":
    valid_research = {
        "ticker": "NVDA",
        "sentiment": "bullish",
        "confidence": 65,
        "time_horizon": "1w",
        "catalysts": ["Strong AI chip demand", "Positive earnings momentum"],
        "risks": ["High valuation", "Export restrictions"],
        "summary": "NVDA has strong near-term momentum from AI demand, but valuation remains a key risk.",
    }

    malformed_research = {
        "ticker": "NVDA",
        "sentiment": "very bullish",
        "confidence": 140,
        "time_horizon": "2y",
        "catalysts": "AI demand",
        "risks": [],
        "summary": "Bad schema example.",
    }

    print("Testing valid Research Brief...")
    print(validate_research_brief(valid_research))

    print("\nTesting malformed Research Brief...")
    try:
        validate_research_brief(malformed_research)
    except ValidationError as e:
        print("Malformed Research Brief correctly rejected.")
        print(e)
