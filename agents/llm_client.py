import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv


LLM_DEBUG_DIR = Path("llm_debug")
LLM_DEBUG_DIR.mkdir(exist_ok=True)


def now_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def get_llm_mode():
    load_dotenv()
    return os.getenv("LLM_MODE", "manual").lower().strip()


def write_debug_file(name, content):
    path = LLM_DEBUG_DIR / name
    path.write_text(str(content))
    return path


def extract_json(text: str) -> dict:
    text = str(text or "").strip()

    if not text:
        raise ValueError("LLM output was empty.")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No valid JSON object found in LLM output:\n{text[:2000]}")

    possible_json = text[start:end + 1]

    try:
        return json.loads(possible_json)
    except json.JSONDecodeError as e:
        raise ValueError(
            "Found JSON-looking text, but it could not be parsed.\n"
            f"JSON error: {e}\n"
            f"Extracted text:\n{possible_json[:2000]}"
        )


def call_llm_manual(prompt: str) -> dict:
    print("\n=== COPY THIS PROMPT INTO CHATGPT/CODEX ===\n")
    print(prompt)
    print("\n=== PASTE ONE-LINE JSON RESPONSE BELOW ===\n")

    raw = input("> ")
    return extract_json(raw)


def call_llm_codex(prompt: str) -> dict:
    strict_prompt = f"""
Return ONLY valid JSON.
No markdown.
No explanation.
No code fences.

{prompt}
"""

    stamp = now_stamp()
    write_debug_file(f"{stamp}_prompt.txt", strict_prompt)

    try:
        result = subprocess.run(
            ["codex", "exec", strict_prompt],
            capture_output=True,
            text=True,
            timeout=360,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "Codex CLI was not found. Either install/login to Codex CLI, "
            "or set LLM_MODE=manual in .env."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Codex CLI timed out after 360 seconds. Try fewer candidates, "
            "or test one ticker manually with --tickers AAPL."
        )

    write_debug_file(f"{stamp}_stdout.txt", result.stdout)
    write_debug_file(f"{stamp}_stderr.txt", result.stderr)
    write_debug_file(f"{stamp}_returncode.txt", result.returncode)

    if result.returncode != 0:
        raise RuntimeError(
            "Codex CLI failed.\n"
            f"Return code: {result.returncode}\n"
            f"STDOUT saved to llm_debug/{stamp}_stdout.txt\n"
            f"STDERR saved to llm_debug/{stamp}_stderr.txt\n"
            f"STDERR preview:\n{result.stderr[:2000]}"
        )

    try:
        return extract_json(result.stdout)
    except Exception as e:
        raise RuntimeError(
            "Codex returned output, but it was not valid JSON.\n"
            f"Prompt saved to llm_debug/{stamp}_prompt.txt\n"
            f"STDOUT saved to llm_debug/{stamp}_stdout.txt\n"
            f"STDERR saved to llm_debug/{stamp}_stderr.txt\n"
            f"Parse error: {e}"
        )


def call_llm(prompt: str) -> dict:
    mode = get_llm_mode()

    if mode == "codex":
        return call_llm_codex(prompt)

    if mode == "manual":
        return call_llm_manual(prompt)

    raise ValueError(f"Unknown LLM_MODE: {mode}. Use LLM_MODE=codex or LLM_MODE=manual.")
