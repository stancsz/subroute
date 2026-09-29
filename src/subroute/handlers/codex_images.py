"""Narrow hosted-image bridge for the streaming-only Codex backend.

LiteLLM 1.103.0 CustomLLM has no Images implementation and its Chat bridge
drops hosted image tools. Keep HTTP/SSE parsing in native litellm.aresponses;
retire this bridge when LiteLLM preserves Codex hosted images natively.
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


async def collect_image_response(request: dict) -> dict:
    """Collect the native response once, retaining every completed output item."""
    token, account = read_codex_credentials()
    stream = None
    request = dict(request)
    # Same documented Codex token-cap limitation as the text adapter.
    for key in ("model", "stream", "max_tokens", "max_output_tokens", "user"):
        request.pop(key, None)
    request.setdefault("store", False)
    try:
        async with asyncio.timeout(IMAGE_TIMEOUT):
            stream = await litellm.aresponses(
                model="openai/gpt-6-luna",
                **request, stream=True, num_retries=0, timeout=IMAGE_TIMEOUT,
                api_base=CODEX_API_BASE, api_key=token,
                extra_headers={"ChatGPT-Account-ID": account,
                               "User-Agent": "OpenAI-Codex/0.151.0 (Windows)"},
            )
            items: dict[str, dict] = {}
            terminal = None
            async for event in stream:
                event = _plain(event)
                kind = event.get("type")
                if kind in {"error", "response.failed", "response.incomplete"}:
                    raise CustomLLMError(502, "Luna image generation failed or was incomplete")
                if kind == "response.output_item.done":
                    item = event["item"]
                    if item.get("type") == "image_generation_call":
                        _image(item)
                    items[item["id"]] = item
                    if sum(item.get("type") == "image_generation_call" for item in items.values()) > 1:
                        raise CustomLLMError(502, "Luna returned more than the supported single image")
                if kind == "response.completed":
                    terminal = event["response"]
                    for item in terminal.get("output") or []:
                        items[item["id"]] = item
                    break
            if not terminal or terminal.get("status") != "completed" or not items:
                raise CustomLLMError(502, "Luna finished without completed output")
            if terminal.get("model") != "gpt-6-luna":
                raise CustomLLMError(502, "Image response did not confirm the requested Luna model")
            images = [item for item in items.values() if item.get("type") == "image_generation_call"]
            if len(images) > 1:
                raise CustomLLMError(502, "Luna returned more than the supported single image")
            for item in images:
                _image(item)
            terminal["output"] = list(items.values())
            if terminal.get("usage"):
                terminal["usage"] = {k: v for k, v in terminal["usage"].items() if k != "cost"}
            return terminal
    except TimeoutError as exc:
        raise CustomLLMError(504, "Luna image generation exceeded its 180 second deadline") from exc
    finally:
        if stream is not None:
            # Native LiteLLM Responses iterator owns an httpx.Response, and
            # does not expose aclose itself in the deployed version.
            await stream.response.aclose()


async def generate_image(prompt: str, options: dict) -> ImageResponse:
    terminal = await collect_image_response({
        "input": [{"role": "user", "content": prompt}],
        "tools": [{"type": "image_generation", **options}],
        "tool_choice": {"type": "image_generation"},
    })
    images = [item for item in terminal["output"] if item.get("type") == "image_generation_call"]
    if len(images) != 1:
        raise CustomLLMError(502, "Luna finished without exactly one completed image")
    item = images[0]
    response = ImageResponse(data=[_image(item)], **{
        key: item[key] for key in ("size", "quality", "background", "output_format") if key in item
    })
    decoded = base64.b64decode(item["result"])
    if decoded.startswith(b"\x89PNG\r\n\x1a\n") and len(decoded) >= 24:
        width, height = struct.unpack(">II", decoded[16:24])
        response.size = f"{width}x{height}"
    # Actual Luna token counts are separate from unavailable image usage/cost.
    response.usage = None
    response.model = terminal["model"]
    response.response_id = terminal.get("id")
    response.luna_usage = terminal.get("usage")
    response.image_usage = item.get("usage")
    return response


async def image_conversation(messages: list, options: dict, context: dict):
    """Reuse native LiteLLM input/tool/usage translation around hosted images."""
    from litellm.completion_extras.litellm_responses_transformation.transformation import LiteLLMResponsesTransformationHandler
    from litellm.responses.utils import ResponseAPILoggingUtils
    from litellm.types.utils import Choices, Message, ModelResponse
    from openai.types.responses import ResponseOutputMessage
    from subroute.handlers.codex_messages import normalize_messages
    from subroute.plugins.image_intent import image_tool

    bridge = LiteLLMResponsesTransformationHandler()
    if "responses_request" in context:
        request = dict(context["responses_request"])
        if isinstance(request.get("input"), str):
            request["input"] = [{"role": "user", "content": request["input"]}]
        elif isinstance(request.get("input"), list):
            request["input"] = normalize_messages(request["input"])
    else:
        inputs, instructions = bridge.convert_chat_completion_messages_to_responses_api(normalize_messages(messages))
        request = {"input": inputs}
        bridge._map_optional_params_to_responses_api_request(options, request)
        if instructions:
            request["instructions"] = instructions
    request["tools"] = [tool for tool in request.get("tools", []) if not image_tool(tool)] + [context["tool"]]
    if context["tool_choice"] == {"type": "image_generation"}:
        request["tool_choice"] = context["tool_choice"]
    terminal = await collect_image_response(request)
    if not terminal.get("usage"):
        raise CustomLLMError(502, "Luna omitted usage required by the conversation protocol bridge")
    choices = bridge._convert_response_output_to_choices(
        # The raw-dict callback retains only the first text block. The SDK
        # message type lets LiteLLM preserve every block and its annotations.
        [ResponseOutputMessage(**item) if item.get("type") == "message" else item
         for item in terminal["output"]],
        handle_raw_dict_callback=bridge._handle_raw_dict_response_item,
    )
    # Keep one Chat choice with all text/tool content, rather than presenting
    # sequential provider output items as alternative completions.
    content = "\n".join(choice.message.content for choice in choices if choice.message.content)
    refusals = [block["refusal"] for item in terminal["output"] if item.get("type") == "message"
                for block in item.get("content", []) if block.get("type") == "refusal" and block.get("refusal")]
    if refusals:
        content = "\n".join(filter(None, [content, *refusals]))
    tools = [tool for choice in choices for tool in (choice.message.tool_calls or [])]
    annotations = [annotation for choice in choices for annotation in (getattr(choice.message, "annotations", None) or [])]
    images = [{"type": "image_url", "image_url": {
        "url": f"data:image/{item.get('output_format', 'png')};base64,{item['result']}"
    }, "index": index} for index, item in enumerate(terminal["output"]) if item.get("type") == "image_generation_call"]
    if not (content or tools or images):
        raise CustomLLMError(502, "Luna returned no supported conversation output")
    protocol = context["protocol"]
    if protocol in {"anthropic_messages", "aanthropic_messages"}:
        # Messages has no generated-image output block. A Markdown data URL is
        # lossless text carried by LiteLLM's normal buffered/SSE Messages path.
        content += "".join(f"\n\n![Generated image]({image['image_url']['url']})" for image in images)
        images = []
    result = ModelResponse(model=terminal["model"], id=terminal.get("id"), choices=[Choices(
        index=0, message=Message(content=content or None, tool_calls=tools or None, images=images or None,
                                 annotations=annotations or None),
        finish_reason="tool_calls" if tools else "stop",
    )], usage=ResponseAPILoggingUtils._transform_response_api_usage_to_chat_usage(terminal["usage"]))
    # The buffered Responses bridge emits either images or text from one
    # choice. Separate internal choices preserve both as Responses items.
    if protocol in {"responses", "aresponses"} and not options.get("stream") and images and content:
        result.choices[0].message.images = None
        result.choices.append(Choices(index=1, message=Message(content=None, images=images), finish_reason="stop"))
    return result
