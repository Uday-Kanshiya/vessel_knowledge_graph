"""
Live smoke test: can we reach the LLM and get JSON back?

Makes one real, billed call. Skipped unless LLM_API_KEY is set in .env.

    uv run pytest tests/integration/test_llm_hello.py -v -s
"""

import pytest

from src import config
from src.llm import LLMClient
from src.utils.token_tracker import TokenTracker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not config.LLM_API_KEY or config.LLM_API_KEY.startswith("your_"),
        reason="LLM_API_KEY not set in .env",
    ),
]


async def test_llm_says_hi_in_json():
    client = LLMClient()
    tracker = TokenTracker(stage="smoke_test")

    # json_object mode requires the word "JSON" to appear in the prompt.
    result = await client.json_completion(
        'Reply with exactly this JSON and nothing else: {"reply": "hi"}',
        system_message="You return only valid JSON.",
        token_tracker=tracker,
    )

    print(f"\nmodel:    {client.llm_config['model']}")
    print(f"response: {result}")
    print(f"tokens:   {tracker.get_totals()}")

    assert set(result) == {"reply"}
    # Case/whitespace tolerant: "Hi" is still a working connection.
    assert result["reply"].strip().lower() == "hi"
