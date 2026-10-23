"""Refresh local Codex subscription credentials immediately before dispatch."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from subroute.handlers.codex_messages import normalize_messages


CODEX_API_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_RESPONSES_LITE_HEADER = "x-openai-internal-codex-responses-lite"


def read_codex_credentials() -> tuple[str, str]:
    auth_path = Path(os.getenv("CODEX_AUTH_FILE", Path.home() / ".codex" / "auth.json"))
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = payload["tokens"]
        access_token = tokens["access_token"]
        account_id = tokens["account_id"]
        if (
            not isinstance(access_token, str)
            or not access_token.strip()
            or not isinstance(account_id, str)
            or not account_id.strip()
        ):
            raise ValueError("Codex auth file contains an invalid access token or account id")
        return access_token, account_id
    except (OSError, KeyError, TypeError, ValueError) as exc:
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
        if (
            getattr(call_type, "value", call_type) in {"responses", "aresponses"}
            and isinstance(updated.get("input"), list)
            and any(isinstance(item, dict) and item.get("type") == "additional_tools"
                    for item in updated["input"])
        ):
            # Use LiteLLM's native adapter. Its custom-provider Chat bridge
            # loses Responses-only items and reasoning.context; native custom
            # provider dispatch currently fails the LlmProviders enum check.
            # Retire this remap when LiteLLM supports native custom providers.
            updated["custom_llm_provider"] = "openai"
            if isinstance(updated.get("model"), str):
                updated["model"] = updated["model"].rsplit("/", 1)[-1]
            headers[CODEX_RESPONSES_LITE_HEADER] = "true"
            # Required defaults for this protocol mode. Preserve explicit
            # caller choices so conflicting inputs remain provider errors.
            updated["reasoning"] = {"context": "all_turns", **(updated.get("reasoning") or {})}
            if updated.get("parallel_tool_calls") is None:
                updated["parallel_tool_calls"] = False
            if updated.get("service_tier") == "fast":
                updated["service_tier"] = "priority"
        updated["extra_headers"] = headers
        # The public Responses contract accepts either a prompt string or an
        # input-item array. The Codex subscription backend accepts the latter;
        # wrap a string as one user message without changing array inputs.
        if isinstance(updated.get("input"), str):
            updated["input"] = [{"role": "user", "content": updated["input"]}]
        for field in ("messages", "input"):
            if isinstance(updated.get(field), list):
                updated[field] = normalize_messages(updated[field])
        return updated


codex_credential_refresher = CodexCredentialRefresher()
