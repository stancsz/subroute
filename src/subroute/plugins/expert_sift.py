"""Bound and compact requests on the dedicated 4040 experts service only."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sift.litellm_proxy import SiftLiteLLMHook


EXPERT_MODELS = (
    "codex-gpt-6.1-sol-advisor",
    "codex-sol-advisor",
    "codex-astra-advisor",
    "codex-luna-advisor",
    "gemini-3.8-flash-advisor",
    "gemini-3.7-flash-advisor",
    "gemini-3.6-flash-advisor",
    "gemini-3.1-pro-advisor",
    "gemini-4-argon-advisor",
)
EXPERT_SIFT_TARGET = 25_600
EXPERT_INPUT_LIMIT = 32_000
CODEX_EXPERT_OUTPUT_LIMIT = 1_024
CODEX_EXPERT_MODELS = frozenset({
    "codex-gpt-6.1-sol-advisor",
    "codex-sol-advisor",
    "codex-astra-advisor",
    "codex-luna-advisor",
})
CORRELATION_HEADERS = frozenset({"x-subroute-request-id", "x-subroute-attempt-id"})


def _has_correlation_fields(data: dict[str, Any]) -> bool:
    if "_subroute_sift_correlation" in data:
        return True
    proxy_request = data.get("proxy_server_request")
    carriers = (
        data.get("extra_headers"),
        proxy_request.get("headers") if isinstance(proxy_request, Mapping) else None,
    )
    return any(
        isinstance(carrier, Mapping)
        and any(str(name).lower() in CORRELATION_HEADERS for name in carrier)
        for carrier in carriers
    )


def _consume_correlation(data: dict[str, Any]) -> dict[str, str] | None:
    """Read gateway IDs, then remove them from all provider-facing carriers."""
    data.pop("_subroute_sift_correlation", None)
    proxy_request = data.get("proxy_server_request")
    values: dict[str, Any] = {}
    if isinstance(proxy_request, Mapping):
        headers = proxy_request.get("headers")
        if isinstance(headers, Mapping) and any(str(name).lower() in CORRELATION_HEADERS for name in headers):
            proxy_request = dict(proxy_request)
            values = {str(name).lower(): value for name, value in headers.items()}
            proxy_request["headers"] = {
                name: value for name, value in headers.items()
                if str(name).lower() not in CORRELATION_HEADERS
            }
            data["proxy_server_request"] = proxy_request
    extra_headers = data.get("extra_headers")
    if isinstance(extra_headers, Mapping) and any(str(name).lower() in CORRELATION_HEADERS for name in extra_headers):
        data["extra_headers"] = {
            name: value for name, value in extra_headers.items()
            if str(name).lower() not in CORRELATION_HEADERS
        }
    parsed: dict[str, str] = {}
    for header, field in (
        ("x-subroute-request-id", "request_id"),
        ("x-subroute-attempt-id", "advisor_attempt_id"),
    ):
        try:
            parsed[field] = str(uuid.UUID(values.get(header)))
        except (ValueError, TypeError, AttributeError):
            return None
    return parsed


def _is_expert_master_auth(auth: Any) -> bool:
    """Only accept correlation carried by an authenticated Experts admin call."""
    try:
        from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
        from litellm.proxy.auth.user_api_key_auth import LITELLM_PROXY_MASTER_KEY_ALIAS
    except ImportError:
        return False
    return (
        isinstance(auth, UserAPIKeyAuth)
        and auth.user_role == LitellmUserRoles.PROXY_ADMIN
        and auth.api_key == LITELLM_PROXY_MASTER_KEY_ALIAS
    )


def _has_nontext_content(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {
            "image", "image_url", "input_image", "input_audio", "audio",
            "video", "file", "input_file", "document",
        }:
            return True
        return any(_has_nontext_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_nontext_content(item) for item in value)
    return False


class ExpertSiftHook(SiftLiteLLMHook):
    """Run Sift and enforce short expert calls before 4040 provider admission."""

    # LiteLLM 1.106.2 discovers header hooks only on the concrete class.
    # Retire this alias when native discovery follows inherited overrides.
    async_post_call_response_headers_hook = SiftLiteLLMHook.async_post_call_response_headers_hook

    def __init__(self) -> None:
        super().__init__(
            {model: EXPERT_SIFT_TARGET for model in EXPERT_MODELS},
            max_target=EXPERT_INPUT_LIMIT,
        )

    @staticmethod
    def _bound_output(data: dict[str, Any], call_type: str) -> None:
        fields = ("max_output_tokens",) if call_type in {"responses", "aresponses"} else (
            ("max_tokens",) if call_type in {"anthropic_messages", "aanthropic_messages"}
            else ("max_completion_tokens", "max_tokens")
        )
        present = [field for field in fields if type(data.get(field)) is int]
        for field in present:
            data[field] = min(data[field], CODEX_EXPERT_OUTPUT_LIMIT)
        if not present:
            data[fields[0]] = CODEX_EXPERT_OUTPUT_LIMIT

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str,
    ) -> dict | None:
        model = data.get("model")
        if model not in EXPERT_MODELS:
            return None

        if _has_correlation_fields(data):
            data = {**data}
        # The internal headers are correlation data, not credentials. Trust them
        # only after LiteLLM has authenticated the Experts master key.
        presented_correlation = _consume_correlation(data)
        correlation = presented_correlation if _is_expert_master_auth(user_api_key_dict) else None
        if correlation is not None:
            data["_subroute_sift_correlation"] = correlation

        # Only this request's Sift work may supply the reserved audit. In
        # particular, multimodal bypass must not echo a caller's forged audit.
        for key in ("metadata", "litellm_metadata"):
            metadata = data.get(key)
            if isinstance(metadata, dict) and "sift" in metadata:
                data = {**data, key: {name: value for name, value in metadata.items() if name != "sift"}}

        # The Codex Responses adapter accepts the explicit output bound. The
        # Antigravity CLI has no equivalent control, so do not pretend a
        # LiteLLM request field limits its generation.
        if model in CODEX_EXPERT_MODELS:
            self._bound_output(data, call_type)
        if not any(data.get(key) is not None for key in (
            "reasoning_effort", "reasoning", "output_config", "thinking",
        )):
            if call_type in {"responses", "aresponses"}:
                data["reasoning"] = {"effort": "low"}
            elif call_type in {"anthropic_messages", "aanthropic_messages"}:
                data["thinking"] = {"type": "adaptive"}
                data["output_config"] = {"effort": "low"}
            else:
                data["reasoning_effort"] = "low"

        # Sift's selector is text-only. Preserve supported multimodal payloads
        # exactly and let the expert model's native 32K admission decide them.
        if _has_nontext_content(data.get("messages")) or _has_nontext_content(data.get("input")) or _has_nontext_content(data.get("system")):
            data.pop("_subroute_sift_correlation", None)
            return data
        try:
            result = await super().async_pre_call_hook(user_api_key_dict, cache, data, call_type)
        finally:
            data.pop("_subroute_sift_correlation", None)
        if isinstance(result, dict):
            result.pop("_subroute_sift_correlation", None)
        return result

expert_sift_hook = ExpertSiftHook()
