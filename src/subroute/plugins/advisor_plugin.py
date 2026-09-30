"""Opt-in Advisor tool injection for LiteLLM's Anthropic Messages endpoint."""

from __future__ import annotations

import os
import re
import uuid
import logging
from typing import Any
from fastapi import HTTPException

from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket

from subroute.handlers.codex_advisor import (
    call_codex_streaming_collect,
    has_tool_history,
    supports_tool_history,
)
from subroute.handlers.antigravity import invoke_agy, model_with_effort, prompt_from_messages
from subroute.handlers.antigravity import AntigravityBridgeError, AntigravityRequestTooLargeError
from subroute.handlers.codex_advisor import CodexAdvisorError


ADVISOR_TOOL_TYPE = "advisor_20260301"
SUPPORTED_CALL_TYPES = frozenset({"anthropic_messages", "aanthropic_messages"})
TOOL_HISTORY_ADVISORS = frozenset({
    "codex-gpt-6.1-sol-advisor",
    "codex-sol-advisor",
    "codex-astra-advisor",
    "codex-luna-advisor",
})
CODEX_SUBSCRIPTION_MODELS = frozenset({
    "codex-subscription", "codex-astra", "codex-terra", "codex-luna", "codex-reserve",
})
ADVISOR_MODEL_NAMES = {
    "codex-gpt-6.1-sol-advisor": "gpt-6.1-sol",
    "codex-sol-advisor": "gpt-6-sol",
    "codex-astra-advisor": "gpt-6-astra",
    "codex-luna-advisor": "gpt-6-luna",
}
ANTIGRAVITY_ADVISOR_MODELS = {
    "gemini-subscription": "gemini-3.8-flash",
    "gemini-subscription-3.7-flash": "gemini-3.7-flash",
    "gemini-subscription-3.6-flash": "gemini-3.6-flash",
    "gemini-subscription-pro": "gemini-3.1-pro",
}
logger = logging.getLogger(__name__)
_SECRET_FIELD = re.compile(
    r"(?i)\b(authorization|api[\s_-]?key|access[\s_-]?token|refresh[\s_-]?token|token|password)(['\"]?\s*[:=]\s*['\"]?)([^'\"\s,;}\]]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_API_KEY_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,})\b")


def _safe_error_reason(error: Exception) -> str:
    """Keep actionable provider context without logging credentials or newlines."""
    reason = str(error)
    reason = _BEARER_SECRET.sub("Bearer [REDACTED]", reason)
    reason = _SECRET_FIELD.sub(r"\1\2[REDACTED]", reason)
    reason = _API_KEY_TOKEN.sub("[REDACTED]", reason)
    reason = " ".join(reason.split())
    return reason[:300] or type(error).__name__


class AdvisorPlugin(CustomLogger):
    """Enable advisor orchestration from the captured routing policy."""

    def __init__(
        self,
        *,
        max_uses: int = 3,
    ) -> None:
        super().__init__()
        if not 1 <= max_uses <= 5:
            raise ValueError("max_uses must be between 1 and LiteLLM's hard cap of 5")
        self.max_uses = max_uses

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> dict | None:
        """Consult the selected advisor on Messages requests; an empty selection disables it."""

        if call_type not in SUPPORTED_CALL_TYPES:
            return None
        if data.get("gateway_image_request") is not None:
            # The image intent exception owns this request; do not add a
            # saved advisor call, instructions, or provider spending.
            return None
        _, metadata = get_or_create_metadata_bucket(data)
        # Native advisor calls retain metadata. Never turn a consultation into
        # another consultation, including Gemini aliases shared with targets.
        if metadata.get("advisor_sub_call") is True:
            return None
        policy = metadata.get("gateway_policy")
        if not isinstance(policy, dict):
            return None
        advisor_model = policy.get("advisor_model")
        if not advisor_model:
            return None
        advisor_effort = policy.get("advisor_reasoning_effort")

        messages = data.get("messages") or []
        if has_tool_history(messages) and (
            advisor_model not in TOOL_HISTORY_ADVISORS
            or not supports_tool_history(messages)
        ):
            metadata["gateway_advisor"] = {
                "status": "skipped",
                "reason": "unsupported_tool_history",
                "model": advisor_model,
            }
            return data

        minimax_gemini_pair = (
            data.get("model") == "minimax"
            and advisor_model in ANTIGRAVITY_ADVISOR_MODELS
        )
        existing_preconsult_pair = (
            data.get("model") in CODEX_SUBSCRIPTION_MODELS | ANTIGRAVITY_ADVISOR_MODELS.keys()
            and advisor_model in {*ADVISOR_MODEL_NAMES, *ANTIGRAVITY_ADVISOR_MODELS}
        )
        if (
            (minimax_gemini_pair or existing_preconsult_pair)
            and advisor_model in ANTIGRAVITY_ADVISOR_MODELS
            and any(not isinstance(message.get("content", ""), str) for message in messages)
        ):
            metadata["gateway_advisor"] = {
                "status": "skipped",
                "reason": "unsupported_content",
                "model": advisor_model,
            }
            return data
        if minimax_gemini_pair or existing_preconsult_pair:
            consultation_id = uuid.uuid4().hex
            try:
                if advisor_model in ANTIGRAVITY_ADVISOR_MODELS:
                    advice, usage = await invoke_agy(
                        model_with_effort(ANTIGRAVITY_ADVISOR_MODELS[advisor_model], advisor_effort),
                        prompt_from_messages(messages),
                        advisor=True,
                    )
                else:
                    advice, usage = await call_codex_streaming_collect(
                        ADVISOR_MODEL_NAMES[advisor_model], messages,
                        **({"reasoning_effort": advisor_effort} if advisor_effort is not None else {}),
                    )
            except (RuntimeError, TimeoutError, ValueError) as exc:
                reason = _safe_error_reason(exc)
                gateway_request_id = data.get("litellm_call_id")
                metadata["gateway_advisor"] = {
                    "status": "failed", "model": advisor_model,
                    "consultation_id": consultation_id, "reason": reason,
                    "gateway_request_id": gateway_request_id,
                }
                logger.error(
                    "advisor consultation failed gateway_request_id=%s consultation_id=%s model=%s base_model=%s error_type=%s error=%s",
                    gateway_request_id, consultation_id, advisor_model,
                    data.get("model"), type(exc).__name__, reason,
                )
                if isinstance(exc, AntigravityRequestTooLargeError):
                    status_code = 413
                elif isinstance(exc, ValueError):
                    status_code = 400
                elif isinstance(exc, TimeoutError):
                    status_code = 504
                elif isinstance(exc, (AntigravityBridgeError, CodexAdvisorError)):
                    upstream_status = exc.status_code
                    status_code = upstream_status if upstream_status == 429 else (
                        504 if upstream_status == 504 else 502
                    )
                else:
                    status_code = 502
                raise HTTPException(
                    status_code=status_code,
                    detail={
                        "error": {
                            "type": "advisor_consultation_failed",
                            "message": "Selected advisor failed; base request was not sent",
                            "request_id": gateway_request_id,
                            "consultation_id": consultation_id,
                        }
                    },
                ) from exc
            data["messages"] = [
                *messages,
                {"role": "developer", "content": f"Independent advisor guidance:\n{advice}"},
            ]
            metadata["gateway_advisor"] = {
                "status": "advice_injected", "model": advisor_model,
                "consultation_id": consultation_id, "usage_source": "provider",
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "reasoning_effort": advisor_effort,
            }
            logger.warning(
                "advisor advice_injected consultation_id=%s model=%s input_tokens=%s output_tokens=%s base_model=%s reasoning_effort=%s",
                consultation_id, advisor_model, usage.prompt_tokens,
                usage.completion_tokens, data["model"], advisor_effort,
            )
            return data

        raw_tools = data.get("tools")
        if raw_tools is None:
            tools: list[dict[str, Any]] = []
        elif isinstance(raw_tools, list) and all(
            isinstance(tool, dict) for tool in raw_tools
        ):
            tools = list(raw_tools)
        else:
            raise ValueError("tools must be a list of objects")

        if not any(tool.get("type") == ADVISOR_TOOL_TYPE for tool in tools):
            tools.append(
                {
                    "type": ADVISOR_TOOL_TYPE,
                    "name": "advisor",
                    "model": advisor_model,
                    "max_uses": self.max_uses,
                    "caching": {"type": "ephemeral", "ttl": "5m"},
                }
            )
            data["tools"] = tools
        return data


advisor_plugin_instance = AdvisorPlugin(
    max_uses=int(os.getenv("ADVISOR_MAX_USES", "3")),
)
