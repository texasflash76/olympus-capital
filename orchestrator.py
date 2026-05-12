import json

from agents.research_analyst import run_research_analyst
from agents.quant_analyst import run_quant_analyst
from agents.portfolio_manager import run_portfolio_manager

from risk_engine import run_pre_trade_risk_checks
from logger import log_trade_cycle, print_recent_logs
from tools.broker import get_account_summary, get_positions


SECTOR_MAP = {
    "NVDA": "Technology",
    "TSLA": "Consumer Cyclical",
    "AAPL": "Technology",
    "MSFT": "Technology",
    "AMZN": "Consumer Cyclical",
    "GOOGL": "Communication Services",
    "META": "Communication Services"
}


def to_dict(obj):
    """
    Converts Pydantic models into normal dictionaries.
    If already a dict, returns it unchanged.
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def build_portfolio_state():
    """
    Builds the portfolio state needed by the Phase 4 risk engine.

    For now:
    - Reads account summary from Alpaca paper account.
    - Reads current positions from Alpaca paper account.
    - Uses a temporary/manual sector exposure dictionary.
    """

    account = get_account_summary()
    positions = get_positions()

    portfolio_nav = float(account["portfolio_value"])
    cash = float(account["cash"])

    # For now, use current NAV as starting/previous close NAV.
    # Later, this should come from saved daily portfolio logs.
    starting_nav = portfolio_nav
    previous_close_nav = portfolio_nav

    portfolio_state = {
        "portfolio_nav": portfolio_nav,
        "starting_nav": starting_nav,
        "previous_close_nav": previous_close_nav,
        "cash": cash,
        "current_positions": positions,
        "sector_exposures": {
            "Technology": 0,
            "Consumer Cyclical": 0,
            "Communication Services": 0
        }
    }

    return portfolio_state


def run_one_ticker(ticker):
    print("\n==============================")
    print(f"STARTING FULL PHASE 5 LOOP FOR {ticker}")
    print("==============================")

    try:
        print("\n--- RESEARCH PHASE ---")
        research_brief = run_research_analyst(ticker)
        research_dict = to_dict(research_brief)

        print("\nVALID RESEARCH BRIEF:")
        print(json.dumps(research_dict, indent=2))

        print("\n--- QUANT PHASE ---")
        quant_signal = run_quant_analyst(ticker)
        quant_dict = to_dict(quant_signal)

        print("\nVALID QUANT SIGNAL:")
        print(json.dumps(quant_dict, indent=2))

        print("\n--- PORTFOLIO MANAGER PHASE ---")
        pm_decision = run_portfolio_manager(research_brief, quant_signal)
        pm_dict = to_dict(pm_decision)

        # The PM schema does not include sector, but the risk engine needs it.
        # So the orchestrator injects sector metadata before risk checks.
        pm_dict["sector"] = SECTOR_MAP.get(ticker, "Unknown")

        print("\nVALID PM DECISION:")
        print(json.dumps(pm_dict, indent=2))

        print("\n--- PORTFOLIO STATE PHASE ---")
        portfolio_state = build_portfolio_state()

        print("\nPORTFOLIO STATE:")
        print(json.dumps(portfolio_state, indent=2))

        print("\n--- RISK ENGINE PHASE ---")
        risk_result = run_pre_trade_risk_checks(
            pm_decision=pm_dict,
            research_brief=research_dict,
            quant_signal=quant_dict,
            portfolio_state=portfolio_state
        )

        print("\nRISK RESULT:")
        print(json.dumps(risk_result, indent=2))

        print("\n--- FINAL DECISION PHASE ---")

        if pm_dict["decision"] == "EXECUTE" and risk_result["approved"] is True:
            final_status = "TRADE_APPROVED_SIMULATION_ONLY"
            print("TRADE APPROVED — simulation only. No order placed yet.")

        elif pm_dict["decision"] == "VETO":
            final_status = "TRADE_VETOED_BY_PM"
            print("TRADE BLOCKED — Portfolio Manager vetoed it.")

        else:
            final_status = "TRADE_BLOCKED_BY_RISK_ENGINE"
            print("TRADE BLOCKED — Risk engine rejected it.")
            for reason in risk_result["reasons"]:
                print(f"- {reason}")

        print("\n--- LOGGING PHASE ---")
        log_trade_cycle(
            ticker=ticker,
            research_brief=research_dict,
            quant_signal=quant_dict,
            pm_decision=pm_dict,
            risk_result=risk_result,
            final_status=final_status
        )

        print("Cycle logged to olympus_audit_log.db")

        return {
            "ticker": ticker,
            "research_brief": research_dict,
            "quant_signal": quant_dict,
            "pm_decision": pm_dict,
            "risk_result": risk_result,
            "final_status": final_status
        }

    except Exception as e:
        print("\nPHASE 5 LOOP FAILED")
        print(str(e))

        error_result = {
            "approved": False,
            "reasons": [f"Orchestrator error: {str(e)}"]
        }

        log_trade_cycle(
            ticker=ticker,
            research_brief={},
            quant_signal={},
            pm_decision={},
            risk_result=error_result,
            final_status="ORCHESTRATOR_ERROR"
        )

        raise


def main():
    ticker = "NVDA"

    result = run_one_ticker(ticker)

    print("\n==============================")
    print("PHASE 5 LOOP COMPLETE")
    print("==============================")

    print("\nFINAL RESULT:")
    print(json.dumps(result, indent=2))

    print("\nRECENT SQLITE LOGS:")
    print_recent_logs(limit=3)


if __name__ == "__main__":
    main()
