"""Deterministic image intent, owned by the request routing policy.

Only inspect the latest user turn and explicit tool contracts. Never classify
system instructions, assistant history, tool results, quoted prose, or code.
"""

from __future__ import annotations

import re
from copy import deepcopy

IMAGE_FUNCTIONS = frozenset({"image_generation", "generate_image", "create_image"})
CONVERSATION_CALLS = frozenset({
    "completion", "acompletion", "responses", "aresponses",
    "anthropic_messages", "aanthropic_messages",
})
_VISUAL = r"(?:image|picture|photo(?:graph)?|illustration|artwork|logo|icon|poster|wallpaper|thumbnail|infographic|portrait)s?"
_PREFIX = r"(?:(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+|(?:i\s+(?:want|need|would\s+like)\s+(?:(?:you\s+)?to\s+)?)|(?:let'?s\s+))?(?:please\s+)?)"
_MAKE = re.compile(
    rf"^{_PREFIX}(?:generate|create|make|render|design)\s+(?:(?:me|us)\s+)?"
    rf"(?!(?:a\s+)?(?:prompt|code|script|function|tutorial|story|description|example|test|api)\b)"
    rf"(?:[\w-]+\s+){{0,5}}{_VISUAL}\b", re.I,
)
_NON_VISUAL_OUTPUT = re.compile(r"\b(?:prompt|code|script|function|tutorial|story|description|example|test|api|article|poem|summary|explanation|documentation)\b", re.I)
_DRAW = re.compile(rf"^{_PREFIX}(?:draw|paint|sketch)\s+(?!conclusions?\b|attention\b|from\b|up\b)\S", re.I)
_NEED = re.compile(rf"^i\s+(?:want|need|would\s+like)\s+(?:an?\s+)?{_VISUAL}\s+(?:of|showing|depicting|with)\b", re.I)
_CHINESE = re.compile(r"^(?:(?:请|麻烦|帮我|给我|能不能|可以|能否|我想要|我要)\s*)*(?:生成|画|绘制|制作|设计)(?:一[张幅个]|个|张)?[^。！？\n]{0,18}(?:图片|图像|插画|照片|海报|头像|壁纸|图标|标志|图|画)|^(?:请|帮我|给我)?画(?:一[只个张幅]|个|只)")


def image_tool(tool: object) -> bool:
    if not isinstance(tool, dict):
        return False
    function = tool.get("function")
    function = function if isinstance(function, dict) else {}
    return tool.get("type") == "image_generation" or (
        function.get("name", tool.get("name")) in IMAGE_FUNCTIONS
    )


def latest_user_text(data: dict) -> str:
    items = data.get("messages", data.get("input", []))
    if isinstance(items, str):
        return items
    for item in reversed(items or []):
        if not isinstance(item, dict):
            continue
        # Tool outputs and an assistant's pending work are not fresh user intent.
        if item.get("type") in {"function_call_output", "image_generation_call"}:
            return ""
        if item.get("role") in {"tool", "assistant"}:
            return ""
        if item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            return content
        return "\n".join(
            block.get("text", "") for block in content or []
            if isinstance(block, dict) and block.get("type") in {"text", "input_text"}
        )
    return ""


def clear_image_request(text: str) -> bool:
    # Cline Desktop encloses the actual user turn in this transport envelope.
    # Unwrap only a whole-message envelope, not arbitrary XML/document content.
    wrapped = re.fullmatch(r'\s*<user_input mode="(?:act|plan|yolo)">([\s\S]*)</user_input>\s*', text)
    if wrapped:
        text = wrapped.group(1)
    text = re.sub(r"```[\s\S]*?```|`[^`]*`", "", text)
    text = re.sub(r'''"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)'[^'\n]+'(?!\w)''', "", text)
    for sentence in re.split(r"[\n.!?。！？]+", text):
        sentence = sentence.strip()
        if sentence.startswith(">"):
            continue
        make = _MAKE.search(sentence)
        if (make and not _NON_VISUAL_OUTPUT.search(make.group())) or _DRAW.search(sentence) or _NEED.search(sentence) or _CHINESE.search(sentence):
            return True
    return False


def image_request_context(data: dict, call_type: str) -> dict | None:
    if call_type not in CONVERSATION_CALLS:
        return None
    tools = data.get("tools") or []
    declared = [tool for tool in tools if image_tool(tool)]
    hosted = [tool for tool in declared if tool.get("type") == "image_generation"]
    selected = image_tool(data.get("tool_choice"))
    intent = clear_image_request(latest_user_text(data))
    if not (hosted or selected or intent):
        return None
    if data.get("n", 1) != 1:
        raise ValueError("Luna image generation supports n=1 only")
    if len(hosted) > 1:
        raise ValueError("Only one hosted image_generation tool is supported")
    if data.get("tool_choice") == "none" and (intent or selected):
        raise ValueError("Image generation requires tool access; tool_choice=none conflicts with this request")
    tool = deepcopy(hosted[0]) if hosted else {"type": "image_generation"}
    if tool.get("size", "auto") != "auto":
        raise ValueError("Codex image generation uses provider-selected dimensions; use size=auto")
    context = {
        "protocol": call_type,
        "reason": "image_tool" if hosted or selected else "image_intent",
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
    elif intent and "tool_choice" not in data:
        context["tool_choice"] = {"type": "image_generation"}
    # Reinsert hosted tools only at the provider boundary. The other tool
    # definitions remain available and retain native LiteLLM translations.
    data["tools"] = [tool for tool in tools if not image_tool(tool)]
    if selected:
        data.pop("tool_choice", None)
    return context
