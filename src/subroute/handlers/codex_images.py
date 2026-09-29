"""Narrow Images-to-Responses bridge for the streaming-only Codex backend.

LiteLLM 1.103.0 CustomLLM has no Images implementation and its Chat bridge
drops hosted image tools. Keep HTTP/SSE parsing in native litellm.aresponses;
retire this bridge when LiteLLM supports Codex subscription Images directly.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import struct
from typing import Any

import litellm
from litellm.llms.custom_llm import CustomLLMError
from litellm.types.utils import ImageResponse

from subroute.plugins.codex_credentials import CODEX_API_BASE, read_codex_credentials

IMAGE_MODEL = "codex-luna"
IMAGE_TIMEOUT = 180
MAX_IMAGE_BYTES = 20 * 1024 * 1024
TOOL_OPTIONS = frozenset({
    "size", "quality", "background", "output_format", "output_compression", "moderation",
})


def image_options(data: dict) -> dict:
    """Validate before LiteLLM's generic drop_params can discard an option."""
    for key in ("style", "user", "image", "mask"):
        if data.get(key) is not None:
            raise CustomLLMError(400, f"Luna image generation does not support {key}")
    if data.get("n", 1) != 1:
        raise CustomLLMError(400, "Luna image generation supports n=1 only")
    if data.get("response_format", "b64_json") != "b64_json":
        raise CustomLLMError(400, "Luna image generation returns response_format=b64_json only")
    if data.get("stream"):
        raise CustomLLMError(400, "The Images endpoint returns a complete image; omit stream")
    if data.get("size", "auto") != "auto":
        raise CustomLLMError(400, "Codex image generation uses provider-selected dimensions; use size=auto")
    return {key: data[key] for key in TOOL_OPTIONS if data.get(key) is not None}


def _plain(value: Any) -> dict:
    return value if isinstance(value, dict) else value.model_dump(exclude_none=True)


def _image(item: dict) -> dict:
    result = item.get("result")
    if item.get("status") != "completed" or not isinstance(result, str) or not result:
        raise CustomLLMError(502, "Luna image tool did not return a completed image")
    if len(result) > (MAX_IMAGE_BYTES * 4 // 3 + 4):
        raise CustomLLMError(502, "Luna image exceeds the 20 MiB response limit")
    try:
        decoded = base64.b64decode(result, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CustomLLMError(502, "Luna image tool returned invalid base64") from exc
    if not (decoded.startswith(b"\x89PNG\r\n\x1a\n") or decoded.startswith(b"\xff\xd8\xff")
            or (decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP")):
        raise CustomLLMError(502, "Luna image tool returned an invalid image payload")
    return {"b64_json": result, "revised_prompt": item.get("revised_prompt")}


async def generate_image(prompt: str, options: dict) -> ImageResponse:
    token, account = read_codex_credentials()
    stream = None
    try:
        async with asyncio.timeout(IMAGE_TIMEOUT):
            stream = await litellm.aresponses(
                model="openai/gpt-6-luna",
                input=[{"role": "user", "content": prompt}],
                tools=[{"type": "image_generation", **options}],
                tool_choice={"type": "image_generation"},
                store=False, stream=True, num_retries=0, timeout=IMAGE_TIMEOUT,
                api_base=CODEX_API_BASE, api_key=token,
                extra_headers={"ChatGPT-Account-ID": account,
                               "User-Agent": "OpenAI-Codex/0.151.0 (Windows)"},
            )
            images: dict[str, dict] = {}
            terminal = None
            async for event in stream:
                event = _plain(event)
                kind = event.get("type")
                if kind in {"error", "response.failed", "response.incomplete"}:
                    raise CustomLLMError(502, "Luna image generation failed or was incomplete")
                if kind == "response.output_item.done":
                    item = event["item"]
                    if item.get("type") == "image_generation_call":
                        images[item["id"]] = item
                        _image(item)
                        if len(images) > 1:
                            raise CustomLLMError(502, "Luna returned more than the requested single image")
                if kind == "response.completed":
                    terminal = event["response"]
                    for item in terminal.get("output") or []:
                        if item.get("type") == "image_generation_call":
                            images[item["id"]] = item
                    break
            if not terminal or terminal.get("status") != "completed" or len(images) != 1:
                raise CustomLLMError(502, "Luna finished without exactly one completed image")
            if terminal.get("model") != "gpt-6-luna":
                raise CustomLLMError(502, "Image response did not confirm the requested Luna model")
            item = next(iter(images.values()))
            response = ImageResponse(
                data=[_image(item)],
                **{key: item[key] for key in ("size", "quality", "background", "output_format") if key in item},
            )
            decoded = base64.b64decode(item["result"])
            if decoded.startswith(b"\x89PNG\r\n\x1a\n") and len(decoded) >= 24:
                width, height = struct.unpack(">II", decoded[16:24])
                response.size = f"{width}x{height}"
            # Responses usage is Luna usage, not image-token usage. Do not
            # mislabel it or let ImageResponse fabricate zero image usage.
            response.usage = None
            response.model = "gpt-6-luna"
            response.response_id = terminal.get("id")
            usage = terminal.get("usage")
            # LiteLLM may add its own estimated cost to the provider's token
            # counts. That estimate excludes the hosted image tool's cost.
            response.luna_usage = {k: v for k, v in usage.items() if k != "cost"} if usage else None
            response.image_usage = item.get("usage")
            return response
    except TimeoutError as exc:
        raise CustomLLMError(504, "Luna image generation exceeded its 180 second deadline") from exc
    finally:
        if stream is not None:
            # Native LiteLLM Responses iterator owns an httpx.Response, and
            # does not expose aclose itself in the deployed version.
            await stream.response.aclose()
