"""Refresh local Codex subscription credentials immediately before dispatch."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


CODEX_API_BASE = "https://chatgpt.com/backend-api/codex"


def read_codex_credentials() -> tuple[str, str]:
    auth_path = Path(os.getenv("CODEX_AUTH_FILE", Path.home() / ".codex" / "auth.json"))
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = payload["tokens"]
        return tokens["access_token"], tokens["account_id"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to load Codex credentials from {auth_path}") from exc


CODEX_MODELS_PREFIXES = ("codex-", "openai/responses/")


def _sanitize_codex_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Codex /responses endpoint rejects role='system'. Convert system messages to user messages."""
    sanitized: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            new_msg = dict(msg)
            new_msg["role"] = "user"
            content = new_msg.get("content", "")
            if isinstance(content, str):
                new_msg["content"] = f"[System Context]\n{content}"
            elif isinstance(content, list):
                new_content = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        new_part = dict(part)
                        new_part["text"] = f"[System Context]\n{new_part.get('text', '')}"
                        new_content.append(new_part)
                    else:
                        new_content.append(part)
                new_msg["content"] = new_content
            sanitized.append(new_msg)
        else:
            sanitized.append(msg)
    return sanitized


class CodexCredentialRefresher(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> dict | None:
        model = str(data.get("model", ""))
        if any(model.startswith(p) for p in ("codex-", "openai/responses/")) or model == "codex-subscription":
            raw_messages = data.get("messages")
            if isinstance(raw_messages, list):
                data["messages"] = _sanitize_codex_messages(raw_messages)
            metadata = data.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("user_id", None)
        return data

    async def async_pre_call_deployment_hook(
        self, kwargs: dict[str, Any], call_type: Any
    ) -> dict[str, Any] | None:
        if str(kwargs.get("api_base", "")).rstrip("/") != CODEX_API_BASE:
            return None
        access_token, account_id = read_codex_credentials()
        updated = kwargs.copy()
        updated["api_key"] = access_token
        headers = dict(updated.get("extra_headers") or {})
        headers["ChatGPT-Account-ID"] = account_id
        updated["extra_headers"] = headers
        updated.pop("max_output_tokens", None)
        return updated


codex_credential_refresher = CodexCredentialRefresher()

