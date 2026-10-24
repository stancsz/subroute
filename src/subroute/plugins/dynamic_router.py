"""Local control plane and request-time model alias resolution."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import Depends, HTTPException, Request
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.custom_llm import CustomLLMError
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket, get_metadata_variable_name_from_kwargs
from litellm.proxy.proxy_server import app
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from pydantic import BaseModel, Field, ValidationError, field_validator
from subroute.ui_control import register_ui_routes, source_configuration


RoutingMode = Literal["alias", "force", "off"]
CodexSearchResponseLength = Literal["short", "medium", "long"]
# Small monotonic per-query bounds: concise results for short, default-like
# coverage for medium, and a still-bounded larger set for long.
CODEX_SEARCH_MAX_RESULTS = {"short": 5, "medium": 10, "long": 20}
VIRTUAL_ALIASES = frozenset({"default", "auto"})
DEFAULT_TARGET_MODEL = "codex-luna"
FALLBACK_CALL_TYPES = frozenset({
    "completion", "acompletion", "responses", "aresponses",
    "anthropic_messages", "aanthropic_messages",
})
MEDIA_CONTENT_TYPES = frozenset({
    "image", "image_url", "input_image", "audio", "audio_url", "input_audio",
    "video", "video_url", "input_video", "file", "input_file",
})
MINIMAX_FLASH_PROVIDER_MODEL = "openrouter/minimax/minimax-m3.1-flash"
MINIMAX_FLASH_DIRECT_PROVIDER_MODEL = "minimax/MiniMax-M3.1-Flash"
MEDIA_CONTENT_KEYS = frozenset({
    "image_url", "input_image", "audio_url", "input_audio", "video_url",
    "input_video", "file_id", "file_data",
})
RETIRED_MODEL_ALIASES = {
    "openai-guided": "openai", "openrouter-guided": "minimax/minimax-m3.1-flash", "minimax-guided": "minimax",
    "codex-terra": "codex-subscription", "codex-reserve": "codex-luna",
    "openrouter": "minimax/minimax-m3.1-flash",
    "openrouter-flash": "minimax/minimax-m3.1-flash",
    "minimax-flash": "minimax/minimax-m3.1-flash",
}
RESPONSES_PRIVATE_CODEX_MODELS = frozenset({"codex-luna", "codex-gpt-6.1-sol"})
ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger(__name__)


def _fallback_eligible(data: dict[str, Any], call_type: str) -> bool:
    """Restrict the generic text-model chain to text-only chat protocols."""
    if call_type not in FALLBACK_CALL_TYPES:
        return False
    model = data.get("model")
    if isinstance(model, str) and model in {
        "minimax-tts", "mimo-v2.5-asr", "mimo-v2.5-tts",
        "mimo-v2.5-tts-voiceclone", "mimo-v2.5-tts-voicedesign",
    }:
        return False

    def contains_media(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("type") in MEDIA_CONTENT_TYPES or MEDIA_CONTENT_KEYS.intersection(value):
                return True
            return any(contains_media(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_media(item) for item in value)
        return False

    return not contains_media(data.get("messages")) and not contains_media(data.get("input"))


def _requested_model_name(request_data: dict[str, Any]) -> str | None:
    # LiteLLM metadata is server-owned; top-level metadata may be client input.
    for key in ("litellm_metadata", "metadata"):
        metadata = request_data.get(key)
        if isinstance(metadata, dict):
            routing = metadata.get("routing")
            if isinstance(routing, dict) and isinstance(routing.get("requested_model"), str):
                return routing["requested_model"]
    requested = request_data.get("model")
    return requested if isinstance(requested, str) else None


def _stamp_requested_model(response: Any, requested_model: str | None) -> None:
    """Keep fallback provider resolution out of the client-visible model field."""
    if not requested_model:
        return
    from litellm.proxy.utils import _UpstreamStreamBoundary
    from litellm.router import FallbackAwareAnthropicMessagesStream
    from litellm.llms.anthropic.pass_through.adapters.streaming_iterator import AnthropicSSEStream

    if isinstance(response, _UpstreamStreamBoundary):
        _stamp_requested_model(response._source, requested_model)
        return
    if isinstance(response, FallbackAwareAnthropicMessagesStream):
        _stamp_requested_model(response._source_iterator, requested_model)
        return
    if isinstance(response, AnthropicSSEStream):
        _stamp_requested_model(response._anthropic_wrapper, requested_model)
        return
    if isinstance(response, dict):
        if "model" in response:
            response["model"] = requested_model
        nested = response.get("response")
        message = response.get("message")
        if isinstance(message, dict) and "model" in message:
            message["model"] = requested_model
    else:
        model_descriptor = getattr(type(response), "model", None)
        if hasattr(response, "model") and not (isinstance(model_descriptor, property) and model_descriptor.fset is None):
            response.model = requested_model
        nested = getattr(response, "response", None)
    if isinstance(nested, dict):
        if "model" in nested:
            nested["model"] = requested_model
    elif nested is not None:
        _stamp_requested_model(nested, requested_model)


@dataclass(frozen=True)
class RoutingState:
    active_model: str
    mode: RoutingMode = "alias"
    policy_version: int = 1
    advisor_model: str | None = "codex-gpt-6.1-sol-advisor"
    reasoning_effort: str | None = None
    fast_mode: bool = False


@dataclass(frozen=True)
class ModelChoice:
    model_id: str
    display_name: str
    capabilities: tuple[str, ...]
    selectable: bool = True
    advisor_selectable: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    provider: str = "Other"
    source_id: str = ""
    access: str = "Configured route"
    reasoning_note: str = ""


class RoutingUpdate(BaseModel):
    model: str
    mode: RoutingMode
    reasoning_effort: str | None = None
    fast_mode: bool | None = None


class AdvisorUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    advisor_model: str


class CodexSearchQuery(BaseModel):
    model_config = {"extra": "forbid"}

    q: str = Field(min_length=1, max_length=2048)
    recency: int | None = Field(default=None, ge=0)
    domains: list[str] | None = None

    @field_validator("q")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("search query must not be blank")
        return value


class CodexSearchCommands(BaseModel):
    model_config = {"extra": "allow"}

    search_query: list[CodexSearchQuery] | None = None
    response_length: CodexSearchResponseLength | None = None


class CodexSearchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    model: str
    reasoning: dict[str, Any] | None = None
    commands: CodexSearchCommands | None = None
    input: str | list[dict[str, Any]] | None = None
    settings: dict[str, Any] | None = None
    max_output_tokens: int | None = None


def _codex_search_error(status_code: int, message: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "message": message,
                "type": "invalid_request_error" if status_code < 500 else "server_error",
                "param": "commands",
                "code": code,
            }
        },
    )


async def codex_search(
    payload: CodexSearchRequest,
    _user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> dict[str, Any]:
    """Implement Codex CLI web search through LiteLLM's configured SearXNG tool."""
    commands = payload.commands
    if commands is None or not commands.model_fields_set:
        raise _codex_search_error(400, "At least one search command is required.", "missing_commands")

    unsupported = [name for name, value in commands.model_extra.items() if value is not None]
    if unsupported:
        raise _codex_search_error(
            400, f"Unsupported Codex search command: {unsupported[0]}", "unsupported_operation"
        )
    queries = commands.search_query
    if not queries:
        raise _codex_search_error(400, "Only commands.search_query is supported.", "unsupported_operation")

    if any(query.recency is not None for query in queries):
        raise _codex_search_error(400, "Search recency filters are not supported.", "unsupported_operation")

    if len(queries) > 8:
        raise _codex_search_error(400, "At most 8 search queries are supported.", "invalid_request")

    try:
        import litellm
        from litellm.proxy.proxy_server import llm_router

        configured_tool = next(
            (
                tool for tool in getattr(llm_router, "search_tools", [])
                if tool.get("search_tool_name") == "subroute-searxng"
            ),
            None,
        )
        if configured_tool is None:
            raise LookupError("configured search tool is unavailable")
        search_params = dict(configured_tool.get("litellm_params") or {})
        provider = search_params.pop("search_provider", None)
        if provider != "searxng":
            raise LookupError("configured search provider is unavailable")

        results = []
        for query in queries:
            # Keep the existing provider default when the CLI omits this
            # field; explicit lengths bound results monotonically per query.
            max_results = (
                CODEX_SEARCH_MAX_RESULTS[commands.response_length]
                if commands.response_length is not None
                else None
            )
            search_options = dict(search_params)
            if max_results is not None:
                search_options["max_results"] = max_results
            response = await litellm.asearch(
                query=query.q,
                search_provider=provider,
                **search_options,
            )
            query_result_count = 0
            for result in response.results:
                item = result.model_dump(exclude_none=True)
                # Codex domains are an allow-list. Apply it locally because
                # LiteLLM's SearXNG adapter does not implement domain filters.
                if query.domains:
                    from urllib.parse import urlsplit

                    host = urlsplit(item.get("url", "")).hostname or ""
                    if not any(
                        host == domain or host.endswith(f".{domain}")
                        for domain in query.domains
                    ):
                        continue
                results.append(item)
                query_result_count += 1
                if max_results is not None and query_result_count >= max_results:
                    break
    except Exception as exc:
        logger.warning("Codex search failed (%s)", type(exc).__name__)
        raise _codex_search_error(502, "Configured web search failed.", "search_failed") from exc

    if not results:
        # An empty aggregate is a failed search, not a successful one: Codex
        # clients would otherwise treat "no evidence" as a verified answer.
        logger.warning("Codex search returned no usable results")
        raise _codex_search_error(
            502, "Configured web search returned no usable results.", "empty_results"
        )

    output = "\n".join(
        f"{index}. {item.get('title', '')}\n{item.get('url', '')}\n{item.get('snippet', '')}"
        for index, item in enumerate(results, start=1)
    )
    return {"output": output, "results": results}


def _load_choices(config_path: Path) -> tuple[ModelChoice, ...]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    choices: list[ModelChoice] = []
    seen: set[str] = set()
    for deployment in raw.get("model_list", []):
        model_id = deployment.get("model_name")
        info = deployment.get("model_info") or {}
        if (
            not isinstance(model_id, str)
            or not model_id
            or model_id in seen
            or model_id in VIRTUAL_ALIASES
            or (info.get("selectable", True) is False and info.get("advisor_selectable") is not True)
        ):
            continue
        raw_capabilities = info.get("capabilities") or []
        if not isinstance(raw_capabilities, list):
            raise ValueError(f"capabilities for {model_id!r} must be a list")
        capabilities = tuple(
            str(value) for value in raw_capabilities if isinstance(value, str)
        )
        efforts = info.get("reasoning_efforts") or []
        if not isinstance(efforts, list) or not all(isinstance(effort, str) for effort in efforts):
            raise ValueError(f"reasoning_efforts for {model_id!r} must be a list of strings")
        choices.append(
            ModelChoice(
                model_id=model_id,
                display_name=str(info.get("display_name") or model_id),
                capabilities=capabilities,
                selectable=info.get("selectable", True) is not False,
                advisor_selectable=info.get("advisor_selectable") is True,
                reasoning_efforts=tuple(efforts),
                provider=str(info.get("provider") or "Other"),
                source_id=str(info.get("source_id") or ""),
                access=str(info.get("access") or "Configured route"),
                reasoning_note=str(info.get("reasoning_note") or ""),
            )
        )
        seen.add(model_id)
    if not choices:
        raise ValueError("litellm config has no selectable physical models")
    return tuple(choices)


class RoutingControlPlane:
    """Own a validated in-memory snapshot backed by an atomic JSON file."""

    def __init__(self, config_path: Path, state_path: Path) -> None:
        self.config_path = config_path
        self.state_path = state_path
        self._lock = threading.RLock()
        self.choices = _load_choices(config_path)
        self.allowed_models = frozenset(choice.model_id for choice in self.choices if choice.selectable)
        self.advisor_models = frozenset(choice.model_id for choice in self.choices if choice.advisor_selectable)
        self._state = self._load_or_create_state()

    def _load_or_create_state(self) -> RoutingState:
        if not self.state_path.exists():
            fallback_model = (
                DEFAULT_TARGET_MODEL
                if DEFAULT_TARGET_MODEL in self.allowed_models
                else "minimax/minimax-m3.1-flash"
                if "minimax/minimax-m3.1-flash" in self.allowed_models
                else self.choices[0].model_id
            )
            initial_model = os.getenv("ACTIVE_MODEL", fallback_model)
            initial_model = RETIRED_MODEL_ALIASES.get(initial_model, initial_model)
            advisor_model = os.getenv("ADVISOR_MODEL", "codex-gpt-6.1-sol-advisor")
            state = RoutingState(active_model=initial_model, advisor_model=advisor_model)
            self._validate(state)
            self._write_atomic(state)
            return state
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        saved_advisor_model = raw.get("advisor_model")
        advisor_model = saved_advisor_model or os.getenv(
            "ADVISOR_MODEL", "codex-gpt-6.1-sol-advisor"
        )
        state = RoutingState(
            active_model=raw["active_model"],
            mode=raw.get("mode", "alias"),
            policy_version=int(raw.get("policy_version", 1)),
            advisor_model=advisor_model,
            reasoning_effort=raw.get("reasoning_effort"),
            fast_mode=raw.get("fast_mode", False),
        )
        replacement = RETIRED_MODEL_ALIASES.get(state.active_model)
        routed_model = replacement or state.active_model
        active_choice = next(
            (choice for choice in self.choices if choice.model_id == routed_model), None
        )
        unsupported_effort = (
            state.reasoning_effort is not None
            and (active_choice is None or state.reasoning_effort not in active_choice.reasoning_efforts)
        )
        migrated = advisor_model != saved_advisor_model or unsupported_effort or any(
            key in raw for key in (
                "advisor_reasoning_effort", "advisor_trigger", "advisor_sensitivity", "advisor_classifier",
            )
        )
        if replacement or migrated:
            state = replace(state, active_model=replacement or state.active_model,
                            reasoning_effort=None if unsupported_effort else state.reasoning_effort,
                            policy_version=state.policy_version + 1)
            self._validate(state)
            self._write_atomic(state)
        else:
            self._validate(state)
        return state

    def _validate(self, state: RoutingState) -> None:
        if state.active_model not in self.allowed_models:
            raise ValueError(f"active model is not selectable: {state.active_model!r}")
        if state.mode not in {"alias", "force", "off"}:
            raise ValueError(f"invalid routing mode: {state.mode!r}")
        if state.policy_version < 1:
            raise ValueError("policy_version must be positive")
        if not isinstance(state.fast_mode, bool):
            raise ValueError("fast_mode must be a boolean")
        active_choice = next(choice for choice in self.choices if choice.model_id == state.active_model)
        if state.fast_mode and active_choice.source_id != "openai-subscription":
            raise ValueError("Fast mode requires an OpenAI subscription target")
        if state.advisor_model is None:
            raise ValueError("an advisor model must remain selected")
        if state.advisor_model not in self.advisor_models:
            raise ValueError(f"advisor model is not selectable: {state.advisor_model!r}")
        if state.reasoning_effort is not None and state.reasoning_effort not in active_choice.reasoning_efforts:
            raise ValueError(f"reasoning effort {state.reasoning_effort!r} is not supported by {state.active_model!r}")

    def _write_atomic(self, state: RoutingState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            dir=self.state_path.parent,
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            text=True,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(asdict(state), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.state_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def snapshot(self) -> RoutingState:
        with self._lock:
            return self._state

    def update(self, model: str, mode: RoutingMode, **settings: Any) -> RoutingState:
        with self._lock:
            candidate = replace(
                self._state,
                active_model=model,
                mode=mode,
                policy_version=self._state.policy_version + 1,
                reasoning_effort=settings.get("reasoning_effort", self._state.reasoning_effort if model == self._state.active_model else None),
                fast_mode=settings.get("fast_mode", self._state.fast_mode if model == self._state.active_model else False),
            )
            self._validate(candidate)
            self._write_atomic(candidate)
            self._state = candidate
            return candidate

    def update_advisor(self, advisor_model: str) -> RoutingState:
        with self._lock:
            candidate = replace(
                self._state,
                advisor_model=advisor_model,
                policy_version=self._state.policy_version + 1,
            )
            self._validate(candidate)
            self._write_atomic(candidate)
            self._state = candidate
            return candidate

    def resolve(self, requested_model: str | None) -> tuple[str | None, RoutingState]:
        state = self.snapshot()
        # `default` is an explicit client request for the saved target, even
        # when the ordinary alias policy is off. It lets clients follow the
        # control desk without synchronizing model IDs themselves.
        if requested_model == "default":
            return state.active_model, state
        # `auto` is a LiteLLM model group with an explicit provider fallback
        # chain, not another spelling of the saved control-desk selection.
        if requested_model == "auto":
            return requested_model, state
        if state.mode == "off":
            return requested_model, state
        if state.mode == "force" or (
            state.mode == "alias" and requested_model in VIRTUAL_ALIASES
        ):
            return state.active_model, state
        return requested_model, state


def _default_control_plane() -> RoutingControlPlane:
    config_path = Path(os.getenv("LITELLM_CONFIG_PATH", ROOT / "config/litellm.yaml"))
    state_path = Path(
        os.getenv("ACTIVE_MODEL_STATE_PATH", ROOT / "config/active_model.json")
    )
    return RoutingControlPlane(config_path, state_path)


def _install_responses_fallback_boundary() -> None:
    """Keep native failover, but never replay an exposed Responses stream.

    LiteLLM 1.106.2 protects Chat output, while its Responses fallback helper
    still synthesizes continuation instructions after emitted output. There
    is no native setting for this boundary. Retire this small guard when the
    native Responses helper exposes the same pre-output-only policy.
    """
    from litellm import Router

    if getattr(Router, "_subroute_responses_pre_output_only", False):
        return
    original = Router._aresponses_fallback_attempt

    async def pre_output_only(self, e, *args, **kwargs):
        if not e.is_pre_first_chunk:
            if e.original_exception is not None:
                raise e.original_exception from e
            raise e
        async for event in original(self, e, *args, **kwargs):
            yield event

    Router._aresponses_fallback_attempt = pre_output_only
    Router._subroute_responses_pre_output_only = True


class DynamicRoutingPlugin(CustomLogger):
    def __init__(self, control_plane: RoutingControlPlane) -> None:
        super().__init__()
        from subroute.token_counting import install_tool_schema_counting_fix
        install_tool_schema_counting_fix()
        from subroute.thinking import install_messages_tool_ordering
        install_messages_tool_ordering()
        from subroute.responses_stream import install_responses_stream_repair
        install_responses_stream_repair()
        from subroute.usage_accounting import install_stream_accounting
        install_stream_accounting()
        _install_responses_fallback_boundary()
        self.control_plane = control_plane

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        from subroute.thinking import repair_thinking_starts
        # LiteLLM's Responses bridge emits text deltas but drops the caption
        # from the final snapshot when the same Chat choice contains images.
        # Preserve already-emitted items only on our hosted-image path.
        context = request_data.get("gateway_image_request") or {}
        repair = context.get("protocol") in {"responses", "aresponses"}
        requested_model = _requested_model_name(request_data)
        completed_items = {}
        _stamp_requested_model(response, requested_model)
        async for event in repair_thinking_starts(response):
            _stamp_requested_model(event, requested_model)
            if repair:
                kind = getattr(event, "type", None)
                if kind == "response.output_item.done":
                    item = event.item
                    item = item if isinstance(item, dict) else item.model_dump(exclude_none=True)
                    completed_items[item["id"]] = item
                elif kind == "response.completed":
                    output = event.response.output
                    ids = {item.get("id") if isinstance(item, dict) else item.id for item in output}
                    event.response.output = [item for key, item in completed_items.items() if key not in ids] + list(output)
            yield event

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        # LiteLLM normally reports the resolved fallback model. Keep the
        # client-visible model stable; fallback attempt metadata remains intact.
        _stamp_requested_model(response, _requested_model_name(data))
        return response

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> dict | None:
        original_requested_model = data.get("model")
        requested_model = (
            RETIRED_MODEL_ALIASES.get(original_requested_model, original_requested_model)
            if isinstance(original_requested_model, str) else original_requested_model
        )
        if requested_model != data.get("model"):
            data["model"] = requested_model
        # This request snapshot is server-owned, never a client override.
        data.pop("gateway_image_request", None)
        if isinstance(data.get("extra_body"), dict):
            data["extra_body"].pop("gateway_image_request", None)
        if call_type in {"aresponses", "responses"}:
            data.setdefault("litellm_metadata", {})
        _, metadata = get_or_create_metadata_bucket(data)
        # Keep the caller-visible identity in server-owned metadata before
        # any routing branch can rewrite data["model"]. Never trust a client
        # supplied routing.requested_model value.
        metadata["routing"] = {"requested_model": original_requested_model}
        if not _fallback_eligible(data, call_type):
            # The global chain is for text-only chat. Preserve media and
            # specialized endpoint semantics by keeping those requests local
            # to their explicitly selected provider route.
            data["disable_fallbacks"] = True
        from subroute.plugins.image_intent import image_request_context
        from subroute.audio import audio_route

        try:
            has_audio = audio_route(data, call_type)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        if has_audio:
            data["disable_fallbacks"] = True
            if requested_model not in self.control_plane.allowed_models | VIRTUAL_ALIASES:
                raise HTTPException(404, detail="unknown model")
            _, metadata = get_or_create_metadata_bucket(data)
            state = self.control_plane.snapshot()
            metadata["gateway_policy"] = {**asdict(state), "advisor_model": None}
            metadata["gateway_reasoning_effort"] = None
            metadata["gateway_target_fast_mode"] = False
            # MiMo's ASR model accepts input_audio on its own Chat Completions
            # route. Preserve that explicit model choice; other audio requests
            # use the existing Gemini subscription route below.
            resolved_model = requested_model if requested_model == "mimo-v2.5-asr" else "gemini-subscription"
            metadata["routing"] = {
                "requested_model": original_requested_model, "resolved_model": resolved_model,
                "mode": "audio_input" if resolved_model == "gemini-subscription" else "speech_recognition",
                "policy_version": state.policy_version,
            }
            data["model"] = resolved_model
            return data

        mimo_tts_models = {
            "mimo-v2.5-tts", "mimo-v2.5-tts-voiceclone", "mimo-v2.5-tts-voicedesign",
        }
        is_speech_output = (
            (call_type == "aspeech" and requested_model == "minimax-tts")
            or (call_type in {"completion", "acompletion"} and requested_model in mimo_tts_models)
        )
        if is_speech_output:
            data["disable_fallbacks"] = True
            _, metadata = get_or_create_metadata_bucket(data)
            state = self.control_plane.snapshot()
            metadata["gateway_policy"] = {**asdict(state), "advisor_model": None}
            metadata["gateway_reasoning_effort"] = None
            metadata["gateway_target_fast_mode"] = False
            metadata["routing"] = {
                "requested_model": original_requested_model, "resolved_model": requested_model,
                "mode": "speech_synthesis", "policy_version": state.policy_version,
            }
            return data

        try:
            image_context = image_request_context(data, call_type)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        if image_context is not None:
            data["disable_fallbacks"] = True
            _, metadata = get_or_create_metadata_bucket(data)
            state = self.control_plane.snapshot()
            metadata["gateway_policy"] = {**asdict(state), "advisor_model": None}
            metadata["gateway_reasoning_effort"] = None
            metadata["gateway_target_fast_mode"] = False
            metadata["routing"] = {
                "requested_model": original_requested_model, "resolved_model": "codex-luna",
                "mode": image_context["reason"], "policy_version": state.policy_version,
            }
            data["model"] = "codex-luna"
            data["gateway_image_request"] = image_context
            return data
        if call_type in {"image_generation", "aimage_generation"}:
            data["disable_fallbacks"] = True
            from subroute.handlers.codex_images import IMAGE_MODEL, image_options

            try:
                options = image_options(data)
            except CustomLLMError as exc:
                raise HTTPException(exc.status_code, detail=str(exc)) from exc
            _, metadata = get_or_create_metadata_bucket(data)
            state = self.control_plane.snapshot()
            metadata["gateway_policy"] = {**asdict(state), "advisor_model": None}
            metadata["gateway_reasoning_effort"] = None
            metadata["gateway_target_fast_mode"] = False
            metadata["routing"] = {
                "requested_model": original_requested_model, "resolved_model": IMAGE_MODEL,
                "mode": "image_generation", "policy_version": state.policy_version,
            }
            data["model"] = IMAGE_MODEL
            data["image_generation_options"] = options
            return data
        _, metadata = get_or_create_metadata_bucket(data)

        if not isinstance(requested_model, str) or (
            requested_model not in self.control_plane.allowed_models
            and requested_model not in VIRTUAL_ALIASES
            and requested_model != "default"
        ):
            raise HTTPException(status_code=404, detail="unknown model")

        resolved_model, state = self.control_plane.resolve(requested_model)
        # The advisor hook consumes this exact request snapshot, never a second
        # read of mutable global policy. Overwrite any client-supplied value.
        metadata["gateway_policy"] = asdict(state)
        # Capture the override once. Deployment hooks also run for nested
        # LiteLLM translations, so they must never read mutable policy again.
        routed = requested_model == "default" or (requested_model != "auto" and (
            state.mode == "force"
            or (state.mode == "alias" and requested_model in VIRTUAL_ALIASES)
        ))
        metadata["gateway_reasoning_effort"] = state.reasoning_effort if routed else None
        metadata["gateway_target_fast_mode"] = bool(routed and state.fast_mode)
        private_responses_tools = (
            call_type in {"responses", "aresponses"}
            and isinstance(data.get("input"), list)
            and any(isinstance(item, dict) and item.get("type") == "additional_tools"
                    for item in data["input"])
        )
        if private_responses_tools:
            # These private Responses input items are not supported by the
            # OpenRouter/MiMo chat bridges. Keep the complete request on a
            # native Codex Responses deployment and constrain fallback to
            # the other configured Codex deployment only.
            if resolved_model not in RESPONSES_PRIVATE_CODEX_MODELS:
                resolved_model = "codex-luna"
            alternate = "codex-gpt-6.1-sol" if resolved_model == "codex-luna" else "codex-luna"
            data["fallbacks"] = [{resolved_model: [alternate]}]
            data["max_fallbacks"] = 1
            data["disable_fallbacks"] = False
            metadata["routing"] = {
                "requested_model": original_requested_model,
                "resolved_model": resolved_model,
                "mode": "responses_private_tools_native_codex",
                "compatibility_reason": "additional_tools_requires_native_codex_responses",
                "policy_version": state.policy_version,
            }
            data["model"] = resolved_model
            logger.info("Responses compatibility route requested=%s resolved=%s fallback=%s",
                        requested_model, resolved_model, alternate)
            return data
        if resolved_model == "qwen3.8-27b":
            choice = data.get("tool_choice")
            named_tool_choice = (
                isinstance(choice, dict)
                and ((choice.get("type") == "function"
                      and isinstance(choice.get("function"), dict)
                      and isinstance(choice["function"].get("name"), str)
                      and bool(choice["function"]["name"]))
                     or (choice.get("type") in ("tool", "function")
                         and isinstance(choice.get("name"), str)
                         and bool(choice["name"])))
            )
            output_limits = [data[name] for name in (
                "max_tokens", "max_completion_tokens", "max_output_tokens",
            ) if name in data and data[name] is not None]
            valid_limits = all(type(value) is int and value > 0 for value in output_limits)
            output_over_local_cap = False
            if valid_limits and output_limits:
                try:
                    from litellm.proxy import proxy_server

                    router = proxy_server.llm_router
                    _, local_output_cap = router.get_configured_token_limits(resolved_model) \
                        if router is not None else (None, None)
                    output_over_local_cap = (
                        type(local_output_cap) is int and local_output_cap > 0
                        and max(output_limits) > local_output_cap
                    )
                except Exception as error:
                    logger.warning("qwen_output_compatibility_check_unavailable reason=%s",
                                   type(error).__name__)
            if named_tool_choice or output_over_local_cap:
                reason = ("named_tool_choice" if named_tool_choice
                          else "output_limit_exceeds_local_cap")
                resolved_model = "mimo-v2.6-pro"
                metadata["routing"] = {
                    "requested_model": original_requested_model,
                    "resolved_model": resolved_model,
                    "mode": "qwen_native_compatibility",
                    "compatibility_reason": reason,
                    "policy_version": state.policy_version,
                }
                data["model"] = resolved_model
                logger.info("Qwen compatibility route requested=%s resolved=%s reason=%s",
                            requested_model, resolved_model, reason)
                return data
        if resolved_model == "qwen3.8-27b":
            from subroute.local_context import frontload_anthropic_system_messages

            data = frontload_anthropic_system_messages(data, call_type)
        if resolved_model == requested_model:
            if original_requested_model != requested_model:
                metadata["routing"] = {
                    "requested_model": original_requested_model,
                    "resolved_model": resolved_model,
                    "mode": state.mode,
                    "policy_version": state.policy_version,
                }
            return data

        metadata["routing"] = {
            "requested_model": original_requested_model,
            "resolved_model": resolved_model,
            "mode": state.mode,
            "policy_version": state.policy_version,
        }
        data["model"] = resolved_model
        logger.debug(
            "resolved model alias requested=%s resolved=%s mode=%s policy_version=%s",
            requested_model,
            resolved_model,
            state.mode,
            state.policy_version,
        )
        return data

    async def async_pre_call_deployment_hook(self, kwargs: dict[str, Any], call_type: Any) -> dict | None:
        metadata = kwargs.get(get_metadata_variable_name_from_kwargs(kwargs)) or {}
        effort = metadata.get("gateway_reasoning_effort")
        fast_mode = metadata.get("gateway_target_fast_mode") is True
        is_minimax_flash = kwargs.get("model") == MINIMAX_FLASH_PROVIDER_MODEL
        is_minimax_flash_direct = kwargs.get("model") == MINIMAX_FLASH_DIRECT_PROVIDER_MODEL
        if is_minimax_flash and call_type in {
            "aresponses", "responses", "anthropic_messages", "aanthropic_messages",
            "acompletion", "completion",
        }:
            if any(
                kwargs.get(key) is not None and not isinstance(kwargs.get(key), dict)
                for key in ("reasoning", "output_config")
            ):
                return None
            if effort is None:
                effort = self._minimax_flash_caller_effort(kwargs)
            if effort is None:
                effort = self._minimax_flash_default_effort(kwargs)
            if effort is not None:
                # OpenRouter's pinned LiteLLM cost metadata rejects
                # reasoning_effort for this model. Its native reasoning field
                # also preserves MiniMax's distinct `max` level.
                updated = kwargs.copy()
                requested_effort = kwargs.get("reasoning_effort")
                requested = kwargs.get("reasoning")
                updated["reasoning"] = {
                    **(requested_effort if isinstance(requested_effort, dict) else {}),
                    **(requested if isinstance(requested, dict) else {}),
                    "effort": effort,
                }
                updated.pop("reasoning_effort", None)
                return updated
        if is_minimax_flash_direct and call_type in {
            "aresponses", "responses", "anthropic_messages", "aanthropic_messages",
            "acompletion", "completion",
        }:
            if any(
                kwargs.get(key) is not None and not isinstance(kwargs.get(key), dict)
                for key in ("reasoning", "output_config")
            ):
                return None
            if effort is None:
                effort = self._minimax_flash_caller_effort(kwargs)
            if effort is None:
                effort = self._minimax_flash_default_effort(kwargs)
        if effort is None and not fast_mode:
            return None
        updated = kwargs.copy()
        if fast_mode and str(kwargs.get("api_base", "")).rstrip("/") == "https://chatgpt.com/backend-api/codex":
            updated["service_tier"] = "fast"
        if effort is None:
            return updated if updated != kwargs else None
        if call_type in {"aresponses", "responses"}:
            updated["reasoning"] = {**(kwargs.get("reasoning") or {}), "effort": effort}
        elif call_type in {"anthropic_messages", "aanthropic_messages"}:
            # LiteLLM 1.101.0 natively maps adaptive thinking/output_config to
            # Responses or Chat reasoning effort. Keep translation in LiteLLM.
            updated["thinking"] = {"type": "adaptive"}
            updated["output_config"] = {**(kwargs.get("output_config") or {}), "effort": effort}
        elif call_type in {"acompletion", "completion"}:
            requested = kwargs.get("reasoning_effort")
            updated["reasoning_effort"] = {**requested, "effort": effort} if isinstance(requested, dict) else effort
        else:
            return None
        return updated

    @staticmethod
    def _minimax_flash_caller_effort(kwargs: dict[str, Any]) -> str | None:
        reasoning_effort = kwargs.get("reasoning_effort")
        if isinstance(reasoning_effort, str):
            return reasoning_effort
        if isinstance(reasoning_effort, dict) and isinstance(reasoning_effort.get("effort"), str):
            return reasoning_effort["effort"]
        for carrier in (kwargs.get("reasoning"), kwargs.get("output_config")):
            if isinstance(carrier, dict) and isinstance(carrier.get("effort"), str):
                return carrier["effort"]
        return None

    @staticmethod
    def _minimax_flash_default_effort(kwargs: dict[str, Any]) -> str | None:
        """Default Flash effort only when the caller supplied no effort or budget."""
        reasoning = kwargs.get("reasoning")
        output_config = kwargs.get("output_config")
        thinking = kwargs.get("thinking")
        reasoning_effort = kwargs.get("reasoning_effort")
        if reasoning_effort is not None:
            return None
        if isinstance(reasoning, dict) and any(
            key in reasoning for key in ("effort", "max_tokens", "budget_tokens")
        ):
            return None
        if isinstance(output_config, dict) and any(
            key in output_config for key in ("effort", "budget_tokens", "max_tokens")
        ):
            return None
        if thinking is not None:
            return None
        return "medium"


def _require_local_control_request(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(client_host)
        allow_private = os.getenv("ALLOW_PRIVATE_CONTROL", "0") in ("1", "true", "True")
        if not (ip.is_loopback or (allow_private and ip.is_private)):
            raise HTTPException(status_code=403, detail="control plane is loopback-only")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="control plane is loopback-only") from exc

    origin = request.headers.get("origin")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin and origin.rstrip("/") != expected_origin.rstrip("/"):
        raise HTTPException(status_code=403, detail="cross-origin control is forbidden")


control_plane = _default_control_plane()
dynamic_routing_plugin = DynamicRoutingPlugin(control_plane)


async def _unsupported_operation(request: Request):
    from fastapi.responses import JSONResponse

    operation = request.scope["subroute_unsupported_operation"]
    return JSONResponse(
        status_code=400,
        content={"error": {
            "message": f"No configured model supports {operation}.",
            "type": "invalid_request_error",
            "param": "model",
            "code": "unsupported_operation",
        }},
    )


def _register_unconfigured_operation_routes() -> None:
    """Shadow unsupported LiteLLM operations before their generic handlers.

    LiteLLM 1.103.0 routes custom provider names through its provider enum in
    skills/rerank and fans embeddings across chat-only deployments, surfacing
    internal failures as HTTP 500. The configured model metadata advertises
    none of these operations. These exact route guards return a caller error
    until a deployment advertises the capability; LiteLLM keeps ownership of
    all supported protocols and requests.
    """
    guards = (
        ("/v1/skills", {"GET", "POST"}, "skills"),
        ("/v1/skills/{skill_id}", {"GET", "DELETE"}, "skills"),
        ("/v1/rerank", {"POST"}, "rerank"),
        ("/v1/embeddings", {"POST"}, "embeddings"),
    )
    for path, methods, operation in guards:
        if any(
            getattr(route, "path", None) == path
            and methods.issubset(getattr(route, "methods", set()) or set())
            and getattr(route, "name", None) == f"subroute_unsupported_{operation}"
            for route in app.router.routes
        ):
            continue

        def make_endpoint(unsupported_operation: str):
            async def endpoint(request: Request):
                request.scope["subroute_unsupported_operation"] = unsupported_operation
                return await _unsupported_operation(request)

            return endpoint

        app.add_api_route(
            path,
            make_endpoint(operation),
            methods=methods,
            name=f"subroute_unsupported_{operation}",
            include_in_schema=False,
        )
        app.router.routes.insert(0, app.router.routes.pop())


_register_unconfigured_operation_routes()


async def active_model_state(request: Request) -> dict[str, Any]:
    _require_local_control_request(request)
    return asdict(control_plane.snapshot())


async def routing_options(request: Request) -> dict[str, Any]:
    """Presentation data for local control clients, never the request data path."""
    _require_local_control_request(request)
    configured = {source["id"]: source["configured"] for source in source_configuration()}
    choices = [{**asdict(choice), "configured": bool(configured.get(choice.source_id, False))} for choice in control_plane.choices]
    return {
        "state": asdict(control_plane.snapshot()),
        "models": [choice for choice in choices if choice["selectable"]],
        "advisor_models": [choice for choice in choices if choice["advisor_selectable"]],
        "client_api_key_required": bool(os.getenv("GATEWAY_MASTER_KEY")),
    }


async def update_active_model(request: Request) -> dict[str, Any]:
    _require_local_control_request(request)
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application/json is required")
    try:
        update = RoutingUpdate.model_validate(await request.json())
        settings = update.model_dump(include={"reasoning_effort", "fast_mode"}, exclude_unset=True)
        return asdict(control_plane.update(update.model, update.mode, **settings))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="invalid routing update") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def advisor_model_state(request: Request) -> dict[str, Any]:
    _require_local_control_request(request)
    return asdict(control_plane.snapshot())


async def update_advisor_model(request: Request) -> dict[str, Any]:
    _require_local_control_request(request)
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="application/json is required")
    try:
        update = AdvisorUpdate.model_validate(await request.json())
        return asdict(control_plane.update_advisor(update.advisor_model))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="invalid advisor update") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _register_routes() -> None:
    existing = {
        (getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", None) or [])))
        for route in app.routes
    }
    routes = (
        ("/api/active-model", active_model_state, ["GET"], None),
        ("/api/active-model", update_active_model, ["POST"], None),
        ("/api/routing-options", routing_options, ["GET"], None),
        ("/api/advisor-model", advisor_model_state, ["GET"], None),
        ("/api/advisor-model", update_advisor_model, ["POST"], None),
    )
    for path, endpoint, methods, response_class in routes:
        key = (path, tuple(sorted(methods)))
        if key not in existing:
            route_options: dict[str, Any] = {
                "methods": methods,
                "include_in_schema": False,
            }
            if response_class is not None:
                route_options["response_class"] = response_class
            app.add_api_route(path, endpoint, **route_options)


_register_routes()
# LiteLLM 1.103.0 registers its generic public routes before custom callbacks
# load. Starlette matches in order, so register this exact Codex-only route
# before LiteLLM's catch-all routes while keeping normal auth dependencies.
if not any(
    getattr(route, "path", None) == "/v1/alpha/search"
    and "POST" in (getattr(route, "methods", None) or set())
    for route in app.router.routes
):
    app.add_api_route(
        "/v1/alpha/search",
        codex_search,
        methods=["POST"],
        dependencies=[Depends(user_api_key_auth)],
        include_in_schema=False,
    )
    search_route = app.router.routes.pop()
    catch_all_index = next(
        (
            index
            for index, route in enumerate(app.router.routes)
            if getattr(route, "path", "").endswith("{path:path}")
        ),
        len(app.router.routes),
    )
    app.router.routes.insert(catch_all_index, search_route)
register_ui_routes(app)
from subroute.usage_accounting import install_receipt_routes
install_receipt_routes(app)
