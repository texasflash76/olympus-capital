import os
import sys
from typing import Any, Dict

from agents.research_analyst import run_research_analyst
from agents.quant_analyst import run_quant_analyst
from agents.portfolio_manager import run_portfolio_manager

from risk_engine import run_pre_trade_risk_checks
from logger import init_db, log_trade_cycle, print_recent_logs

from tools.broker import (
    get_account_summary,
    get_positions,
    submit_paper_market_order,
)

from tools.web_research import get_company_profile, get_sector_for_ticker


SECTOR_MAP = {
    # Technology
    "AAPL": "Technology",
    "MSFT": "Technology",
    "NVDA": "Technology",
    "AMD": "Technology",
    "AVGO": "Technology",
    "ORCL": "Technology",
    "CRM": "Technology",
    "ADBE": "Technology",
    "INTC": "Technology",
    "QCOM": "Technology",
    "TXN": "Technology",
    "MU": "Technology",
    "PLTR": "Technology",
    "SNOW": "Technology",
    "NET": "Technology",
    "CRWD": "Technology",
    "PANW": "Technology",
    "NOW": "Technology",
    "SHOP": "Technology",

    # Communication Services
    "GOOGL": "Communication Services",
    "GOOG": "Communication Services",
    "META": "Communication Services",
    "NFLX": "Communication Services",
    "DIS": "Communication Services",
    "CMCSA": "Communication Services",
    "T": "Communication Services",
    "VZ": "Communication Services",
    "TMUS": "Communication Services",
    "SPOT": "Communication Services",
    "PINS": "Communication Services",
    "SNAP": "Communication Services",
    "RDDT": "Communication Services",

    # Consumer Cyclical
    "AMZN": "Consumer Cyclical",
    "TSLA": "Consumer Cyclical",
    "HD": "Consumer Cyclical",
    "LOW": "Consumer Cyclical",
    "MCD": "Consumer Cyclical",
    "SBUX": "Consumer Cyclical",
    "NKE": "Consumer Cyclical",
    "BKNG": "Consumer Cyclical",
    "ABNB": "Consumer Cyclical",
    "GM": "Consumer Cyclical",
    "F": "Consumer Cyclical",
    "RIVN": "Consumer Cyclical",
    "LCID": "Consumer Cyclical",
    "BABA": "Consumer Cyclical",
    "JD": "Consumer Cyclical",

    # Consumer Defensive
    "WMT": "Consumer Defensive",
    "COST": "Consumer Defensive",
    "TGT": "Consumer Defensive",
    "PG": "Consumer Defensive",
    "KO": "Consumer Defensive",
    "PEP": "Consumer Defensive",
    "MDLZ": "Consumer Defensive",
    "CL": "Consumer Defensive",

    # Financial Services
    "JPM": "Financial Services",
    "BAC": "Financial Services",
    "WFC": "Financial Services",
    "C": "Financial Services",
    "GS": "Financial Services",
    "MS": "Financial Services",
    "V": "Financial Services",
    "MA": "Financial Services",
    "AXP": "Financial Services",
    "PYPL": "Financial Services",
    "COIN": "Financial Services",
    "HOOD": "Financial Services",
    "BRK.B": "Financial Services",
    "BRK-B": "Financial Services",

    # Healthcare
    "LLY": "Healthcare",
    "UNH": "Healthcare",
    "JNJ": "Healthcare",
    "PFE": "Healthcare",
    "MRK": "Healthcare",
    "ABBV": "Healthcare",
    "TMO": "Healthcare",
    "ABT": "Healthcare",
    "DHR": "Healthcare",
    "ISRG": "Healthcare",
    "REGN": "Healthcare",
    "VRTX": "Healthcare",
    "MRNA": "Healthcare",
    "BNTX": "Healthcare",

    # Energy
    "XOM": "Energy",
    "CVX": "Energy",
    "COP": "Energy",
    "SLB": "Energy",
    "OXY": "Energy",
    "EOG": "Energy",

    # Industrials
    "BA": "Industrials",
    "CAT": "Industrials",
    "DE": "Industrials",
    "GE": "Industrials",
    "HON": "Industrials",
    "UPS": "Industrials",
    "RTX": "Industrials",
    "LMT": "Industrials",

    # Materials
    "LIN": "Materials",
    "APD": "Materials",
    "SHW": "Materials",
    "FCX": "Materials",
    "NEM": "Materials",

    # Real Estate
    "AMT": "Real Estate",
    "PLD": "Real Estate",
    "SPG": "Real Estate",
    "O": "Real Estate",

    # Utilities
    "NEE": "Utilities",
    "DUK": "Utilities",
    "SO": "Utilities",
}


def to_dict(obj: Any) -> Dict:
    """
    Converts Pydantic models or normal dicts into plain Python dictionaries.
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump()

    if isinstance(obj, dict):
        return obj

    raise TypeError(f"Cannot convert object to dict: {type(obj)}")


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def get_sector(ticker: str) -> str:
    """
    Dynamically identifies a company's sector.

    Priority:
    1. Web-derived company profile from tools.web_research
    2. Old SECTOR_MAP fallback
    3. Unknown

    This means the bot is no longer dependent on manually adding every ticker.
    """
    ticker = ticker.upper().strip()

    try:
        sector = get_sector_for_ticker(ticker)
        if sector and sector != "Unknown":
            return sector
    except Exception as e:
        print(f"Dynamic sector lookup failed for {ticker}: {e}")

    return SECTOR_MAP.get(ticker, "Unknown")


def build_portfolio_state() -> Dict:
    """
    Builds current portfolio state from Alpaca paper account data.

    Important:
    Unknown-sector positions are NOT added to sector_exposures.
    This prevents the sector risk rule from blocking the bot just because
    a ticker is not in SECTOR_MAP.
    """
    account = get_account_summary()
    positions = get_positions()

    portfolio_nav = safe_float(account.get("portfolio_value", 0))
    cash = safe_float(account.get("cash", 0))

    sector_exposures = {}

    for position in positions:
        symbol = position.get("symbol", "")
        market_value = safe_float(position.get("market_value", 0))
        sector = get_sector(symbol)

        if sector == "Unknown":
            continue

        if sector not in sector_exposures:
            sector_exposures[sector] = 0.0

        sector_exposures[sector] += market_value

    return {
        "portfolio_nav": portfolio_nav,
        "starting_nav": portfolio_nav,
        "previous_close_nav": portfolio_nav,
        "cash": cash,
        "current_positions": positions,
        "sector_exposures": sector_exposures,
    }


def run_one_ticker(ticker, allow_execution=True):   
    """
    Full trading loop for one ticker:

    Research Analyst
    -> Quant Analyst
    -> Portfolio Manager
    -> Risk Engine
    -> Optional Alpaca paper execution
    -> SQLite log
    """
    ticker = ticker.strip().upper()
    company_profile = get_company_profile(ticker)
    sector = company_profile.get("sector") or get_sector(ticker)
    industry = company_profile.get("industry", "Unknown")
    company_name = company_profile.get("company_name", "")

    print(f"\n=== RUNNING OLYMPUS CAPITAL LOOP FOR {ticker} ===")
    print(f"Company: {company_name or ticker}")
    print(f"Sector: {sector}")
    print(f"Industry: {industry}")

    if sector == "Unknown":
        print(f"Sector warning: {ticker} sector could not be found dynamically. Sector risk check will use Unclassified.")

    print("\n--- RESEARCH ANALYST PHASE ---")
    research_brief = run_research_analyst(ticker)
    research_brief_dict = to_dict(research_brief)

    research_brief_dict["company_profile"] = company_profile
    research_brief_dict["sector"] = sector
    research_brief_dict["industry"] = industry

    print("\n--- QUANT ANALYST PHASE ---")
    quant_signal = run_quant_analyst(ticker)
    quant_signal_dict = to_dict(quant_signal)

    print("\n--- PORTFOLIO MANAGER PHASE ---")
    pm_decision = run_portfolio_manager(research_brief, quant_signal)
    pm_decision_dict = to_dict(pm_decision)

    # Add sector metadata for risk engine.
    # If unknown, use a harmless temporary sector bucket with zero exposure.
    if sector == "Unknown":
        pm_decision_dict["sector"] = "Unclassified"
    else:
        pm_decision_dict["sector"] = sector

    print("\n--- PORTFOLIO STATE PHASE ---")
    portfolio_state = build_portfolio_state()

    # Prevent unknown tickers from failing sector exposure because of missing sector info.
    if sector == "Unknown":
        portfolio_state["sector_exposures"]["Unclassified"] = 0.0

    print("\n--- RISK ENGINE PHASE ---")
    risk_result = run_pre_trade_risk_checks(
        pm_decision=pm_decision_dict,
        research_brief=research_brief_dict,
        quant_signal=quant_signal_dict,
        portfolio_state=portfolio_state,
    )

    print("\n--- EXECUTION PHASE ---")

    execution_result = None

    if pm_decision_dict["decision"] == "EXECUTE" and risk_result["approved"]:
        target_trade_size_pct = float(os.getenv("TARGET_TRADE_SIZE_PCT", "1"))

        proposed_trade_value = portfolio_state["portfolio_nav"] * (
            target_trade_size_pct / 100
)

        pm_decision_dict["ai_suggested_size_pct"] = pm_decision_dict.get("size_pct")
        pm_decision_dict["size_pct"] = target_trade_size_pct
        if not allow_execution:
            final_status = "RECOMMENDED_NOT_EXECUTED"
            execution_result = {
                "mode": "recommendation_only",
                "message": "Trade was approved by PM and risk engine, but execution was disabled for this run.",
                "proposed_trade_value": proposed_trade_value,
            }

        else:
            try:
                execution_result = submit_paper_market_order(
                    ticker=ticker,
                    direction=pm_decision_dict["direction"],
                    notional_value=proposed_trade_value,
                )
                final_status = "PAPER_TRADE_SUBMITTED"

            except Exception as e:
                execution_result = {"error": str(e)}
                final_status = "TRADE_APPROVED_BUT_EXECUTION_BLOCKED"

    elif pm_decision_dict["decision"] == "VETO":
        final_status = "TRADE_VETOED_BY_PM"
        execution_result = None

    else:
        final_status = "TRADE_BLOCKED_BY_RISK_ENGINE"
        execution_result = None

    print(f"Final status: {final_status}")
    print(f"Execution result: {execution_result}")

    print("\n--- LOGGING PHASE ---")
    log_trade_cycle(
        ticker=ticker,
        research_brief=research_brief_dict,
        quant_signal=quant_signal_dict,
        pm_decision=pm_decision_dict,
        risk_result=risk_result,
        final_status=final_status,
    )

    result = {
        "ticker": ticker,
        "company_profile": company_profile,
        "sector": sector,
        "industry": industry,
        "research_brief": research_brief_dict,
        "quant_signal": quant_signal_dict,
        "pm_decision": pm_decision_dict,
        "risk_result": risk_result,
        "portfolio_state": portfolio_state,
        "execution_result": execution_result,
        "final_status": final_status,
    }

    print("\n=== LOOP COMPLETE ===")
    print(f"Ticker: {ticker}")
    print(f"Sector: {sector}")
    print(f"Final Status: {final_status}")

    return result

def main():
    init_db()

    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "NVDA"

    try:
        run_one_ticker(ticker)
    except Exception as e:
        print("\nPHASE 5 LOOP FAILED")
        print(str(e))
        raise

    print("\n--- RECENT LOGS ---")
    print_recent_logs(limit=5)


if __name__ == "__main__":
    main()
