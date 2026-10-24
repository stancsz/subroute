"""Required Qwen template translation before native provider dispatch."""

from __future__ import annotations

from typing import Any

from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket


def frontload_anthropic_system_messages(data: dict[str, Any], call_type: str) -> dict[str, Any]:
    """Move in-sequence system entries to Qwen's required initial prefix.

    LiteLLM keeps Anthropic message roles and positions. Claude Code can send a
    system entry after its user turn, which this GGUF template rejects. Keep
    every entry and its role, preserving order within each group.
    """
    if call_type not in {"anthropic_messages", "aanthropic_messages"}:
        return data
    messages = data.get("messages")
    if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
        return data
    instructions = [message for message in messages if message.get("role") in {"system", "developer"}]
    if not instructions:
        return data
    dialogue = [message for message in messages if message.get("role") not in {"system", "developer"}]
    reordered = instructions + dialogue
    if reordered != messages:
        data["messages"] = reordered
        _, metadata = get_or_create_metadata_bucket(data)
        metadata["subroute_local_system_translation"] = "initial_system_prefix"
    return data
