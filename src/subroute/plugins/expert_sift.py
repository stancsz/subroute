"""Bound and compact requests on the dedicated 4040 experts service only."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

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


class ExpertSiftHook(CustomLogger):
    """Run Sift and enforce short expert calls before 4040 provider admission."""

    def __init__(self) -> None:
        super().__init__()
        self.sift = SiftLiteLLMHook(
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
            return data
        try:
            return await self.sift.async_pre_call_hook(user_api_key_dict, cache, data, call_type)
        except HTTPException:
            raise

    async def async_post_call_response_headers_hook(
        self, data: dict, user_api_key_dict: Any, response: object,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, object] | None = None,
    ) -> dict[str, str] | None:
        """Forward Sift's native response audit headers for compacted calls."""
        return await self.sift.async_post_call_response_headers_hook(
            data, user_api_key_dict, response, request_headers, litellm_call_info,
        )


expert_sift_hook = ExpertSiftHook()
