"""Codex instruction-role compatibility at the authenticated provider boundary.

Use developer for system instructions; never demote instructions to user text.
All content and tool fields remain intact for LiteLLM's native translation.
"""

from typing import Any


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(message, role="developer") if message.get("role") == "system" else dict(message)
            for message in messages]
