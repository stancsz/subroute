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
from typing import Any

import yaml
from fastapi import HTTPException, Request
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.custom_llm import CustomLLMError
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket, get_metadata_variable_name_from_kwargs
from litellm.proxy.proxy_server import app
from subroute.ui_control import register_ui_routes
from pydantic import BaseModel, ValidationError


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
ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger(__name__)


def _fallback_eligible(data: dict[str, Any], call_type: str) -> bool:
    """Restrict the generic text-model chain to text-only chat protocols."""
    if call_type not in FALLBACK_CALL_TYPES:
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
    reasoning_effort: str | None = None
    fast_mode: bool = False


@dataclass(frozen=True)
class ModelChoice:
    model_id: str
    display_name: str
    reasoning_efforts: tuple[str, ...] = ()
    provider: str = "Other"
    source_id: str = ""


class RoutingUpdate(BaseModel):
    model: str
    reasoning_effort: str | None = None
    fast_mode: bool | None = None


def _load_choices(deployments: list[dict]) -> tuple[ModelChoice, ...]:
    choices: list[ModelChoice] = []
    seen: set[str] = set()
    for deployment in deployments:
        model_id = deployment.get("model_name")
        info = deployment.get("model_info") or {}
        if (
            not isinstance(model_id, str)
            or not model_id
            or model_id in seen
            or model_id in VIRTUAL_ALIASES
            or info.get("selectable", True) is False
        ):
            continue
        efforts = info.get("reasoning_efforts") or []
        if not isinstance(efforts, list) or not all(isinstance(effort, str) for effort in efforts):
            raise ValueError(f"reasoning_efforts for {model_id!r} must be a list of strings")
        choices.append(
            ModelChoice(
                model_id=model_id,
                display_name=str(info.get("display_name") or model_id),
                reasoning_efforts=tuple(efforts),
                provider=str(info.get("provider") or "Other"),
                source_id=str(info.get("source_id") or ""),
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
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        self.deployments = config.get("model_list", [])
        self.model_aliases = (config.get("router_settings") or {}).get("model_group_alias") or {}
        self.choices = _load_choices(self.deployments)
        from subroute.provider_catalog import read_catalog
        try:
            self.catalog = read_catalog(state_path)
        except (OSError, ValueError, json.JSONDecodeError):
            logger.warning("Unable to read provider catalog; saved dynamic selections require repair")
            self.catalog = {"openai": [], "gemini": [], "openai_current": [], "gemini_current": [], "openai_efforts": {}}
            # The saved selection is already control-plane state. Preserve it
            # when only the supplementary inventory has been damaged.
            try:
                from subroute.provider_catalog import _validated_models
                saved = json.loads(state_path.read_text(encoding="utf-8"))["active_model"]
                for prefix, provider in (("catalog-openai/", "openai"), ("catalog-antigravity/", "gemini")):
                    if isinstance(saved, str) and saved.startswith(prefix):
                        value = saved.removeprefix(prefix)
                        if provider == "gemini":
                            value = bytes.fromhex(value).decode("ascii")
                        self.catalog[provider] = _validated_models([value])
                        self.catalog[f"{provider}_current"] = list(self.catalog[provider])
            except (OSError, ValueError, UnicodeDecodeError, KeyError, TypeError, json.JSONDecodeError):
                pass
        self._publish_catalog_choices()
        self.allowed_models = frozenset(choice.model_id for choice in self.choices)
        self._state = self._load_or_create_state()

    def _publish_catalog_choices(self) -> None:
        from subroute.provider_catalog import catalog_alias
        static = tuple(choice for choice in self.choices if not choice.model_id.startswith(("catalog-openai/", "catalog-antigravity/")))
        configured_codex = {
            entry.get("litellm_params", {}).get("model", "").removeprefix("codex-subscription/"): choice
            for entry in self.deployments for choice in static
            if entry.get("model_name") == choice.model_id
            and isinstance(entry.get("litellm_params", {}).get("model"), str)
            and entry["litellm_params"]["model"].startswith("codex-subscription/")
        }
        dynamic = tuple(
            replace(configured_codex[model], model_id=catalog_alias(provider, model),
                    display_name=model)
            if provider == "openai" and model in configured_codex else
            ModelChoice(model_id=catalog_alias(provider, model), display_name=model,
                        provider=label, source_id=source)
            for provider, label, source in (
                ("openai", "OpenAI", "openai-subscription"),
                ("gemini", "Antigravity", "gemini-subscription"),
            ) for model in self.catalog[provider]
        )
        dynamic = tuple(replace(choice, reasoning_efforts=tuple(self.catalog["openai_efforts"][choice.display_name]))
                        if choice.source_id == "openai-subscription" and choice.display_name in self.catalog["openai_efforts"]
                        else choice for choice in dynamic)
        self.choices = static + dynamic

    def add_catalog_models(self, provider: str, models: list[str], efforts: dict[str, list[str]] | None = None) -> None:
        from subroute.provider_catalog import SavedEffortUnavailable, _validated_efforts, _validated_models, write_catalog
        if provider not in {"openai", "gemini"}:
            raise ValueError("invalid catalog provider")
        with self._lock:
            catalog = {key: (dict(value) if isinstance(value, dict) else list(value)) for key, value in self.catalog.items()}
            catalog[provider] = _validated_models(sorted(set(catalog[provider]) | set(models)))
            catalog[f"{provider}_current"] = _validated_models(models)
            if provider == "openai" and efforts is not None:
                validated_efforts = _validated_efforts(efforts, catalog["openai_current"])
                state = self._state
                slug = state.active_model.removeprefix("catalog-openai/") if state.active_model.startswith("catalog-openai/") else None
                if slug is None:
                    upstream = next((entry.get("litellm_params", {}).get("model", "") for entry in self.deployments
                                     if entry.get("model_name") == state.active_model), "")
                    if isinstance(upstream, str) and upstream.startswith("codex-subscription/"):
                        slug = upstream.removeprefix("codex-subscription/")
                if (slug in validated_efforts and state.reasoning_effort is not None
                        and state.reasoning_effort not in validated_efforts[slug]):
                    raise SavedEffortUnavailable("saved reasoning effort no longer offered")
                catalog["openai_efforts"].update(validated_efforts)
            write_catalog(self.state_path, catalog)
            self.catalog = catalog
            self._publish_catalog_choices()
            self.allowed_models = frozenset(choice.model_id for choice in self.choices)

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
            initial_model = self._configured_target(initial_model) or fallback_model
            state = RoutingState(active_model=initial_model)
            self._validate(state)
            self._write_atomic(state)
            return state
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        state = RoutingState(
            active_model=raw["active_model"],
            reasoning_effort=raw.get("reasoning_effort"),
            fast_mode=raw.get("fast_mode", False),
        )
        routed_model = self._configured_target(state.active_model)
        if routed_model is None:
            routed_model = DEFAULT_TARGET_MODEL if DEFAULT_TARGET_MODEL in self.allowed_models else self.choices[0].model_id
        active_choice = next(
            (choice for choice in self.choices if choice.model_id == routed_model), None
        )
        unsupported_effort = (
            state.reasoning_effort is not None
            and (active_choice is None or state.reasoning_effort not in active_choice.reasoning_efforts)
        )
        state = replace(
            state,
            active_model=routed_model,
            reasoning_effort=None if unsupported_effort else state.reasoning_effort,
            fast_mode=state.fast_mode and active_choice is not None and active_choice.source_id == "openai-subscription",
        )
        self._validate(state)
        if raw != asdict(state):
            self._write_atomic(state)
        return state

    def _configured_target(self, model: str) -> str | None:
        if model in self.allowed_models:
            return model
        alias = self.model_aliases.get(model)
        if isinstance(alias, dict):
            alias = alias.get("model")
        return alias if isinstance(alias, str) and alias in self.allowed_models else None

    def _validate(self, state: RoutingState) -> None:
        if state.active_model not in self.allowed_models:
            raise ValueError(f"active model is not selectable: {state.active_model!r}")
        if not isinstance(state.fast_mode, bool):
            raise ValueError("fast_mode must be a boolean")
        active_choice = next(choice for choice in self.choices if choice.model_id == state.active_model)
        if state.fast_mode and active_choice.source_id != "openai-subscription":
            raise ValueError("Fast mode requires an OpenAI subscription target")
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

    def update(self, model: str, **settings: Any) -> RoutingState:
        with self._lock:
            candidate = replace(
                self._state,
                active_model=model,
                reasoning_effort=settings.get("reasoning_effort", self._state.reasoning_effort if model == self._state.active_model else None),
                fast_mode=settings.get("fast_mode", self._state.fast_mode if model == self._state.active_model else False),
            )
            self._validate(candidate)
            self._write_atomic(candidate)
            self._state = candidate
            return candidate

    def resolve(self, requested_model: str | None) -> tuple[str | None, RoutingState]:
        state = self.snapshot()
        return (state.active_model if requested_model == "default" else requested_model), state


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
        if call_type in {"anthropic_messages", "aanthropic_messages"}:
            from litellm.llms.anthropic.pass_through.messages.interceptors.advisor import ANTHROPIC_ADVISOR_TOOL_TYPE

            if any(isinstance(tool, dict) and tool.get("type") == ANTHROPIC_ADVISOR_TOOL_TYPE
                   for tool in data.get("tools") or []):
                raise HTTPException(status_code=400, detail="The Advisor tool has been retired.")
        original_requested_model = data.get("model")
        if isinstance(original_requested_model, str) and original_requested_model.startswith(("catalog-openai/", "catalog-antigravity/")) and original_requested_model not in self.control_plane.allowed_models:
            raise HTTPException(status_code=404, detail="model is not in the refreshed provider catalog")
        requested_model = original_requested_model
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
        if requested_model == "auto" and not _fallback_eligible(data, call_type):
            # The global chain is for text-only chat. Preserve media and
            # specialized endpoint semantics by keeping those requests local
            # to their explicitly selected provider route.
            data["disable_fallbacks"] = True
        resolved_model, state = self.control_plane.resolve(requested_model)
        from subroute.audio import audio_route
        from subroute.plugins.image_tools import image_request_context

        try:
            if any(choice.model_id == resolved_model and choice.source_id == "gemini-subscription"
                   for choice in self.control_plane.choices):
                audio_route(data, call_type)
            image_context = image_request_context(data, call_type) if resolved_model == "codex-luna" else None
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        if image_context is not None:
            data["disable_fallbacks"] = True
            _, metadata = get_or_create_metadata_bucket(data)
            state = self.control_plane.snapshot()
            metadata["gateway_reasoning_effort"] = None
            metadata["gateway_target_fast_mode"] = False
            metadata["routing"] = {
                "requested_model": original_requested_model, "resolved_model": "codex-luna",
                "mode": "image_tool",
            }
            data["model"] = "codex-luna"
            data["gateway_image_request"] = image_context
            return data
        if resolved_model == "codex-luna" and call_type in {"image_generation", "aimage_generation"}:
            data["disable_fallbacks"] = True
            from subroute.handlers.codex_images import IMAGE_MODEL, image_options

            try:
                options = image_options(data)
            except CustomLLMError as exc:
                raise HTTPException(exc.status_code, detail=str(exc)) from exc
            _, metadata = get_or_create_metadata_bucket(data)
            state = self.control_plane.snapshot()
            metadata["gateway_reasoning_effort"] = None
            metadata["gateway_target_fast_mode"] = False
            metadata["routing"] = {
                "requested_model": original_requested_model, "resolved_model": IMAGE_MODEL,
                "mode": "image_generation",
            }
            data["model"] = IMAGE_MODEL
            data["image_generation_options"] = options
            return data
        _, metadata = get_or_create_metadata_bucket(data)

        # Capture the override once. Deployment hooks also run for nested
        # LiteLLM translations, so they must never read mutable policy again.
        routed = requested_model == "default"
        metadata["gateway_reasoning_effort"] = state.reasoning_effort if routed else None
        metadata["gateway_target_fast_mode"] = bool(routed and state.fast_mode)
        if resolved_model == requested_model:
            return data

        metadata["routing"] = {
            "requested_model": original_requested_model,
            "resolved_model": resolved_model,
        }
        data["model"] = resolved_model
        logger.debug(
            "resolved model alias requested=%s resolved=%s",
            requested_model,
            resolved_model,
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


async def active_model_state(request: Request) -> dict[str, Any]:
    _require_local_control_request(request)
    return asdict(control_plane.snapshot())


async def routing_options(request: Request) -> dict[str, Any]:
    """Presentation data for local control clients, never the request data path."""
    _require_local_control_request(request)
    from subroute.provider_catalog import catalog_alias
    visible = {catalog_alias(provider, model) for provider in ("openai", "gemini")
               for model in control_plane.catalog[f"{provider}_current"]}
    state = control_plane.snapshot()
    active = state.active_model
    choices = [asdict(choice)
               for choice in control_plane.choices
               if not choice.model_id.startswith(("catalog-openai/", "catalog-antigravity/"))
               or choice.model_id in visible or choice.model_id == active]
    # The current provider inventory is the target menu. Keep a saved static
    # route visible without changing its routing.
    static_models = {entry.get("model_name"): entry.get("litellm_params", {}).get("model", "")
                     for entry in control_plane.deployments}
    hidden_targets = set()
    for choice in choices:
        model_id = choice["model_id"]
        upstream = static_models.get(model_id, "")
        if not isinstance(upstream, str):
            continue
        slug = upstream.partition("/")[2]
        if not slug or "*" in slug:
            continue
        provider = "openai" if upstream.startswith("codex-subscription/") else "gemini" if upstream.startswith("antigravity/") else None
        if provider == "gemini" and slug.startswith("agy-"):
            slug = "claude-" + slug.removeprefix("agy-")
        choice["display_name"] = slug
        if provider is None or not control_plane.catalog[f"{provider}_current"]:
            continue
        if model_id != active:
            hidden_targets.add(model_id)
            continue
        if provider == "gemini":
            from subroute.handlers.antigravity import MODELS, model_with_effort
            if state.reasoning_effort:
                slug = model_with_effort(slug, state.reasoning_effort)
            canonical = next((name for name in control_plane.catalog["gemini_current"]
                              if MODELS.get(name) and MODELS.get(name) == MODELS.get(slug)), None)
            slug = canonical or slug
        choice["display_name"] = slug
        hidden_targets.add(catalog_alias(provider, slug))
    return {
        "state": asdict(state),
        "models": [choice for choice in choices if choice["model_id"] not in hidden_targets],
    }


async def refresh_model_catalog(request: Request) -> dict[str, Any]:
    _require_local_control_request(request)
    from subroute.provider_catalog import refresh_catalog
    return await refresh_catalog()


async def update_active_model(request: Request) -> dict[str, Any]:
    _require_local_control_request(request)
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application/json is required")
    try:
        update = RoutingUpdate.model_validate(await request.json())
        settings = update.model_dump(include={"reasoning_effort", "fast_mode"}, exclude_unset=True)
        return asdict(control_plane.update(update.model, **settings))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="invalid routing update") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _register_routes() -> None:
    routes = (
        ("/api/active-model", active_model_state, "GET"),
        ("/api/active-model", update_active_model, "POST"),
        ("/api/routing-options", routing_options, "GET"),
        ("/api/model-catalog/refresh", refresh_model_catalog, "POST"),
    )
    for path, endpoint, method in routes:
        if not any(getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or ())
                   for route in app.routes):
            app.add_api_route(path, endpoint, methods=[method], include_in_schema=False)


_register_routes()
register_ui_routes(app)
