"""Translate explicitly requested image tools to the Codex hosted tool."""

from __future__ import annotations

from copy import deepcopy

CONVERSATION_CALLS = frozenset({
    "completion", "acompletion", "responses", "aresponses",
    "anthropic_messages", "aanthropic_messages",
})


def image_tool(tool: object) -> bool:
    return isinstance(tool, dict) and tool.get("type") == "image_generation"


def image_request_context(data: dict, call_type: str) -> dict | None:
    if call_type not in CONVERSATION_CALLS:
        return None
    tools = data.get("tools") or []
    hosted = [tool for tool in tools if image_tool(tool)]
    choice = data.get("tool_choice")
    selected = image_tool(choice)
    if choice == "none" or not (hosted or selected):
        return None
    if data.get("n", 1) != 1:
        raise ValueError("Luna image generation supports n=1 only")
    if len(hosted) > 1:
        raise ValueError("Only one hosted image_generation tool is supported")
    tool = deepcopy(hosted[0]) if hosted else {"type": "image_generation"}
    if tool.get("size", "auto") != "auto":
        raise ValueError("Codex image generation uses provider-selected dimensions; use size=auto")
    context = {
        "protocol": call_type,
        "tool": tool,
        "tool_choice": deepcopy(data.get("tool_choice", "auto")),
    }
    # Preserve native Responses inputs/options across LiteLLM's Chat bridge,
    # which otherwise discards hosted tools and some Responses-only fields.
    if call_type in {"responses", "aresponses"}:
        from litellm.types.llms.openai import ResponsesAPIOptionalRequestParams

        context["responses_request"] = deepcopy({
            k: v for k, v in data.items()
            if k == "input" or k in ResponsesAPIOptionalRequestParams.__annotations__
        })
    if selected:
        context["tool_choice"] = {"type": "image_generation"}
    # Reinsert hosted tools only at the provider boundary. The other tool
    # definitions remain available and retain native LiteLLM translations.
    data["tools"] = [tool for tool in tools if not image_tool(tool)]
    if selected:
        data.pop("tool_choice", None)
    return context
