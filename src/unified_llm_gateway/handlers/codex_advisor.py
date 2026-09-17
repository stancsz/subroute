"""In-process LiteLLM CustomLLM provider for OpenAI Codex Subscription (e.g. Terra) as an Advisor."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
import time
import uuid
from typing import Any
import httpx

from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError
from litellm.types.utils import GenericStreamingChunk, ModelResponse, Usage

from unified_llm_gateway.plugins.codex_credentials import CODEX_API_BASE, read_codex_credentials
from unified_llm_gateway.handlers.codex_messages import normalize_messages

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


async def stream_events(response: httpx.Response):
    """Decode SSE data fields at event boundaries, including multiline JSON."""
    fields = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            fields.append(line[5:].lstrip(" "))
        elif not line and fields:
            data = "\n".join(fields)
            fields.clear()
            if data == "[DONE]":
                return
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Malformed Codex stream event") from exc
            if not isinstance(event, dict):
                raise RuntimeError("Invalid Codex stream event")
            yield event
    if fields:
        raise RuntimeError("Truncated Codex SSE event")


def build_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in normalize_messages(messages):
        role = str(message.get("role", "user"))
        if role not in {"user", "assistant", "developer"} or message.get("tool_calls"):
            raise ValueError("Codex advisor accepts text messages without tool calls only")
        content = message.get("content", "")
        if isinstance(content, list):
            if any(not isinstance(item, dict) or item.get("type") not in {"text", "input_text", "output_text"}
                   or not isinstance(item.get("text"), str) for item in content):
                raise ValueError("Codex advisor does not support non-text content")
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            ]
            content = "\n".join(text_parts)
        elif not isinstance(content, str):
            raise ValueError("Codex advisor content must be text")

        if not content.strip():
            continue

        valid_role = role
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
) -> tuple[str, Usage]:
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
    terminal = None
    async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    err_body = await response.aread()
                    detail = err_body.decode("utf-8", errors="replace")[:200]
                    raise RuntimeError(f"Codex subscription API error {response.status_code}: {detail}")
                async for event in stream_events(response):
                    if event.get("type") in {"response.failed", "response.incomplete", "error"}:
                        raise RuntimeError("Codex advisor stream failed or was incomplete")
                    if event.get("type") == "response.completed":
                        if terminal is not None:
                            raise RuntimeError("Duplicate Codex completion event")
                        terminal = event.get("response")
                        if not isinstance(terminal, dict) or terminal.get("status") != "completed":
                            raise RuntimeError("Invalid Codex completion event")
                    if event.get("type") == "response.output_text.delta":
                        delta = event.get("delta")
                        if terminal is not None or not isinstance(delta, str):
                            raise RuntimeError("Invalid Codex text delta")
                        deltas.append(delta)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP error communicating with Codex backend: {exc}") from exc

    if terminal is None:
        raise RuntimeError("Codex stream ended without a completed response")
    result = "".join(deltas).strip()
    if not result:
        raise RuntimeError("Codex subscription returned an empty response")
    raw_usage = terminal.get("usage")
    if not isinstance(raw_usage, dict) or any(
        type(raw_usage.get(key)) is not int or raw_usage[key] < 0
        for key in ("input_tokens", "output_tokens", "total_tokens")
    ):
        raise RuntimeError("Codex completed response omitted valid provider usage")
    return result, Usage(
        prompt_tokens=raw_usage["input_tokens"],
        completion_tokens=raw_usage["output_tokens"],
        total_tokens=raw_usage["total_tokens"],
    )


class CodexAdvisorLLM(CustomLLM):
    """Bridge for Codex subscription to act as a synchronous text advisor."""

    async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        model = str(kwargs.get("model") or (args[0] if args else "gpt-5.6-terra"))
        if "/" in model:
            model = model.split("/", 1)[1]
        messages = kwargs.get("messages") or (args[1] if len(args) > 1 else [])
        optional_params = kwargs.get("optional_params") or {}
        if optional_params.get("tools") or kwargs.get("tools"):
            raise CustomLLMError(status_code=400, message="Codex advisor does not support tools")

        try:
            content, usage = await call_codex_streaming_collect(model, messages)
        except ValueError as exc:
            raise CustomLLMError(status_code=400, message=str(exc)) from exc
        except (RuntimeError, TimeoutError) as exc:
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
            usage=usage,
        )

    async def astreaming(self, *args: Any, **kwargs: Any) -> AsyncIterator[GenericStreamingChunk]:
        raise CustomLLMError(status_code=400, message="Advisor streaming is not supported")
        yield  # pragma: no cover


codex_advisor_handler = CodexAdvisorLLM()
