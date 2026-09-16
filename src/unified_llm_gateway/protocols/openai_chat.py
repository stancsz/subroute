from __future__ import annotations

from typing import Any

from litellm.types.utils import ModelResponse

from ..kernel import CompletionRequest


class OpenAIChatProtocol:
    """Minimal non-streaming OpenAI Chat Completions boundary."""

    @staticmethod
    def to_request(payload: dict[str, Any]) -> CompletionRequest:
        allowed = {
            "model", "messages", "temperature", "max_tokens",
            "max_completion_tokens", "timeout",
        }
        if set(payload) - allowed:
            raise ValueError("Unsupported OpenAI Chat fields")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages or not all(
            isinstance(item, dict)
            and set(item) == {"role", "content"}
            and item.get("role") in {"system", "developer", "user", "assistant"}
            and isinstance(item.get("content"), str)
            and bool(item["content"])
            for item in messages
        ):
            raise ValueError("OpenAI Chat messages must be plain text")
        return CompletionRequest(
            model=payload.get("model"),
            messages=messages,
            temperature=payload.get("temperature"),
            max_tokens=payload.get("max_tokens", payload.get("max_completion_tokens")),
            timeout=payload.get("timeout"),
        )

    @staticmethod
    def from_response(response: ModelResponse) -> dict[str, Any]:
        if len(response.choices) != 1:
            raise ValueError("OpenAI Chat requires one choice")
        choice = response.choices[0]
        if choice.message.role != "assistant" or not isinstance(choice.message.content, str):
            raise ValueError("OpenAI Chat response must contain assistant text")
        return response.model_dump(exclude_none=True)
