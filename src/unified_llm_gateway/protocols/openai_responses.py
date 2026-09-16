from __future__ import annotations

from typing import Any

from litellm.types.utils import ModelResponse

from ..kernel import CompletionRequest


class OpenAIResponsesProtocol:
    """Minimal non-streaming, text-only OpenAI Responses boundary."""

    @staticmethod
    def to_request(payload: dict[str, Any]) -> CompletionRequest:
        allowed = {"model", "input", "temperature", "max_output_tokens", "timeout"}
        if set(payload) - allowed:
            raise ValueError("Unsupported Responses fields")
        raw_input = payload.get("input")
        if isinstance(raw_input, str) and raw_input:
            messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list) and raw_input and all(
            isinstance(item, dict)
            and set(item) == {"role", "content"}
            and item.get("role") in {"system", "user", "assistant"}
            and isinstance(item.get("content"), str)
            and bool(item["content"])
            for item in raw_input
        ):
            messages = [
                {"role": item["role"], "content": item["content"]}
                for item in raw_input
            ]
        else:
            raise ValueError("Responses input must be text or message items")
        return CompletionRequest(
            model=payload.get("model"),
            messages=messages,
            temperature=payload.get("temperature"),
            max_tokens=payload.get("max_output_tokens"),
            timeout=payload.get("timeout"),
        )

    @staticmethod
    def from_response(response: ModelResponse) -> dict[str, Any]:
        if len(response.choices) != 1:
            raise ValueError("Responses output requires one choice")
        choice = response.choices[0]
        if choice.message.role != "assistant" or not isinstance(choice.message.content, str):
            raise ValueError("Responses output must contain assistant text")
        text = choice.message.content
        usage = response.usage
        return {
            "id": response.id,
            "object": "response",
            "created_at": response.created,
            "status": "completed",
            "model": response.model,
            "output": [
                {
                    "id": f"msg_{response.id}",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {
                "input_tokens": usage.prompt_tokens if usage else 0,
                "output_tokens": usage.completion_tokens if usage else 0,
                "total_tokens": usage.total_tokens if usage else 0,
            },
        }
