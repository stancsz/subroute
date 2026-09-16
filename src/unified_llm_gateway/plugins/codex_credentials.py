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


class CodexCredentialRefresher(CustomLogger):
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
        return updated


codex_credential_refresher = CodexCredentialRefresher()
