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


class QuantSignal(BaseModel):
    ticker: str
    direction: Literal["long", "short", "flat"]
    strength: int = Field(ge=0, le=100)
    entry_price: float
    stop_loss: float
    size_pct: int = Field(ge=1, le=15)
    primary_signal: str
    confirming_signals: list[str]


class PMDecision(BaseModel):
    ticker: str
    decision: Literal["EXECUTE", "VETO"]
    direction: Literal["long", "short"]
    size_pct: int = Field(ge=1, le=15)
    reasoning: str = Field(min_length=30)
    risk_flags: list[str]


def validate_research_brief(data: dict) -> ResearchBrief:
    return ResearchBrief.model_validate(data)


def validate_quant_signal(data: dict) -> QuantSignal:
    return QuantSignal.model_validate(data)


def validate_pm_decision(data: dict) -> PMDecision:
    return PMDecision.model_validate(data)


if __name__ == "__main__":
    valid_research = {
        "ticker": "NVDA",
        "sentiment": "bullish",
        "confidence": 78,
        "time_horizon": "1w",
        "catalysts": ["Strong AI chip demand", "Positive earnings momentum"],
        "risks": ["High valuation", "Export restrictions"],
        "summary": "NVDA has strong near-term momentum from AI demand, but valuation remains a key risk."
    }

    malformed_research = {
        "ticker": "NVDA",
        "sentiment": "very bullish",
        "confidence": 140,
        "time_horizon": "2y",
        "catalysts": "AI demand",
        "risks": [],
        "summary": "Bad schema example."
    }

    print("Testing valid Research Brief...")
    print(validate_research_brief(valid_research))

    print("\nTesting malformed Research Brief...")
    try:
        validate_research_brief(malformed_research)
    except ValidationError as e:
        print("Malformed Research Brief correctly rejected.")
        print(e)
