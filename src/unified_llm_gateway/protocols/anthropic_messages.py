from __future__ import annotations

from typing import Any

from litellm.types.utils import ModelResponse

from ..kernel import CompletionRequest


def _system_text(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    raise ValueError("Anthropic system must be plain text")


class AnthropicMessagesProtocol:
    """Minimal non-streaming, text-only Anthropic Messages boundary."""

    @staticmethod
    def to_request(payload: dict[str, Any]) -> CompletionRequest:
        allowed = {"model", "system", "messages", "temperature", "max_tokens", "timeout"}
        if set(payload) - allowed:
            raise ValueError("Unsupported Anthropic Messages fields")
        if payload.get("max_tokens") is None:
            raise ValueError("Anthropic max_tokens is required")
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages or not all(
            isinstance(item, dict)
            and set(item) == {"role", "content"}
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
            and bool(item["content"])
            for item in raw_messages
        ):
            raise ValueError("Anthropic messages must contain user or assistant items")
        messages: list[dict[str, Any]] = []
        if payload.get("system") is not None:
            messages.append({"role": "system", "content": _system_text(payload["system"])})
        messages.extend(
            {"role": item["role"], "content": item["content"]}
            for item in raw_messages
        )
        return CompletionRequest(
            model=payload.get("model"),
            messages=messages,
            temperature=payload.get("temperature"),
            max_tokens=payload.get("max_tokens"),
            timeout=payload.get("timeout"),
        )

    @staticmethod
    def from_response(response: ModelResponse) -> dict[str, Any]:
        if len(response.choices) != 1:
            raise ValueError("Anthropic output requires one choice")
        choice = response.choices[0]
        if choice.message.role != "assistant" or not isinstance(choice.message.content, str):
            raise ValueError("Anthropic output must contain assistant text")
        usage = response.usage
        stop_reasons = {
            "stop": "end_turn",
            "length": "max_tokens",
        }
        if choice.finish_reason not in stop_reasons:
            raise ValueError("Unsupported Anthropic stop reason")
        stop_reason = stop_reasons[choice.finish_reason]
        return {
            "id": response.id,
            "type": "message",
            "role": "assistant",
            "model": response.model,
            "content": [{"type": "text", "text": choice.message.content}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.prompt_tokens if usage else 0,
                "output_tokens": usage.completion_tokens if usage else 0,
            },
        }
