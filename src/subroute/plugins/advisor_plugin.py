"""Expose the saved Advisor as an optional LiteLLM Messages tool."""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Any

from fastapi import HTTPException
from litellm.router import Router
from litellm.router_utils.cooldown_handlers import is_advisor_orchestration_failure
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket
from litellm.types.llms.anthropic import ANTHROPIC_ADVISOR_TOOL_TYPE

ADVISOR_TOOL_TYPE = ANTHROPIC_ADVISOR_TOOL_TYPE
ADVISOR_TOOL_DESCRIPTION = (
    "Use once only when an independent expert view is likely to change your answer. "
    "Ask a focused question and return a concise, actionable response."
)
SUPPORTED_CALL_TYPES = frozenset({"anthropic_messages", "aanthropic_messages"})
ADVISOR_MODEL_NAMES = {
    "codex-gpt-6.1-sol-advisor": "gpt-6.1-sol",
    "codex-sol-advisor": "gpt-6-sol",
    "codex-astra-advisor": "gpt-6-astra",
    "codex-luna-advisor": "gpt-6-luna",
}
EXPERT_TOOL_ROUTES = {
    **{alias: alias for alias in ADVISOR_MODEL_NAMES},
    "gemini-subscription": "gemini-3.8-flash-advisor",
    "gemini-subscription-argon": "gemini-4-argon-advisor",
    "gemini-subscription-3.7-flash": "gemini-3.7-flash-advisor",
    "gemini-subscription-3.6-flash": "gemini-3.6-flash-advisor",
    "gemini-subscription-pro": "gemini-3.1-pro-advisor",
}


def expert_route(advisor_model: str) -> str:
    """Return the fixed private 4040 transport alias for a saved expert."""
    alias = EXPERT_TOOL_ROUTES.get(advisor_model)
    if alias is None:
        raise HTTPException(status_code=400, detail="Saved Advisor is not available on the experts service")
    return f"experts/{alias}"


def _stop_parent_fallback_after_advisor_failure() -> None:
    """Keep LiteLLM from replaying a consultation after Advisor selection.

    The pinned interceptor marks expert errors, but leaves later target errors
    unmarked. Its Router uses the native marker only for cooldown accounting.
    Observe native selection within each handler invocation, without replacing
    the tool loop. Use a separate error attribute so a later target failure can
    still affect its deployment health. Retire when native orchestration and
    Router fallback jointly enforce a consultation budget across failures.
    """
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor

    handle = advisor.AdvisorOrchestrationHandler.handle
    if not getattr(handle, "_subroute_advisor_lifecycle_guard", False):
        selected: ContextVar[bool | None] = ContextVar("subroute_advisor_selected", default=None)
        find_tool_use = advisor._find_advisor_tool_use

        @wraps(find_tool_use)
        def observe_selection(*args: Any, **kwargs: Any) -> Any:
            result = find_tool_use(*args, **kwargs)
            if result is not None and selected.get() is not None:
                selected.set(True)
            return result

        @wraps(handle)
        async def guard_lifecycle(*args: Any, **kwargs: Any) -> Any:
            token = selected.set(False)
            try:
                return await handle(*args, **kwargs)
            except Exception as error:
                if selected.get():
                    setattr(error, "_subroute_advisor_selected", True)
                raise
            finally:
                selected.reset(token)

        guard_lifecycle._subroute_advisor_lifecycle_guard = True
        advisor._find_advisor_tool_use = observe_selection
        advisor.AdvisorOrchestrationHandler.handle = guard_lifecycle

    method = Router.async_function_with_fallbacks_common_utils
    if getattr(method, "_subroute_advisor_fallback_guard", False):
        return

    @wraps(method)
    async def no_parent_replay(self: Router, *args: Any, **kwargs: Any) -> Any:
        exception = args[0] if args else kwargs.get("e")
        if isinstance(exception, Exception) and (
            is_advisor_orchestration_failure(exception)
            or getattr(exception, "_subroute_advisor_selected", False)
        ):
            raise exception
        return await method(self, *args, **kwargs)

    no_parent_replay._subroute_advisor_fallback_guard = True
    Router.async_function_with_fallbacks_common_utils = no_parent_replay


class AdvisorPlugin(CustomLogger):
    """Expose one bounded, model-selected Advisor tool on Messages requests."""

    def __init__(self) -> None:
        super().__init__()
        _stop_parent_fallback_after_advisor_failure()

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str,
    ) -> dict | None:
        if call_type not in SUPPORTED_CALL_TYPES:
            return None

        _, metadata = get_or_create_metadata_bucket(data)
        policy = metadata.get("gateway_policy")
        advisor_model = policy.get("advisor_model") if isinstance(policy, dict) else None
        tools = data.get("tools")
        if tools is None:
            tools = []
        if not isinstance(tools, list):
            raise HTTPException(status_code=400, detail="Messages tools must be a list")

        advisor_tools = [
            tool for tool in tools
            if isinstance(tool, dict) and tool.get("type") == ADVISOR_TOOL_TYPE
        ]
        if len(advisor_tools) > 1:
            raise HTTPException(status_code=400, detail="At most one Advisor tool may be declared")
        if not advisor_model:
            if advisor_tools:
                raise HTTPException(status_code=400, detail="Advisor tool requires a configured expert")
            return None
        route = expert_route(advisor_model)

        if advisor_tools:
            tool = advisor_tools[0]
            # The configured policy, not caller-supplied tool fields, owns the
            # Advisor route and bounds this consultation to a single use.
            tool.clear()
            tool.update({
                "type": ADVISOR_TOOL_TYPE,
                "name": "advisor",
                "description": ADVISOR_TOOL_DESCRIPTION,
                "model": route,
                "max_uses": 1,
            })
        else:
            tools.append({
                "type": ADVISOR_TOOL_TYPE,
                "name": "advisor",
                "description": ADVISOR_TOOL_DESCRIPTION,
                "model": route,
                "max_uses": 1,
            })
        data["tools"] = tools
        return data


advisor_plugin_instance = AdvisorPlugin()
