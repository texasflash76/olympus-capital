from schemas import validate_pm_decision
from agents.llm_client import call_llm_manual


PORTFOLIO_MANAGER_SYSTEM_PROMPT = """
You are the Portfolio Manager of a small quantitative hedge fund.

You receive:
1. A Research Brief
2. A Quant Signal

Your job is to synthesize both and make a final trade decision.

Important rules:
- You are the final decision-maker.
- You must decide either EXECUTE or VETO.
- You must explain your reasoning clearly.
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include explanations outside the JSON.

Decision framework:
1. Research sentiment and Quant direction should agree:
   - bullish + long = agreement
   - bearish + short = agreement
   - neutral + anything = weak/no agreement
   - bullish + short = conflict
   - bearish + long = conflict

2. Combined conviction should be strong enough:
   - research confidence + quant strength should usually be above 130 for EXECUTE
   - if below 130, usually VETO

3. Position sizing must be reasonable:
   - size_pct must never exceed 15
   - if the signal is weak, size should be small

4. If there is conflict or weak evidence, VETO.

Return JSON matching this exact schema:

{
  "ticker": "NVDA",
  "decision": "EXECUTE" | "VETO",
  "direction": "long" | "short" | "flat",
  "size_pct": integer from 1 to 15,
  "reasoning": "plain English explanation, at least 30 characters",
  "risk_flags": ["string"]
}

Rules:
- If decision is VETO, direction can still show the proposed direction, but size_pct should usually be 1.
- If Quant direction is flat, decision should be VETO.
- Never invent new data.
- Base your decision only on the Research Brief and Quant Signal provided.
- Be specific. Do not just say "signals align." Explain which signals align or conflict.
"""


def build_pm_prompt(research_brief, quant_signal) -> str:
    if hasattr(research_brief, "model_dump"):
        research_brief = research_brief.model_dump()

    if hasattr(quant_signal, "model_dump"):
        quant_signal = quant_signal.model_dump()

    prompt = f"""
{PORTFOLIO_MANAGER_SYSTEM_PROMPT}

Research Brief:
{research_brief}

Quant Signal:
{quant_signal}

Now produce the Portfolio Manager Decision JSON.
"""

    return prompt


def run_portfolio_manager(research_brief, quant_signal):
    """
    Runs the Portfolio Manager agent.

    Current Phase 3 version:
    - Receives validated Research Brief
    - Receives validated Quant Signal
    - Builds a strict JSON prompt
    - Lets you manually call ChatGPT/Codex
    - Validates the response using schemas.py
    """

    prompt = build_pm_prompt(research_brief, quant_signal)

    raw_output = call_llm_manual(prompt)

    validated_output = validate_pm_decision(raw_output)

    return validated_output
