"""Expose the saved Advisor as an optional LiteLLM Messages tool."""

from __future__ import annotations

import inspect
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
TARGET_SYSTEM_PROMPT = (
    "During planning and material design, blocker, and acceptance decisions, use "
    "/codex-advisor when an independent view could change the next action.\n"
    "If the skill is missing, install it from https://github.com/stancsz/northstar when able.\n"
    "Keep consultations focused and verify advice yourself."
)
TARGET_CALL_TYPES = frozenset({
    "completion", "acompletion", "responses", "aresponses",
    "anthropic_messages", "aanthropic_messages",
})
SUPPORTED_CALL_TYPES = frozenset({"anthropic_messages", "aanthropic_messages"})
ADVISOR_MODEL_NAMES = {
    "codex-gpt-6.1-sol-advisor": "gpt-6.1-sol",
    "codex-sol-advisor": "gpt-6-sol",
    "codex-astra-advisor": "gpt-6-astra",
    "codex-luna-advisor": "gpt-6-luna",
}
EXPERT_TOOL_ROUTES = {
    **{alias: alias for alias in ADVISOR_MODEL_NAMES},
    "gemini-3.8-flash-advisor": "gemini-3.8-flash-advisor",
    "gemini-4-argon-advisor": "gemini-4-argon-advisor",
    "gemini-subscription": "gemini-3.8-flash-advisor",
    "gemini-subscription-argon": "gemini-4-argon-advisor",
    "claude-opus-5-5-low-advisor": "claude-opus-5-5-low-advisor",
}


def expert_route(advisor_model: str) -> str:
    """Return the fixed private 4040 transport alias for a saved expert."""
    alias = EXPERT_TOOL_ROUTES.get(advisor_model)
    if alias is None:
        raise HTTPException(status_code=400, detail="Saved Advisor is not available on the experts service")
    return f"experts/{alias}"


def _add_target_system_prompt(data: dict, call_type: str) -> None:
    """Add the same short default instruction to each public text protocol."""
    model = data.get("model")
    if (
        call_type not in TARGET_CALL_TYPES
        or not isinstance(model, str)
        or model.startswith("experts/")
        or model.endswith("-advisor")
        or data.get("gateway_image_request") is not None
        or any(
            isinstance(data.get(key), dict) and data[key].get("advisor_sub_call") is True
            for key in ("metadata", "litellm_metadata")
        )
    ):
        return

    if call_type in SUPPORTED_CALL_TYPES:
        system = data.get("system")
        if system is None:
            data["system"] = TARGET_SYSTEM_PROMPT
        elif isinstance(system, str) and TARGET_SYSTEM_PROMPT not in system:
            data["system"] = f"{TARGET_SYSTEM_PROMPT}\n\n{system}"
        elif isinstance(system, list) and not any(
            isinstance(block, dict)
            and isinstance(block.get("text"), str)
            and TARGET_SYSTEM_PROMPT in block["text"]
            for block in system
        ):
            data["system"] = [{"type": "text", "text": TARGET_SYSTEM_PROMPT}, *system]
    elif call_type in {"responses", "aresponses"}:
        instructions = data.get("instructions")
        if instructions is None:
            data["instructions"] = TARGET_SYSTEM_PROMPT
        elif isinstance(instructions, str) and TARGET_SYSTEM_PROMPT not in instructions:
            data["instructions"] = f"{TARGET_SYSTEM_PROMPT}\n\n{instructions}"
    else:
        messages = data.get("messages")
        if isinstance(messages, list) and not any(
            isinstance(message, dict)
            and message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and TARGET_SYSTEM_PROMPT in message["content"]
            for message in messages
        ):
            data["messages"] = [{"role": "system", "content": TARGET_SYSTEM_PROMPT}, *messages]


def _prevent_advisor_replay_on_parent_fallback() -> None:
    """Keep LiteLLM fallbacks from repeating a selected Advisor consultation.

    The pinned interceptor marks expert errors, but leaves later target errors
    unmarked. Preserve those target fallbacks while removing the single-use
    Advisor tool from the fallback request. Retire when native orchestration and
    Router fallback jointly enforce this consultation budget across failures.
    """
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor

    method = Router.async_function_with_fallbacks_common_utils
    try:
        parameters = tuple(inspect.signature(method).parameters.values())
    except (TypeError, ValueError) as error:
        raise RuntimeError("unsupported LiteLLM Router fallback hook signature") from error
    request_kwargs = next((parameter for parameter in parameters if parameter.name == "kwargs"), None)
    if (
        len(parameters) < 2
        or parameters[0].name != "self"
        or parameters[1].name != "e"
        or request_kwargs is None
        or request_kwargs.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ):
        raise RuntimeError("unsupported LiteLLM Router fallback hook signature")

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

    if getattr(method, "_subroute_advisor_fallback_guard", False):
        return

    @wraps(method)
    async def no_parent_replay(self: Router, *args: Any, **kwargs: Any) -> Any:
        exception = args[0] if args else kwargs.get("e")
        if isinstance(exception, Exception):
            if is_advisor_orchestration_failure(exception):
                raise exception
            if getattr(exception, "_subroute_advisor_selected", False):
                try:
                    bound = inspect.signature(method).bind(self, *args, **kwargs)
                except (TypeError, ValueError):
                    raise exception
                request_kwargs = bound.arguments.get("kwargs")
                if not isinstance(request_kwargs, dict):
                    raise exception
                tools = request_kwargs.get("tools")
                if tools is not None:
                    if not isinstance(tools, list):
                        raise exception
                    fallback_tools = [
                        tool for tool in tools
                        if not isinstance(tool, dict) or tool.get("type") != ADVISOR_TOOL_TYPE
                    ]
                    if len(fallback_tools) != len(tools):
                        clean_request_kwargs = dict(request_kwargs)
                        clean_request_kwargs["tools"] = fallback_tools
                        bound.arguments["kwargs"] = clean_request_kwargs
                        return await method(*bound.args, **bound.kwargs)
                return await method(self, *args, **kwargs)
        return await method(self, *args, **kwargs)

    no_parent_replay._subroute_advisor_fallback_guard = True
    Router.async_function_with_fallbacks_common_utils = no_parent_replay


class AdvisorPlugin(CustomLogger):
    """Expose one bounded, model-selected Advisor tool on Messages requests."""

    def __init__(self) -> None:
        super().__init__()
        self._request_advisor_model: ContextVar[str | None] = ContextVar(
            f"subroute_advisor_model_{id(self)}", default=None,
        )
        _prevent_advisor_replay_on_parent_fallback()

    @staticmethod
    def _configured_tools(tools: Any, advisor_model: str) -> list[dict[str, Any]]:
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
        definition = {
            "type": ADVISOR_TOOL_TYPE,
            "name": "advisor",
            "description": ADVISOR_TOOL_DESCRIPTION,
            "model": expert_route(advisor_model),
            "max_uses": 1,
        }
        configured = [dict(tool) if isinstance(tool, dict) else tool for tool in tools]
        advisor_tool = next(
            (tool for tool in configured if isinstance(tool, dict) and tool.get("type") == ADVISOR_TOOL_TYPE),
            None,
        )
        requested_model = advisor_tool.get("model") if advisor_tool is not None else None
        if requested_model is not None:
            if not isinstance(requested_model, str) or requested_model not in EXPERT_TOOL_ROUTES:
                raise HTTPException(status_code=400, detail="Requested Advisor model is not available")
            advisor_model = requested_model
        if advisor_tool is None:
            configured.append(definition)
        else:
            advisor_tool.clear()
            advisor_tool.update(definition)
        return configured

    async def async_pre_request_hook(self, model: str, messages: list, kwargs: dict) -> dict | None:
        """Attach the saved tool after LiteLLM rebuilds its Messages request."""
        advisor_model = self._request_advisor_model.get()
        if advisor_model is None:
            return None
        self._request_advisor_model.set(None)
        request = dict(kwargs)
        request["tools"] = self._configured_tools(request.get("tools"), advisor_model)
        return request

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str,
    ) -> dict | None:
        _add_target_system_prompt(data, call_type)
        if call_type not in SUPPORTED_CALL_TYPES:
            self._request_advisor_model.set(None)
            return None

        _, metadata = get_or_create_metadata_bucket(data)
        if metadata.get("advisor_sub_call") is True:
            self._request_advisor_model.set(None)
            return None
        policy = metadata.get("gateway_policy")
        tools = data.get("tools")
        if tools is None:
            tools = []
        if not isinstance(tools, list):
            self._request_advisor_model.set(None)
            raise HTTPException(status_code=400, detail="Messages tools must be a list")

        advisor_tools = [
            tool for tool in tools
            if isinstance(tool, dict) and tool.get("type") == ADVISOR_TOOL_TYPE
        ]
        if len(advisor_tools) > 1:
            self._request_advisor_model.set(None)
            raise HTTPException(status_code=400, detail="At most one Advisor tool may be declared")
        requested_model = advisor_tools[0].get("model") if advisor_tools else None
        if requested_model is not None:
            if not isinstance(requested_model, str) or requested_model not in EXPERT_TOOL_ROUTES:
                self._request_advisor_model.set(None)
                raise HTTPException(status_code=400, detail="Requested Advisor model is not available")
            advisor_model = requested_model
        else:
            advisor_model = policy.get("advisor_model") if isinstance(policy, dict) else None
        if not advisor_model:
            self._request_advisor_model.set(None)
            if advisor_tools:
                raise HTTPException(status_code=400, detail="Advisor tool requires a configured expert")
            return None
        self._request_advisor_model.set(advisor_model)
        # LiteLLM rebuilds Messages kwargs after this hook. Inject once in
        # async_pre_request_hook, where the installed handler consumes them.
        return None


advisor_plugin_instance = AdvisorPlugin()
