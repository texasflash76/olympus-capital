import json


def call_llm_manual(prompt: str) -> dict:
    """
    Manual ChatGPT/Codex mode.

    This does NOT use API credits.
    It prints the prompt, then you paste the prompt into ChatGPT/Codex,
    copy the JSON response, and paste it back into terminal.
    """

    print("\n" + "=" * 80)
    print("COPY THIS PROMPT INTO CHATGPT / CODEX")
    print("=" * 80)
    print(prompt)
    print("=" * 80)

    print("\nPaste the JSON response from ChatGPT/Codex below.")
    print("It must be one valid JSON object on one line.\n")

    raw_response = input("JSON response: ").strip()

    try:
        return json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON returned by LLM: {e}")
