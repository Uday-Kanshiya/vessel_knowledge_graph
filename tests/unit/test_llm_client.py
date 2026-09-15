"""
LLMClient unit tests. No network: the OpenAI SDK client is replaced with a fake
that records the request and returns a canned completion.
"""

import pytest

from src.llm import LLMClient, LLMError, is_reasoning_model
from src.llm import llm_client as llm_module
from src.utils import observability
from src.utils.token_tracker import TokenTracker


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def model_dump(self):
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _completion(content, model="gpt-4o-mini", finish_reason="stop", usage=(10, 5)):
    return _Obj(
        choices=[_Obj(message=_Obj(content=content), finish_reason=finish_reason)],
        usage=_Usage(*usage) if usage else None,
        model=model,
        id="cmpl-1",
    )


class _FakeCompletions:
    def __init__(self, result):
        self.result = result
        self.params = None

    async def create(self, **params):
        self.params = params
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _client(monkeypatch, result, model="gpt-4o-mini"):
    monkeypatch.setattr(
        llm_module,
        "get_llm_config",
        lambda: {
            "provider": "openai",
            "api_key": "test-key",
            "model": model,
            "max_tokens": 1000,
            "temperature": 0.4,
            "base_url": "http://localhost",
        },
    )
    client = LLMClient()
    fake = _FakeCompletions(result)
    client.client = _Obj(chat=_Obj(completions=fake))
    return client, fake


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(llm_module, "get_llm_config", lambda: {"provider": "openai", "api_key": ""})
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        LLMClient()


async def test_messages_wrapped_and_system_prepended(monkeypatch):
    client, fake = _client(monkeypatch, _completion("hi"))
    await client.simple_completion("hello", system_message="be brief")
    assert fake.params["messages"] == [
        {"role": "system", "content": [{"type": "text", "text": "be brief"}]},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]


@pytest.mark.parametrize(
    "model, expected",
    [
        ("gpt-5-mini", True),
        ("prod-gpt-5-mini", True),  # Azure deployment name
        ("o3-mini", True),
        ("gpt-4o-mini", False),
        ("gpt-4o3", False),  # o-series is a prefix match only
    ],
)
def test_is_reasoning_model(model, expected):
    assert is_reasoning_model(model) is expected


async def test_reasoning_model_uses_max_completion_tokens_and_no_temperature(monkeypatch):
    client, fake = _client(monkeypatch, _completion("hi"), model="gpt-5-mini")
    await client.simple_completion("hello", temperature=0.9)
    assert fake.params["max_completion_tokens"] == 1000
    assert "max_tokens" not in fake.params
    assert "temperature" not in fake.params


async def test_explicit_zero_temperature_is_kept(monkeypatch):
    client, fake = _client(monkeypatch, _completion("hi"))
    await client.simple_completion("hello", temperature=0.0)
    assert fake.params["temperature"] == 0.0


async def test_per_call_model_override(monkeypatch):
    client, fake = _client(monkeypatch, _completion("hi"))
    await client.simple_completion("hello", model="gpt-5")
    assert fake.params["model"] == "gpt-5"
    assert "max_completion_tokens" in fake.params


async def test_token_tracker_records_usage_and_model(monkeypatch):
    client, _ = _client(monkeypatch, _completion("hi", model="gpt-4o-mini", usage=(120, 30)))
    tracker = TokenTracker(stage="window_update", casefile_id="cf1")
    await client.simple_completion("a", token_tracker=tracker)
    await client.simple_completion("b", token_tracker=tracker)

    totals = tracker.get_totals()
    assert totals["total_input_tokens"] == 240
    assert totals["total_output_tokens"] == 60
    assert totals["call_count"] == 2
    assert totals["by_model"]["gpt-4o-mini"]["call_count"] == 2


async def test_json_completion_parses_and_sets_response_format(monkeypatch):
    client, fake = _client(monkeypatch, _completion('{"outcome": "no_effect"}'))
    assert await client.json_completion("x") == {"outcome": "no_effect"}
    assert fake.params["response_format"] == {"type": "json_object"}


async def test_json_completion_empty_content_names_finish_reason(monkeypatch):
    client, _ = _client(monkeypatch, _completion(None, finish_reason="length"))
    with pytest.raises(LLMError, match="finish_reason='length'"):
        await client.json_completion("x")


async def test_json_completion_invalid_json(monkeypatch):
    client, _ = _client(monkeypatch, _completion("not json"))
    with pytest.raises(LLMError, match="not valid JSON"):
        await client.json_completion("x")


async def test_provider_error_is_chained(monkeypatch):
    original = TimeoutError("upstream timed out")
    client, _ = _client(monkeypatch, original)
    with pytest.raises(LLMError) as exc:
        await client.simple_completion("x")
    assert exc.value.__cause__ is original


async def test_langfuse_kwargs_never_reach_plain_sdk(monkeypatch):
    # With tracing off, `name`/`metadata` would be forwarded to the API and fail.
    monkeypatch.setattr(observability, "_openai_wrapped", False)
    client, fake = _client(monkeypatch, _completion("hi"))
    await client.simple_completion("x", token_tracker=TokenTracker(stage="consolidate"))
    assert "name" not in fake.params
    assert "metadata" not in fake.params


def test_generation_kwargs_when_wrapped(monkeypatch):
    monkeypatch.setattr(observability, "_openai_wrapped", True)
    kw = observability.generation_kwargs(TokenTracker(stage="window_update", casefile_id="cf1"))
    assert kw["name"] == "window_update"
    assert kw["metadata"]["casefile_id"] == "cf1"


def test_observability_is_noop_when_disabled():
    with observability.trace_root("casefile") as span:
        span.update(output="anything")
    observability.update_current_trace(session_id="s")
    observability.flush()
