"""In-process LiteLLM CustomLLM provider for OpenAI Codex Subscription (e.g. Terra) as an Advisor."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any
import httpx

from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError
from litellm.types.utils import GenericStreamingChunk, ModelResponse, Usage

from unified_llm_gateway.plugins.codex_credentials import CODEX_API_BASE, read_codex_credentials

MODELS = {
    "gpt-5.6-terra": "gpt-5.6-terra",
    "terra": "gpt-5.6-terra",
    "gpt-5.6-sol": "gpt-5.6-sol",
    "sol": "gpt-5.6-sol",
    "gpt-6-astra": "gpt-6-astra",
    "astra": "gpt-6-astra",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "luna": "gpt-5.6-luna",
    "gpt-reserve": "gpt-reserve",
}


def build_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") in ("text", "input_text")
            ]
            content = "\n".join(text_parts)
        elif not isinstance(content, str):
            content = str(content)

        if not content.strip():
            continue

        valid_role = role if role in ("user", "assistant", "system") else "user"
        content_type = "output_text" if valid_role == "assistant" else "input_text"
        items.append({
            "type": "message",
            "role": valid_role,
            "content": [{"type": content_type, "text": content}],
        })
    if not items:
        raise ValueError("No input messages provided to Codex advisor")
    return items


async def call_codex_streaming_collect(
    model_name: str,
    messages: list[dict[str, Any]],
    timeout: float = 60.0,
) -> str:
    access_token, account_id = read_codex_credentials()
    target_model = MODELS.get(model_name, model_name)
    input_items = build_responses_input(messages)

    url = f"{CODEX_API_BASE}/responses"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "ChatGPT-Account-ID": account_id,
        "User-Agent": "OpenAI-Codex/0.151.0 (Windows)",
        "Content-Type": "application/json",
    }
    payload = {
        "model": target_model,
        "stream": True,
        "store": False,
        "input": input_items,
    }

    deltas: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    err_body = await response.aread()
                    detail = err_body.decode("utf-8", errors="replace")[:200]
                    raise RuntimeError(f"Codex subscription API error {response.status_code}: {detail}")
                async for line in response.aiter_lines():
                    line = line.strip()
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            if event.get("type") == "response.output_text.delta":
                                deltas.append(event.get("delta", ""))
                        except json.JSONDecodeError:
                            continue
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP error communicating with Codex backend: {exc}") from exc

    result = "".join(deltas).strip()
    if not result:
        raise RuntimeError("Codex subscription returned an empty response")
    return result


class CodexAdvisorLLM(CustomLLM):
    """Bridge for Codex subscription to act as a synchronous text advisor."""

    async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        model = str(kwargs.get("model") or (args[0] if args else "gpt-5.6-terra"))
        if "/" in model:
            model = model.split("/", 1)[1]
        messages = kwargs.get("messages") or (args[1] if len(args) > 1 else [])

        try:
            content = await call_codex_streaming_collect(model, messages)
        except ValueError as exc:
            raise CustomLLMError(status_code=400, message=str(exc)) from exc
        except RuntimeError as exc:
            raise CustomLLMError(status_code=502, message=str(exc)) from exc

        return ModelResponse(
            id=f"chatcmpl-codex-advisor-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            usage=Usage(
                prompt_tokens=max(1, len(str(messages)) // 4),
                completion_tokens=max(1, len(content) // 4),
                total_tokens=max(2, (len(str(messages)) + len(content)) // 4),
            ),
        )

    async def astreaming(self, *args: Any, **kwargs: Any) -> AsyncIterator[GenericStreamingChunk]:
        raise CustomLLMError(status_code=400, message="Advisor streaming is not supported")
        yield  # pragma: no cover


codex_advisor_handler = CodexAdvisorLLM()
