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


def has_tool_history(messages: list[dict[str, Any]]) -> bool:
    """Return whether a chat history contains an OpenAI or Anthropic tool turn."""
    for message in messages:
        if message.get("role") == "tool" or message.get("tool_calls"):
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") in {"tool_use", "tool_result"}
            for item in content
        ):
            return True
    return False


def supports_tool_history(messages: list[dict[str, Any]]) -> bool:
    """Check the exact conversion used by the Codex advisor without mutating input."""
    if not has_tool_history(messages):
        return True
    try:
        build_responses_input(messages)
    except ValueError:
        return False
    return True


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
    seen_tool_calls: set[str] = set()

    def add_message(role: str, content: str) -> None:
        if not content.strip():
            return
        content_type = "output_text" if role == "assistant" else "input_text"
        items.append({
            "type": "message",
            "role": role,
            "content": [{"type": content_type, "text": content}],
        })

    def add_function_call(call_id: Any, name: Any, arguments: Any) -> None:
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Advisor tool call is missing a call id")
        if call_id in seen_tool_calls:
            raise ValueError(f"Duplicate advisor tool call id: {call_id}")
        if not isinstance(name, str) or not name:
            raise ValueError("Advisor tool call is missing a function name")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        if not isinstance(arguments, str):
            raise ValueError("Advisor tool call arguments must be JSON text or an object")
        try:
            json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("Advisor tool call arguments must be valid JSON") from exc
        items.append({
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        })
        seen_tool_calls.add(call_id)

    def tool_output(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list) and all(
            isinstance(part, dict)
            and part.get("type") in {"text", "input_text", "output_text"}
            and isinstance(part.get("text"), str)
            for part in content
        ):
            return "\n".join(part["text"] for part in content)
        raise ValueError("Advisor tool result must contain text only")

    def add_function_output(call_id: Any, output: Any) -> None:
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Advisor tool result is missing a call id")
        if call_id not in seen_tool_calls:
            raise ValueError(f"Advisor tool result has no matching call: {call_id}")
        items.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": tool_output(output),
        })

    for message in normalize_messages(messages):
        role = str(message.get("role", "user"))
        if role == "tool":
            add_function_output(message.get("tool_call_id"), message.get("content", ""))
            continue
        if role not in {"user", "assistant", "developer"}:
            raise ValueError(f"Codex advisor does not support message role: {role}")

        content = message.get("content", "")
        if isinstance(content, list):
            text_parts: list[str] = []

            def flush_text() -> None:
                if text_parts:
                    add_message(role, "\n".join(text_parts))
                    text_parts.clear()

            for part in content:
                if not isinstance(part, dict):
                    raise ValueError("Codex advisor content blocks must be objects")
                part_type = part.get("type")
                if part_type in {"text", "input_text", "output_text"} and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                elif part_type == "tool_use" and role == "assistant":
                    flush_text()
                    add_function_call(part.get("id"), part.get("name"), part.get("input", {}))
                elif part_type == "tool_result" and role == "user":
                    flush_text()
                    add_function_output(part.get("tool_use_id"), part.get("content", ""))
                else:
                    raise ValueError("Codex advisor does not support this content block")
            flush_text()
        elif not isinstance(content, str):
            raise ValueError("Codex advisor content must be text")
        else:
            add_message(role, content)

        tool_calls = message.get("tool_calls")
        if tool_calls is not None:
            if role != "assistant" or not isinstance(tool_calls, list):
                raise ValueError("Advisor tool_calls must be an assistant list")
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict) or not isinstance(tool_call.get("function"), dict):
                    raise ValueError("Advisor tool call must contain a function object")
                function = tool_call["function"]
                add_function_call(
                    tool_call.get("id"),
                    function.get("name"),
                    function.get("arguments", "{}"),
                )
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
