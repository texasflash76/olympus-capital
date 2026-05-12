from risk_engine import run_pre_trade_risk_checks, check_max_position_size


def test_position_size_block():
    passed, reason = check_max_position_size(16)

    print("=== TEST 1: MAX POSITION SIZE ===")
    print("Passed:", passed)
    print("Reason:", reason)

    assert passed is False


def test_full_risk_engine_blocks_bad_trade():
    pm_decision = {
        "ticker": "NVDA",
        "decision": "EXECUTE",
        "direction": "long",
        "size_pct": 16,
        "sector": "Technology",
        "reasoning": "Research and quant both support the trade.",
        "risk_flags": [],
    }

    research_brief = {
        "ticker": "NVDA",
        "sentiment": "bullish",
        "confidence": 75,
        "time_horizon": "1w",
        "catalysts": ["AI demand"],
        "risks": ["Valuation risk"],
        "summary": "NVDA has strong AI demand but valuation risk remains.",
    }

    quant_signal = {
        "ticker": "NVDA",
        "direction": "long",
        "strength": 65,
        "entry_price": 218.38,
        "stop_loss": 207.46,
        "size_pct": 16,
        "primary_signal": "MACD supports a long signal.",
        "confirming_signals": ["RSI is elevated but not extreme."],
    }

    portfolio_state = {
        "portfolio_nav": 3000,
        "starting_nav": 3000,
        "previous_close_nav": 3000,
        "cash": 3000,
        "current_positions": [],
        "sector_exposures": {
            "Technology": 200,
        },
    }

    result = run_pre_trade_risk_checks(
        pm_decision=pm_decision,
        research_brief=research_brief,
        quant_signal=quant_signal,
        portfolio_state=portfolio_state,
    )

    print("\n=== TEST 2: FULL RISK ENGINE ===")
    print("Approved:", result["approved"])

    for reason in result["reasons"]:
        print("-", reason)

    assert result["approved"] is False


if __name__ == "__main__":
    test_position_size_block()
    test_full_risk_engine_blocks_bad_trade()

    print("\nPhase 4 checkpoint passed: risk engine blocks oversized positions.")
