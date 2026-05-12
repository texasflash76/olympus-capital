import json

from agents.research_analyst import run_research_analyst
from agents.quant_analyst import run_quant_analyst
from agents.portfolio_manager import run_portfolio_manager


def print_validated_output(title, output):
    print(f"\n=== {title} ===")

    if hasattr(output, "model_dump"):
        print(json.dumps(output.model_dump(), indent=2))
    else:
        print(json.dumps(output, indent=2))


def main():
    ticker = "NVDA"

    print(f"\nRunning Research Analyst for {ticker}...\n")
    research_brief = run_research_analyst(ticker)
    print_validated_output("VALID RESEARCH BRIEF", research_brief)

    print(f"\nRunning Quant Analyst for {ticker}...\n")
    quant_signal = run_quant_analyst(ticker)
    print_validated_output("VALID QUANT SIGNAL", quant_signal)

    print(f"\nRunning Portfolio Manager for {ticker}...\n")
    pm_decision = run_portfolio_manager(research_brief, quant_signal)
    print_validated_output("VALID PM DECISION", pm_decision)


if __name__ == "__main__":
    main()
