from typing import Dict, List, Tuple


MAX_POSITION_SIZE_PCT = 15
MAX_SECTOR_EXPOSURE_PCT = 30
MAX_OPEN_POSITIONS = 8
MAX_DRAWDOWN_PCT = 15
MIN_RESEARCH_CONFIDENCE = 60
MIN_QUANT_STRENGTH = 55
DAILY_LOSS_LIMIT_PCT = 5


def pass_result(reason: str) -> Tuple[bool, str]:
    return True, reason


def fail_result(reason: str) -> Tuple[bool, str]:
    return False, reason


def check_max_position_size(size_pct: float) -> Tuple[bool, str]:
    if size_pct > MAX_POSITION_SIZE_PCT:
        return fail_result(
            f"Position size {size_pct}% exceeds max allowed {MAX_POSITION_SIZE_PCT}%."
        )

    return pass_result(
        f"Position size {size_pct}% is within max allowed {MAX_POSITION_SIZE_PCT}%."
    )


def check_max_open_positions(current_positions: List[Dict]) -> Tuple[bool, str]:
    open_count = len(current_positions)

    if open_count >= MAX_OPEN_POSITIONS:
        return fail_result(
            f"Portfolio already has {open_count} open positions; max allowed is {MAX_OPEN_POSITIONS}."
        )

    return pass_result(
        f"Portfolio has {open_count} open positions; max allowed is {MAX_OPEN_POSITIONS}."
    )


def check_drawdown(current_nav: float, starting_nav: float) -> Tuple[bool, str]:
    if starting_nav <= 0:
        return fail_result("Starting NAV must be greater than zero.")

    drawdown_pct = ((starting_nav - current_nav) / starting_nav) * 100

    if drawdown_pct >= MAX_DRAWDOWN_PCT:
        return fail_result(
            f"Drawdown is {drawdown_pct:.2f}%, which exceeds max allowed {MAX_DRAWDOWN_PCT}%."
        )

    return pass_result(
        f"Drawdown is {drawdown_pct:.2f}%, which is below max allowed {MAX_DRAWDOWN_PCT}%."
    )


def check_daily_loss(current_nav: float, previous_close_nav: float) -> Tuple[bool, str]:
    if previous_close_nav <= 0:
        return fail_result("Previous close NAV must be greater than zero.")

    daily_loss_pct = ((previous_close_nav - current_nav) / previous_close_nav) * 100

    if daily_loss_pct >= DAILY_LOSS_LIMIT_PCT:
        return fail_result(
            f"Daily loss is {daily_loss_pct:.2f}%, which exceeds max allowed {DAILY_LOSS_LIMIT_PCT}%."
        )

    return pass_result(
        f"Daily loss is {daily_loss_pct:.2f}%, which is below max allowed {DAILY_LOSS_LIMIT_PCT}%."
    )


def check_minimum_agreement(research_confidence: float, quant_strength: float) -> Tuple[bool, str]:
    if research_confidence <= MIN_RESEARCH_CONFIDENCE:
        return fail_result(
            f"Research confidence {research_confidence} is not above required {MIN_RESEARCH_CONFIDENCE}."
        )

    if quant_strength <= MIN_QUANT_STRENGTH:
        return fail_result(
            f"Quant strength {quant_strength} is not above required {MIN_QUANT_STRENGTH}."
        )

    return pass_result(
        f"Research confidence {research_confidence} and quant strength {quant_strength} pass minimum agreement rules."
    )


def check_no_leverage(cash: float, proposed_trade_value: float) -> Tuple[bool, str]:
    if proposed_trade_value > cash:
        return fail_result(
            f"Proposed trade value ${proposed_trade_value:.2f} exceeds available cash ${cash:.2f}."
        )

    return pass_result(
        f"Proposed trade value ${proposed_trade_value:.2f} is covered by available cash ${cash:.2f}."
    )


def check_sector_exposure(
    sector: str,
    proposed_trade_value: float,
    current_sector_exposures: Dict[str, float],
    portfolio_nav: float,
) -> Tuple[bool, str]:
    if portfolio_nav <= 0:
        return fail_result("Portfolio NAV must be greater than zero.")

    current_sector_value = current_sector_exposures.get(sector, 0)
    new_sector_value = current_sector_value + proposed_trade_value
    new_sector_pct = (new_sector_value / portfolio_nav) * 100

    if new_sector_pct > MAX_SECTOR_EXPOSURE_PCT:
        return fail_result(
            f"{sector} exposure would become {new_sector_pct:.2f}%, exceeding max allowed {MAX_SECTOR_EXPOSURE_PCT}%."
        )

    return pass_result(
        f"{sector} exposure would become {new_sector_pct:.2f}%, within max allowed {MAX_SECTOR_EXPOSURE_PCT}%."
    )


def run_pre_trade_risk_checks(
    pm_decision: Dict,
    research_brief: Dict,
    quant_signal: Dict,
    portfolio_state: Dict,
) -> Dict:
    """
    Runs all hard risk checks before trade execution.

    Returns:
    {
        "approved": bool,
        "reasons": [str]
    }
    """

    reasons = []

    portfolio_nav = float(portfolio_state["portfolio_nav"])
    starting_nav = float(portfolio_state["starting_nav"])
    previous_close_nav = float(portfolio_state["previous_close_nav"])
    cash = float(portfolio_state["cash"])
    current_positions = portfolio_state.get("current_positions", [])
    current_sector_exposures = portfolio_state.get("sector_exposures", {})

    size_pct = float(pm_decision["size_pct"])
    sector = pm_decision.get("sector", "Unknown")

    proposed_trade_value = portfolio_nav * (size_pct / 100)

    checks = [
        check_max_position_size(size_pct),
        check_max_open_positions(current_positions),
        check_drawdown(portfolio_nav, starting_nav),
        check_daily_loss(portfolio_nav, previous_close_nav),
        check_minimum_agreement(
            float(research_brief["confidence"]),
            float(quant_signal["strength"]),
        ),
        check_no_leverage(cash, proposed_trade_value),
        check_sector_exposure(
            sector,
            proposed_trade_value,
            current_sector_exposures,
            portfolio_nav,
        ),
    ]

    approved = True

    for passed, reason in checks:
        reasons.append(reason)
        if not passed:
            approved = False

    return {
        "approved": approved,
        "reasons": reasons,
    }
