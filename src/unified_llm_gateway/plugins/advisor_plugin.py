"""Opt-in Advisor tool injection for LiteLLM's Anthropic Messages endpoint."""

from __future__ import annotations

import os
import uuid
import logging
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

from unified_llm_gateway.handlers.codex_advisor import (
    call_codex_streaming_collect,
    has_tool_history,
    supports_tool_history,
)


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
    "codex-sol-advisor": "gpt-5.6-sol",
    "codex-astra-advisor": "gpt-6-astra",
}
logger = logging.getLogger(__name__)


def _csv(name: str, default: str) -> frozenset[str]:
    value = os.getenv(name, default)
    return frozenset(item.strip() for item in value.split(",") if item.strip())


class AdvisorPlugin(CustomLogger):
    """Inject LiteLLM's native advisor orchestration tool for opt-in aliases."""

    def __init__(
        self,
        *,
        advisor_model: str = "gemini-subscription",
        target_model_aliases: frozenset[str] | None = None,
        max_uses: int = 3,
    ) -> None:
        super().__init__()
        if not advisor_model:
            raise ValueError("advisor_model must not be empty")
        if not 1 <= max_uses <= 5:
            raise ValueError("max_uses must be between 1 and LiteLLM's hard cap of 5")
        self.advisor_model = advisor_model
        self.target_model_aliases = target_model_aliases or frozenset(
            {"minimax-guided", "openai-guided", "openrouter-guided"}
        )
        self.max_uses = max_uses

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> dict | None:
        """Add one advisor tool without mutating non-guided requests."""

        if call_type not in SUPPORTED_CALL_TYPES:
            return None
        policy = (data.get("metadata") or {}).get("gateway_policy")
        policy_selected_target = (
            isinstance(policy, dict)
            and policy.get("advisor_model") is not None
            and policy.get("active_model") == data.get("model")
        )
        if data.get("model") not in self.target_model_aliases and not policy_selected_target:
            return None

        advisor_model = self.advisor_model
        if isinstance(policy, dict):
            advisor_model = policy["advisor_model"]
        elif self is advisor_plugin_instance:
            raise ValueError("Gateway routing policy must be resolved before advisor injection")
        if advisor_model is None:
            return data

        messages = data.get("messages") or []
        if has_tool_history(messages) and (
            advisor_model not in TOOL_HISTORY_ADVISORS
            or not supports_tool_history(messages)
        ):
            metadata = data.setdefault("metadata", {})
            metadata["gateway_advisor"] = {
                "status": "skipped",
                "reason": "unsupported_tool_history",
                "model": advisor_model,
            }
            return data

        if (
            data.get("model") in CODEX_SUBSCRIPTION_MODELS
            and advisor_model in ADVISOR_MODEL_NAMES
        ):
            consultation_id = uuid.uuid4().hex
            try:
                advice, usage = await call_codex_streaming_collect(
                    ADVISOR_MODEL_NAMES[advisor_model], messages
                )
            except (RuntimeError, TimeoutError, ValueError) as exc:
                metadata = data.setdefault("metadata", {})
                metadata["gateway_advisor"] = {
                    "status": "failed", "model": advisor_model,
                    "consultation_id": consultation_id, "reason": str(exc),
                }
                raise RuntimeError("Codex advisor consultation failed; base request was not sent") from exc
            data["messages"] = [
                *messages,
                {"role": "developer", "content": f"Independent advisor guidance:\n{advice}"},
            ]
            metadata = data.setdefault("metadata", {})
            metadata["gateway_advisor"] = {
                "status": "advice_injected", "model": advisor_model,
                "consultation_id": consultation_id, "usage_source": "provider",
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
            }
            logger.warning(
                "advisor advice_injected consultation_id=%s model=%s input_tokens=%s output_tokens=%s base_model=%s",
                consultation_id, advisor_model, usage.prompt_tokens,
                usage.completion_tokens, data["model"],
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
    advisor_model=os.getenv("ADVISOR_MODEL", "gemini-subscription"),
    target_model_aliases=_csv(
        "ADVISOR_TARGET_MODELS",
        "minimax-guided,openai-guided,openrouter-guided",
    ),
    max_uses=int(os.getenv("ADVISOR_MAX_USES", "3")),
)
