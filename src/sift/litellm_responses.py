"""A selection view of stateless, textual Responses input.

LiteLLM owns protocol conversion and inference. Sift uses its deployed converter
only to inspect text, then restores retained original items, including IDs and
tool types. No converted chat message is sent to the provider by this adapter.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import HTTPException
from litellm.responses.litellm_completion_transformation.transformation import (
    LiteLLMCompletionResponsesConfig,
)


def _textual(value: Any) -> bool:
    return isinstance(value, str) or isinstance(value, list) and all(
        isinstance(block, dict) and block.get("type") in {"input_text", "output_text", "text"}
        and isinstance(block.get("text"), str) for block in value
    )


def response_selection_view(
    data: dict[str, Any],
) -> tuple[list[dict[str, Any]], Callable[[list[dict[str, Any]]], list[dict[str, Any]]]]:
    """Give the core a chat-shaped view and a lossless retained-item mapping.

    Consecutive parallel calls share one cycle boundary. The private converter
    is pinned to the optional LiteLLM dependency; replace this view when LiteLLM
    exposes a selection API that preserves non-message Responses items.
    """
    if data.get("previous_response_id") or data.get("conversation"):
        raise HTTPException(422, detail="Sift requires stateless Responses input with complete history")
    raw = data.get("input")
    items = [{"role": "user", "content": raw}] if isinstance(raw, str) else raw
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise HTTPException(400, detail="Sift requires textual Responses input")
    views: list[dict[str, Any]] = []
    originals: dict[str, list[dict[str, Any]]] = {}
    pending_calls: set[str] = set()
    instructions = data.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise HTTPException(422, detail="Sift requires textual Responses instructions")
        views.append({"role": "system", "content": instructions, "name": "sift_instructions"})
        originals["sift_instructions"] = []  # Counted in its original top-level field.
    for index, item in enumerate(items):
        kind = item.get("type", "message")
        if kind == "message":
            supported = item.get("role") in {"system", "developer", "user", "assistant"} and _textual(item.get("content"))
        elif kind in {"function_call", "custom_tool_call"}:
            supported = all(isinstance(item.get(field), str) and item[field]
                            for field in ("call_id", "name")) and isinstance(
                                item.get("input" if kind == "custom_tool_call" else "arguments"), str)
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            supported = isinstance(item.get("call_id"), str) and bool(item["call_id"]) and _textual(item.get("output"))
        elif kind == "reasoning":
            summary = item.get("summary", [])
            supported = not item.get("encrypted_content") and isinstance(summary, list) and all(
                isinstance(block, dict) and block.get("type") == "summary_text"
                and isinstance(block.get("text"), str) for block in summary
            ) and not item.get("content")
        else:
            supported = False
        if not supported:
            raise HTTPException(422, detail="Sift supports textual messages, function/custom tools and plain reasoning summaries only")
        if kind in {"function_call", "custom_tool_call"}:
            starts_batch = not pending_calls
            if item["call_id"] in pending_calls:
                raise HTTPException(422, detail="Sift requires distinct pending tool call IDs")
            pending_calls.add(item["call_id"])
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            if item["call_id"] not in pending_calls:
                raise HTTPException(422, detail="Sift requires each tool output's call in the supplied history")
            pending_calls.remove(item["call_id"])
        elif pending_calls and kind == "message" and item["role"] != "assistant":
            raise HTTPException(422, detail="Sift requires pending tool results before a new user or instruction turn")
        if kind == "reasoning":
            # Historical reasoning is not evidence; retained items stay exact.
            view = {"role": "assistant", "content": ""}
        else:
            converted = LiteLLMCompletionResponsesConfig.transform_responses_api_input_to_messages(
                input=[item], responses_api_request={},
            )
            if kind in {"function_call_output", "custom_tool_call_output"}:
                # LiteLLM may retrieve a cached call wrapper for an output. The
                # original call is already represented by its own input item.
                converted = [message for message in converted if message.get("role") == "tool"]
            if len(converted) != 1:
                raise HTTPException(422, detail="Sift cannot inspect this Responses item without losing its boundary")
            view = dict(converted[0])
        if kind in {"function_call", "custom_tool_call"} and not starts_batch:
            # Calls may interleave with reasoning or earlier results. Only the
            # first call of an outstanding batch is a safe eviction boundary.
            view["content"] = "tool_calls " + json.dumps(view.pop("tool_calls"), ensure_ascii=False)
        if view.get("role") == "assistant" and views and views[-1].get("role") == "assistant":
            previous = views[-1]
            def text(content: Any) -> str:
                return content if isinstance(content, str) else "\n".join(
                    block.get("text", "") for block in (content or [])
                )
            previous["content"] = "\n".join(filter(None, (text(previous.get("content")), text(view.get("content")))))
            previous["tool_calls"] = [*previous.get("tool_calls", []), *view.get("tool_calls", [])]
            originals[previous["name"]].append(item)
        else:
            marker = f"sift_original_{index}"
            originals[marker] = [item]
            views.append({**view, "name": marker})

    if pending_calls:
        raise HTTPException(422, detail="Sift requires complete tool results in stateless Responses history")

    def restore(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        for message in selected:
            marker = message.get("name")
            if marker in originals:
                output.extend(originals[marker])
            else:
                # The core only creates historical user-role excerpt messages.
                if message.get("role") != "user" or not isinstance(message.get("content"), str):
                    raise ValueError("unexpected Sift selection view")
                output.append({"role": "user", "content": message["content"]})
        return output

    return views, restore
