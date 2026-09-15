"""
Async LLM client — OpenAI or Azure OpenAI, with optional token tracking and
optional Langfuse tracing.

Ported from qhsc_mail_process/src/llm/llm_client.py with the same public
surface (chat_completion / json_completion / simple_completion /
test_connection), so code and habits move between the two repos. Where the
behaviour differs, it is a fix, and the line says why.

Langfuse is touched on exactly the lines marked `# observability:`. To cut it
out, follow the steps at the top of src/utils/observability.py.
"""

import json
import logging
from typing import Any, Optional

from src.config import get_llm_config
from src.utils.observability import generation_kwargs, openai_client_classes  # observability:
from src.utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)

AsyncOpenAI, AsyncAzureOpenAI = openai_client_classes()  # observability:


class LLMError(Exception):
    """A failed provider call, or output that could not be used."""


def is_reasoning_model(model: str) -> bool:
    """
    Reasoning models reject `temperature` and take `max_completion_tokens`
    instead of `max_tokens`.

    Substring match for gpt-5/gpt-6 so Azure deployment names like
    "prod-gpt-5-mini" still match; prefix match for the o-series, where a
    substring would hit unrelated names.
    """
    name = model.lower()
    return "gpt-5" in name or "gpt-6" in name or name.startswith(("o1", "o3", "o4"))


class LLMClient:
    """Async chat-completions client for the configured provider."""

    def __init__(self, model: Optional[str] = None):
        """
        Args:
            model: override LLM_MODEL for every call on this client. Pipeline
                stages use different models (docs/02 §2.2), so each stage
                builds its own client rather than passing model= on every call.
        """
        self.llm_config = get_llm_config()
        if model:
            self.llm_config["model"] = model
        if not self.llm_config["api_key"]:
            raise LLMError("LLM_API_KEY is not set")

        if self.llm_config["provider"] == "azure":
            self.client = AsyncAzureOpenAI(
                azure_endpoint=self.llm_config["azure_endpoint"],
                api_key=self.llm_config["api_key"],
                api_version=self.llm_config["azure_api_version"],
            )
        else:
            self.client = AsyncOpenAI(
                base_url=self.llm_config["base_url"],
                api_key=self.llm_config["api_key"],
            )

    @staticmethod
    def _prepare_messages(
        messages: list[dict[str, Any]], system_message: Optional[str]
    ) -> list[dict[str, Any]]:
        """Prepend the system message; wrap plain-string content as a text part."""
        prepared = []
        if system_message:
            prepared.append(
                {"role": "system", "content": [{"type": "text", "text": system_message}]}
            )
        for message in messages:
            if isinstance(message["content"], str):
                prepared.append(
                    {
                        "role": message["role"],
                        "content": [{"type": "text", "text": message["content"]}],
                    }
                )
            else:
                prepared.append(message)
        return prepared

    def _request_params(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        response_format: Optional[dict[str, str]],
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"model": model, "messages": messages}

        # `is not None`, not `or`: qhse used `temperature or default`, which
        # silently turns an explicit temperature=0.0 into the default.
        tokens = max_tokens if max_tokens is not None else self.llm_config["max_tokens"]
        if is_reasoning_model(model):
            params["max_completion_tokens"] = tokens
        else:
            params["max_tokens"] = tokens
            params["temperature"] = (
                temperature if temperature is not None else self.llm_config["temperature"]
            )

        if response_format:
            params["response_format"] = response_format
        return params

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        system_message: Optional[str] = None,
        response_format: Optional[dict[str, str]] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        token_tracker: Optional[TokenTracker] = None,
        **kwargs,
    ) -> dict[str, Any]:
        """
        Send a chat completion request.

        Args:
            messages: [{"role": ..., "content": str | list}, ...]
            system_message: prepended as the system turn
            response_format: e.g. {"type": "json_object"}
            max_tokens: per-call override of LLM_MAX_TOKENS
            temperature: per-call override; ignored for reasoning models
            token_tracker: accumulates usage for this unit of work
            **kwargs: passed to the API; `model=` overrides the client's model

        Returns:
            {"content", "finish_reason", "usage", "model", "id"}

        Raises:
            LLMError: the provider call failed. The original exception is
                chained as __cause__, so retry logic can inspect its type.
        """
        model = kwargs.pop("model", None) or self.llm_config["model"]
        params = self._request_params(
            model,
            self._prepare_messages(messages, system_message),
            max_tokens,
            temperature,
            response_format,
        )
        params.update(kwargs)
        params.update(generation_kwargs(token_tracker))  # observability:

        try:
            completion = await self.client.chat.completions.create(**params)
        except Exception as e:
            # Chained, not stringified: qhse raised a bare Exception(str(e)),
            # which loses the type (rate limit vs auth vs timeout) that a
            # backlog run needs to decide whether to retry.
            raise LLMError(f"LLM call failed ({model}): {e}") from e

        choice = completion.choices[0]
        usage = completion.usage
        if token_tracker is not None and usage is not None:
            token_tracker.add_call(
                usage.prompt_tokens or 0, usage.completion_tokens or 0, model=completion.model
            )

        return {
            "content": choice.message.content,
            "finish_reason": choice.finish_reason,
            "usage": usage.model_dump() if usage else None,
            "model": completion.model,
            "id": completion.id,
        }

    async def json_completion(
        self,
        prompt: str,
        system_message: Optional[str] = None,
        token_tracker: Optional[TokenTracker] = None,
        **kwargs,
    ) -> dict[str, Any]:
        """
        Get a JSON object response.

        Raises:
            LLMError: the call failed, the response was empty, or it was not JSON.
        """
        response = await self.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            system_message=system_message,
            response_format={"type": "json_object"},
            token_tracker=token_tracker,
            **kwargs,
        )

        content = response["content"]
        if not content:
            # A reasoning model that spends its whole budget thinking returns
            # content=None with finish_reason="length". qhse passed that None
            # to json.loads and surfaced an unrelated TypeError.
            raise LLMError(
                f"Empty response from {response['model']} "
                f"(finish_reason={response['finish_reason']!r})"
            )
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"Response was not valid JSON: {e}") from e

    async def simple_completion(
        self,
        prompt: str,
        system_message: Optional[str] = None,
        token_tracker: Optional[TokenTracker] = None,
        **kwargs,
    ) -> str:
        """Get a plain text response. Empty string if the model returned no content."""
        response = await self.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            system_message=system_message,
            token_tracker=token_tracker,
            **kwargs,
        )
        return response["content"] or ""

    async def test_connection(self) -> bool:
        """True if the provider answers. Never raises."""
        try:
            response = await self.simple_completion(
                "Hello! Please respond with 'Connection successful.'",
                system_message="You are a helpful assistant. Respond briefly and directly.",
            )
            return "connection successful" in response.lower()
        except Exception as e:
            logger.warning("LLM connection test failed: %s", e)
            return False
