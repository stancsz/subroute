"""Opt-in Advisor tool injection for LiteLLM's Anthropic Messages endpoint."""

from __future__ import annotations

import os
from typing import Any

from litellm.integrations.custom_logger import CustomLogger


ADVISOR_TOOL_TYPE = "advisor_20260301"
SUPPORTED_CALL_TYPES = frozenset({"anthropic_messages", "aanthropic_messages"})


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
        if data.get("model") not in self.target_model_aliases:
            return None

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
            advisor_model = self.advisor_model
            policy = (data.get("metadata") or {}).get("gateway_policy")
            if isinstance(policy, dict):
                advisor_model = policy["advisor_model"]
            elif self is advisor_plugin_instance:
                raise ValueError("Gateway routing policy must be resolved before advisor injection")
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
