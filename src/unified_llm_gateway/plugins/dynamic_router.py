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
from fastapi import HTTPException, Request
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import get_or_create_metadata_bucket, get_metadata_variable_name_from_kwargs
from litellm.proxy.proxy_server import app
from pydantic import BaseModel, ValidationError
from unified_llm_gateway.ui_control import register_ui_routes, source_configuration


RoutingMode = Literal["alias", "force", "off"]
VIRTUAL_ALIASES = frozenset({"current", "default", "auto"})
RETIRED_MODEL_ALIASES = {"openai-guided": "openai", "openrouter-guided": "openrouter", "minimax-guided": "minimax"}
ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutingState:
    active_model: str
    mode: RoutingMode = "alias"
    policy_version: int = 1
    advisor_model: str | None = "gemini-subscription"
    reasoning_effort: str | None = None
    advisor_reasoning_effort: str | None = None


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


class AdvisorUpdate(BaseModel):
    advisor_model: str | None
    reasoning_effort: str | None = None


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
            initial_model = os.getenv("ACTIVE_MODEL", self.choices[0].model_id)
            initial_model = RETIRED_MODEL_ALIASES.get(initial_model, initial_model)
            state = RoutingState(active_model=initial_model, advisor_model=os.getenv("ADVISOR_MODEL", "gemini-subscription"))
            self._validate(state)
            self._write_atomic(state)
            return state
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        state = RoutingState(
            active_model=raw["active_model"],
            mode=raw.get("mode", "alias"),
            policy_version=int(raw.get("policy_version", 1)),
            advisor_model=raw.get("advisor_model", os.getenv("ADVISOR_MODEL", "gemini-subscription")),
            reasoning_effort=raw.get("reasoning_effort"),
            advisor_reasoning_effort=raw.get("advisor_reasoning_effort"),
        )
        if replacement := RETIRED_MODEL_ALIASES.get(state.active_model):
            state = replace(state, active_model=replacement, policy_version=state.policy_version + 1)
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
        if state.advisor_model is not None and state.advisor_model not in self.advisor_models:
            raise ValueError(f"advisor model is not selectable: {state.advisor_model!r}")
        for model, effort in (
            (state.active_model, state.reasoning_effort),
            (state.advisor_model, state.advisor_reasoning_effort),
        ):
            choice = next((item for item in self.choices if item.model_id == model), None)
            if effort is not None and (choice is None or effort not in choice.reasoning_efforts):
                raise ValueError(f"reasoning effort {effort!r} is not supported by {model!r}")

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
            )
            self._validate(candidate)
            self._write_atomic(candidate)
            self._state = candidate
            return candidate

    def update_advisor(self, advisor_model: str | None, **settings: Any) -> RoutingState:
        with self._lock:
            candidate = replace(
                self._state,
                advisor_model=advisor_model,
                policy_version=self._state.policy_version + 1,
                advisor_reasoning_effort=settings.get("reasoning_effort", self._state.advisor_reasoning_effort if advisor_model == self._state.advisor_model else None),
            )
            self._validate(candidate)
            self._write_atomic(candidate)
            self._state = candidate
            return candidate

    def resolve(self, requested_model: str | None) -> tuple[str | None, RoutingState]:
        state = self.snapshot()
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


class DynamicRoutingPlugin(CustomLogger):
    def __init__(self, control_plane: RoutingControlPlane) -> None:
        super().__init__()
        self.control_plane = control_plane

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> dict | None:
        requested_model = data.get("model")
        if call_type in {"aresponses", "responses"}:
            data.setdefault("litellm_metadata", {})
        _, metadata = get_or_create_metadata_bucket(data)
        if (
            isinstance(metadata, dict)
            and metadata.get("advisor_sub_call") is True
        ) or (
            isinstance(requested_model, str)
            and requested_model.endswith("-advisor")
        ):
            return None

        resolved_model, state = self.control_plane.resolve(requested_model)
        # The advisor hook consumes this exact request snapshot, never a second
        # read of mutable global policy. Overwrite any client-supplied value.
        metadata["gateway_policy"] = asdict(state)
        # Capture the override once. Deployment hooks also run for nested
        # LiteLLM translations, so they must never read mutable policy again.
        routed = state.mode == "force" or (state.mode == "alias" and requested_model in VIRTUAL_ALIASES)
        metadata["gateway_reasoning_effort"] = state.reasoning_effort if routed else None
        if resolved_model == requested_model:
            return data

        metadata["routing"] = {
            "requested_model": requested_model,
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
        if metadata.get("advisor_sub_call") is True:
            effort = (metadata.get("gateway_policy") or {}).get("advisor_reasoning_effort")
        else:
            effort = metadata.get("gateway_reasoning_effort")
        if effort is None:
            return None
        updated = kwargs.copy()
        if call_type in {"aresponses", "responses"}:
            updated["reasoning"] = {**(kwargs.get("reasoning") or {}), "effort": effort}
        elif call_type in {"anthropic_messages", "aanthropic_messages"}:
            # LiteLLM 1.101.0 natively maps adaptive thinking/output_config to
            # Responses or Chat reasoning effort. Keep translation in LiteLLM.
            updated["thinking"] = {"type": "adaptive"}
            updated["output_config"] = {**(kwargs.get("output_config") or {}), "effort": effort}
        elif call_type in {"acompletion", "completion"}:
            updated["reasoning_effort"] = effort
        else:
            return None
        return updated


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
        settings = update.model_dump(include={"reasoning_effort"}, exclude_unset=True)
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
        settings = update.model_dump(include={"reasoning_effort"}, exclude_unset=True)
        return asdict(control_plane.update_advisor(update.advisor_model, **settings))
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
register_ui_routes(app)
