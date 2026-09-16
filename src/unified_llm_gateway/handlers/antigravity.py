"""In-process LiteLLM provider for the authenticated Antigravity CLI."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError
from litellm.types.utils import GenericStreamingChunk, ModelResponse


MODELS = {
    "gemini-3.8-flash": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-med": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
    "gemini-3.1-pro": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
}


def prompt_from_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content", "")
        if not isinstance(content, str):
            raise ValueError("Antigravity subscription currently accepts text messages only")
        parts.append(f"[{str(message.get('role', 'user')).capitalize()}]:\n{content}")
    prompt = "\n\n".join(parts)
    if not prompt.strip():
        raise ValueError("Antigravity prompt is empty")
    return prompt


async def invoke_agy(model: str, prompt: str) -> str:
    executable = os.getenv("AGY_PATH") or shutil.which("agy")
    if not executable:
        raise RuntimeError("Antigravity CLI is unavailable")
    target = MODELS.get(model)
    if target is None:
        raise ValueError(f"Unsupported Antigravity model: {model}")

    process = await asyncio.create_subprocess_exec(
        executable,
        "--model",
        target,
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(prompt.encode("utf-8"))
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Antigravity exited with code {process.returncode}: {detail}")

    deltas: list[str] = []
    final_text = ""
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "step_update":
            delta = event.get("step_update", {}).get("text_delta", "")
            if delta:
                deltas.append(delta)
        elif event.get("event") == "result":
            final_text = event.get("result", {}).get("response", "")
    content = final_text or "".join(deltas)
    if not content:
        raise RuntimeError("Antigravity returned no response text")
    return content


class AntigravityLLM(CustomLLM):
    """Minimal text-only provider. Unsupported protocol features fail closed."""

    async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:
        model = str(kwargs.get("model") or (args[0] if args else "gemini-3.8-flash"))
        messages = kwargs.get("messages") or (args[1] if len(args) > 1 else [])
        optional_params = kwargs.get("optional_params") or {}
        if optional_params.get("stream"):
            raise CustomLLMError(status_code=400, message="Antigravity streaming is not yet certified")
        if optional_params.get("tools"):
            raise CustomLLMError(status_code=400, message="Antigravity tool calls are not yet certified")
        try:
            prompt = prompt_from_messages(messages)
            content = await invoke_agy(model, prompt)
        except ValueError as exc:
            raise CustomLLMError(status_code=400, message=str(exc)) from exc
        except RuntimeError as exc:
            raise CustomLLMError(status_code=502, message=str(exc)) from exc

        return ModelResponse(
            id=f"chatcmpl-agy-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        )

    async def astreaming(self, *args: Any, **kwargs: Any) -> AsyncIterator[GenericStreamingChunk]:
        raise CustomLLMError(status_code=400, message="Antigravity streaming is not yet certified")
        yield  # pragma: no cover


antigravity_handler = AntigravityLLM()
