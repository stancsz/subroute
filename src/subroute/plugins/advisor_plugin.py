"""Opt-in Advisor tool injection for LiteLLM's Anthropic Messages endpoint."""

from __future__ import annotations

import os
import uuid
import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket

from subroute.handlers.codex_advisor import (
    call_codex_streaming_collect,
    has_tool_history,
    supports_tool_history,
)
from subroute.handlers.antigravity import invoke_agy, model_with_effort, prompt_from_messages


ADVISOR_TOOL_TYPE = "advisor_20260301"
SUPPORTED_CALL_TYPES = frozenset({"anthropic_messages", "aanthropic_messages"})
TOOL_HISTORY_ADVISORS = frozenset({
    "codex-terra-advisor",
    "codex-sol-advisor",
    "codex-astra-advisor",
})
CODEX_SUBSCRIPTION_MODELS = frozenset({
    "codex-subscription", "codex-astra", "codex-terra", "codex-luna", "codex-reserve",
})
ADVISOR_MODEL_NAMES = {
    "codex-terra-advisor": "gpt-5.6-terra",
    "codex-sol-advisor": "gpt-6-sol",
    "codex-astra-advisor": "gpt-6-astra",
}
ANTIGRAVITY_ADVISOR_MODELS = {
    "gemini-subscription": "gemini-3.8-flash",
    "gemini-subscription-3.7-flash": "gemini-3.7-flash",
    "gemini-subscription-3.6-flash": "gemini-3.6-flash",
    "gemini-subscription-pro": "gemini-3.1-pro",
}
logger = logging.getLogger(__name__)


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

        if (
            data.get("model") in CODEX_SUBSCRIPTION_MODELS | ANTIGRAVITY_ADVISOR_MODELS.keys()
            and advisor_model in {*ADVISOR_MODEL_NAMES, *ANTIGRAVITY_ADVISOR_MODELS}
        ):
            consultation_id = uuid.uuid4().hex
            try:
                if advisor_model in ANTIGRAVITY_ADVISOR_MODELS:
                    advice, usage = await invoke_agy(
                        model_with_effort(ANTIGRAVITY_ADVISOR_MODELS[advisor_model], advisor_effort),
                        prompt_from_messages(messages),
                    )
                else:
                    advice, usage = await call_codex_streaming_collect(
                        ADVISOR_MODEL_NAMES[advisor_model], messages,
                        **({"reasoning_effort": advisor_effort} if advisor_effort is not None else {}),
                    )
            except (RuntimeError, TimeoutError, ValueError) as exc:
                metadata["gateway_advisor"] = {
                    "status": "failed", "model": advisor_model,
                    "consultation_id": consultation_id, "reason": str(exc),
                }
                raise RuntimeError("Subscription advisor consultation failed; base request was not sent") from exc
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
