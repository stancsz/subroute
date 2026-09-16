"""Local control plane and request-time model alias resolution."""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import app
from pydantic import BaseModel, ValidationError


RoutingMode = Literal["alias", "force", "off"]
VIRTUAL_ALIASES = frozenset({"current", "default", "auto"})
ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutingState:
    active_model: str
    mode: RoutingMode = "alias"
    policy_version: int = 1
    advisor_model: str = "gemini-subscription"


@dataclass(frozen=True)
class ModelChoice:
    model_id: str
    display_name: str
    capabilities: tuple[str, ...]
    selectable: bool = True
    advisor_selectable: bool = False


class RoutingUpdate(BaseModel):
    model: str
    mode: RoutingMode


class AdvisorUpdate(BaseModel):
    advisor_model: str


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
        choices.append(
            ModelChoice(
                model_id=model_id,
                display_name=str(info.get("display_name") or model_id),
                capabilities=capabilities,
                selectable=info.get("selectable", True) is not False,
                advisor_selectable=info.get("advisor_selectable") is True,
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
            state = RoutingState(active_model=initial_model)
            self._validate(state)
            self._write_atomic(state)
            return state
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        state = RoutingState(
            active_model=raw["active_model"],
            mode=raw.get("mode", "alias"),
            policy_version=int(raw.get("policy_version", 1)),
            advisor_model=raw.get("advisor_model", "gemini-subscription"),
        )
        self._validate(state)
        return state

    def _validate(self, state: RoutingState) -> None:
        if state.active_model not in self.allowed_models:
            raise ValueError(f"active model is not selectable: {state.active_model!r}")
        if state.mode not in {"alias", "force", "off"}:
            raise ValueError(f"invalid routing mode: {state.mode!r}")
        if state.policy_version < 1:
            raise ValueError("policy_version must be positive")
        if self.advisor_models and state.advisor_model not in self.advisor_models:
            raise ValueError(f"advisor model is not selectable: {state.advisor_model!r}")

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

    def update(self, model: str, mode: RoutingMode) -> RoutingState:
        with self._lock:
            candidate = RoutingState(
                active_model=model,
                mode=mode,
                policy_version=self._state.policy_version + 1,
                advisor_model=self._state.advisor_model,
            )
            self._validate(candidate)
            self._write_atomic(candidate)
            self._state = candidate
            return candidate

    def update_advisor(self, advisor_model: str) -> RoutingState:
        with self._lock:
            candidate = RoutingState(self._state.active_model, self._state.mode, self._state.policy_version + 1, advisor_model)
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
        metadata = data.get("metadata")
        if (
            isinstance(metadata, dict)
            and metadata.get("advisor_sub_call") is True
        ) or (
            isinstance(requested_model, str)
            and requested_model.endswith("-advisor")
        ):
            return None

        resolved_model, state = self.control_plane.resolve(requested_model)
        if resolved_model == requested_model:
            return None

        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            data["metadata"] = metadata
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


def _render_page(control_plane: RoutingControlPlane) -> str:
    state = control_plane.snapshot()
    options = "".join(
        f'<option value="{html.escape(choice.model_id)}" '
        f'{"selected" if choice.model_id == state.active_model else ""}>'
        f'{html.escape(choice.display_name)}'
        f'{html.escape(" [" + ", ".join(choice.capabilities) + "]") if choice.capabilities else ""}'
        "</option>"
        for choice in control_plane.choices if choice.selectable
    )
    advisor_options = "".join(
        f'<option value="{html.escape(choice.model_id)}" '
        f'{"selected" if choice.model_id == state.advisor_model else ""}>'
        f'{html.escape(choice.display_name)}</option>'
        for choice in control_plane.choices if choice.advisor_selectable
    )
    modes = "".join(
        f'<option value="{mode}" {"selected" if mode == state.mode else ""}>{label}</option>'
        for mode, label in (
            ("alias", "alias · only current"),
            ("force", "force · all new requests"),
            ("off", "off · LiteLLM passthrough"),
        )
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Model routing</title><style>
:root{{color-scheme:dark;font:16px system-ui;background:#0b0f14;color:#e8edf2}}body{{margin:0;min-height:100vh;display:grid;place-items:center}}
main{{width:min(34rem,calc(100% - 2rem));padding:2rem;border:1px solid #29313a;border-radius:18px;background:#121820;box-shadow:0 20px 60px #0008}}
h1{{margin:0 0 .4rem;font-size:1.6rem}}p{{color:#9eabb8}}label{{display:block;margin:1.2rem 0 .35rem}}select{{width:100%;padding:.8rem;border-radius:9px;border:1px solid #394552;background:#0b0f14;color:inherit}}
#status{{min-height:1.4rem;color:#69d49c}}
.legend{{margin:1.2rem 0 0;padding:0 0 0 1.2rem;font-size:0.85rem;color:#8b98a5;line-height:1.5}}
.legend li{{margin-bottom:0.4rem}}
.legend strong{{color:#c5d1de}}
.legend code{{background:#1d2630;padding:0.1rem 0.3rem;border-radius:4px;color:#69d49c}}
small{{display:block;margin-top:1.5rem;color:#74808c}}</style></head>
<body><main><h1>Model routing</h1><p>Changes apply to new requests only.</p>
<label for="model">Active physical model</label><select id="model">{options}</select>
<label for="mode">Routing mode</label><select id="mode">{modes}</select>
<label for="advisor">Advisor model</label><select id="advisor">{advisor_options}</select>
<ul class="legend">
  <li><strong>alias</strong>: Only requests asking for <code>current</code>, <code>default</code>, or <code>auto</code> resolve to the active model. Explicit model requests are left untouched.</li>
  <li><strong>force</strong>: Overrides <em>every</em> incoming request to use the active model, ignoring whatever model name the client asked for.</li>
  <li><strong>off</strong>: Bypasses dynamic routing completely; requests pass through directly using LiteLLM's standard model matching.</li>
</ul>
<p id="status">Policy v{state.policy_version}</p><small>LiteLLM's official dashboard remains available at <a href="/ui">/ui</a>.</small>
</main><script>
const model=document.querySelector('#model'),mode=document.querySelector('#mode'),advisor=document.querySelector('#advisor'),status=document.querySelector('#status');
async function save(){{status.textContent='Saving…';const response=await fetch('/api/active-model',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{model:model.value,mode:mode.value}})}});const body=await response.json();if(!response.ok)throw new Error(body.detail||'Update failed');status.textContent=`Active: ${{body.active_model}} · ${{body.mode}} · policy v${{body.policy_version}}`;}}
async function saveAdvisor(){{status.textContent='Saving…';const response=await fetch('/api/advisor-model',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{advisor_model:advisor.value}})}});const body=await response.json();if(!response.ok)throw new Error(body.detail||'Update failed');status.textContent=`Advisor: ${{body.advisor_model}} · policy v${{body.policy_version}}`;}}
for(const input of [model,mode])input.addEventListener('change',()=>save().catch(error=>status.textContent=error.message));
advisor.addEventListener('change',()=>saveAdvisor().catch(error=>status.textContent=error.message));
</script></body></html>"""


control_plane = _default_control_plane()
dynamic_routing_plugin = DynamicRoutingPlugin(control_plane)


async def model_routing_page(request: Request) -> HTMLResponse:
    _require_local_control_request(request)
    return HTMLResponse(_render_page(control_plane))


async def active_model_state(request: Request) -> dict[str, Any]:
    _require_local_control_request(request)
    return asdict(control_plane.snapshot())


async def update_active_model(request: Request) -> dict[str, Any]:
    _require_local_control_request(request)
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application/json is required")
    try:
        update = RoutingUpdate.model_validate(await request.json())
        return asdict(control_plane.update(update.model, update.mode))
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
        ("/m", model_routing_page, ["GET"], HTMLResponse),
        ("/s", model_routing_page, ["GET"], HTMLResponse),
        ("/api/active-model", active_model_state, ["GET"], None),
        ("/api/active-model", update_active_model, ["POST"], None),
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
