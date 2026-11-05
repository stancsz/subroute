"""Refresh local Codex subscription credentials immediately before dispatch."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.custom_llm import CustomLLMError
from subroute.handlers.codex_messages import normalize_messages


CODEX_API_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_RESPONSES_LITE_HEADER = "x-openai-internal-codex-responses-lite"
# Internal-only metadata key for the streaming-only Codex Responses backend.
_CODEX_STREAMING_MODEL = "subroute-codex-responses-stream"


def _missing_streaming_metadata(model: str) -> bool:
    if litellm.utils.supports_native_streaming(model, "openai"):
        return False
    try:
        litellm.utils.get_model_info_helper(model=model, custom_llm_provider="openai")
    except litellm.exceptions.ModelNotMappedError:
        return True
    except Exception:
        # Leave LiteLLM's normal decision in place for unrelated lookup failures.
        return False
    return False


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
        input_items = updated.get("input")
        tools = updated.get("tools")
        additional_tools = isinstance(input_items, list) and any(
            isinstance(item, dict) and item.get("type") == "additional_tools" for item in input_items
        )
        custom_tools = (isinstance(tools, list) and any(
            isinstance(tool, dict) and tool.get("type") == "custom" for tool in tools
        )) or (isinstance(input_items, list) and any(
            isinstance(item, dict) and item.get("type") in {"custom_tool_call", "custom_tool_call_output"}
            for item in input_items
        ))
        if (
            getattr(call_type, "value", call_type) in {"responses", "aresponses"}
            and (additional_tools or custom_tools)
        ):
            # Use LiteLLM's native adapter. Its custom-provider Chat bridge
            # loses Responses-only items and reasoning.context; native custom
            # provider dispatch currently fails the LlmProviders enum check.
            # Retire this remap when LiteLLM supports native custom providers.
            updated["custom_llm_provider"] = "openai"
            if isinstance(updated.get("model"), str):
                updated["model"] = updated["model"].rsplit("/", 1)[-1]
                if _missing_streaming_metadata(updated["model"]):
                    # LiteLLM otherwise fakes a stream for a model missing from
                    # its cost map, but this backend requires a real stream.
                    # Scope the metadata to an internal name and restore the
                    # actual model in the native adapter's outgoing body.
                    extra_body = updated.get("extra_body")
                    if extra_body is None or isinstance(extra_body, dict):
                        if isinstance(extra_body, dict) and "model" in extra_body and extra_body["model"] != updated["model"]:
                            raise CustomLLMError(400, "extra_body.model conflicts with the selected Codex model")
                        if not litellm.utils.supports_native_streaming(_CODEX_STREAMING_MODEL, "openai"):
                            litellm.register_model({f"openai/{_CODEX_STREAMING_MODEL}": {
                                "litellm_provider": "openai", "mode": "responses",
                                "supports_native_streaming": True,
                            }})
                        updated["extra_body"] = {"model": updated["model"], **(extra_body or {})}
                        updated["model"] = _CODEX_STREAMING_MODEL
            if additional_tools:
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
