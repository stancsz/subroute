from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Awaitable

from litellm.types.utils import ModelResponse

from ..kernel import CompletionRequest


_MODELS = {
    "gemini-3.8-flash": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-med": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
    "gemini-3.1-pro": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
}


def _prompt(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        parts.append(f"[{str(message.get('role', 'user')).capitalize()}]:\n{content}")
    return "\n\n".join(parts)


async def _run(command: list[str], prompt: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(prompt.encode("utf-8"))
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Antigravity exited with code {process.returncode}: {detail}")
    return stdout.decode("utf-8", errors="replace")


Runner = Callable[[list[str], str], Awaitable[str]]


@dataclass(frozen=True)
class GeminiSubscriptionAdapter:
    """Call the authenticated local Antigravity CLI and return ModelResponse."""

    agy_path: str
    runner: Runner = _run

    async def complete(self, request: CompletionRequest) -> ModelResponse:
        target = _MODELS.get(request.model)
        if target is None:
            raise ValueError(f"Unsupported Antigravity model: {request.model}")
        prompt = _prompt(request.messages)
        if not prompt.strip():
            raise ValueError("Antigravity prompt is empty")
        output = await asyncio.wait_for(
            self.runner(
                [
                    self.agy_path,
                    "--model",
                    target,
                    "--output-format",
                    "stream-json",
                    "--input-format",
                    "text",
                ],
                prompt,
            ),
            timeout=request.timeout,
        )
        text_parts: list[str] = []
        final_text = ""
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "step_update":
                delta = event.get("step_update", {}).get("text_delta", "")
                if delta:
                    text_parts.append(delta)
            elif event.get("event") == "result":
                final_text = event.get("result", {}).get("response", "")
        content = final_text or "".join(text_parts)
        if not content:
            raise RuntimeError("Antigravity returned no response text")
        return ModelResponse(
            id=f"chatcmpl-agy-{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
            model=request.model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            usage={
                "prompt_tokens": max(1, len(prompt) // 4),
                "completion_tokens": max(1, len(content) // 4),
                "total_tokens": max(1, (len(prompt) + len(content)) // 4),
            },
        )


def gemini_subscription_adapter(
    *, agy_path: str | None = None, runner: Runner = _run
) -> GeminiSubscriptionAdapter:
    executable = agy_path or os.getenv("AGY_PATH") or shutil.which("agy")
    if not executable:
        raise ValueError("Antigravity CLI is unavailable")
    return GeminiSubscriptionAdapter(executable, runner)
