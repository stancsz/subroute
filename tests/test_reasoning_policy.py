"""Policy persistence and real LiteLLM translation, without provider spending."""

import asyncio
import json
from pathlib import Path

import litellm
import pytest
from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app
from litellm.types.utils import Usage

from subroute.handlers import antigravity
from subroute.plugins import dynamic_router
from subroute.plugins.dynamic_router import DynamicRoutingPlugin, RoutingControlPlane


ROOT = Path(__file__).parents[1]


@pytest.fixture
def control(tmp_path):
    return RoutingControlPlane(ROOT / "config/litellm.yaml", tmp_path / "state.json")


def test_reasoning_persists_independently_and_old_state_still_loads(control):
    control.update("codex-luna", reasoning_effort="high")
    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot() == control.snapshot()
    control.update("codex-luna")  # Existing API clients omit effort.
    assert control.snapshot().reasoning_effort == "high"
    assert control.snapshot().reasoning_effort == "high"
    control.update("desktop")
    assert control.snapshot().reasoning_effort is None
    raw = json.loads(control.state_path.read_text())
    raw.pop("reasoning_effort")
    control.state_path.write_text(json.dumps(raw))
    assert RoutingControlPlane(control.config_path, control.state_path).snapshot().reasoning_effort is None


def test_invalid_effort_fails_before_state_changes(control, monkeypatch):
    monkeypatch.setattr(dynamic_router, "control_plane", control)
    client = TestClient(app, client=("127.0.0.1", 50000))
    original = control.snapshot()
    for endpoint, body in [
        ("/api/active-model", {"model": "gemini-subscription-pro", "mode": "alias", "reasoning_effort": "medium"}),
        ("/api/active-model", {"model": "codex-luna", "mode": "alias", "reasoning_effort": "ultra"}),
    ]:
        assert client.post(endpoint, json=body).status_code == 422
        assert control.snapshot() == original
    result = client.post("/api/active-model", json={"model": "codex-luna", "mode": "force", "reasoning_effort": "high"})
    assert result.status_code == 200
    assert result.json()["reasoning_effort"] == "high"
    assert client.post("/api/active-model", json={"model": "codex-luna", "mode": "force", "reasoning_effort": None}).json()["reasoning_effort"] is None


def test_fast_mode_is_openai_target_only_and_persists(control, monkeypatch):
    monkeypatch.setattr(dynamic_router, "control_plane", control)
    client = TestClient(app, client=("127.0.0.1", 50000))
    original = control.snapshot()
    assert client.post("/api/active-model", json={
        "model": "openrouter", "mode": "force", "fast_mode": True,
    }).status_code == 422
    assert control.snapshot() == original

    response = client.post("/api/active-model", json={
        "model": "codex-luna", "mode": "force", "fast_mode": True,
    })
    assert response.status_code == 200
    assert response.json()["fast_mode"] is True
    assert RoutingControlPlane(control.config_path, control.state_path).snapshot().fast_mode is True
    # Existing clients that omit the field retain the current target setting.
    assert client.post("/api/active-model", json={
        "model": "codex-luna", "mode": "alias",
    }).json()["fast_mode"] is True
    # Selecting another target clears the saved speed setting.
    response = client.post("/api/active-model", json={
        "model": "minimax", "mode": "force",
    })
    assert response.status_code == 200
    assert response.json()["fast_mode"] is False


@pytest.mark.parametrize("requested,expected", [("default", True), ("codex-luna", False)])
def test_fast_mode_uses_target_routing_snapshot(control, requested, expected):
    control.update("codex-luna", fast_mode=True)
    plugin = DynamicRoutingPlugin(control)
    data = {"model": requested, "metadata": {}}
    asyncio.run(plugin.async_pre_call_hook({}, None, data, "acompletion"))
    control.update("codex-luna", fast_mode=False)
    assert data["metadata"]["gateway_target_fast_mode"] is expected
    dispatch = {**data, "api_base": "https://chatgpt.com/backend-api/codex"}
    result = asyncio.run(plugin.async_pre_call_deployment_hook(dispatch, "acompletion"))
    assert (result or dispatch).get("service_tier") == ("fast" if expected else None)
    assert "service_tier" not in data
    dispatch["metadata"]["advisor_sub_call"] = True
    advisor_dispatch = asyncio.run(plugin.async_pre_call_deployment_hook(dispatch, "acompletion"))
    assert (advisor_dispatch or dispatch).get("service_tier") == ("fast" if expected else None)


@pytest.mark.parametrize("requested,expected", [("default", "high"), ("codex-luna", None)])
def test_effort_only_overrides_routed_requests_and_uses_one_snapshot(control, requested, expected):
    control.update("codex-luna", reasoning_effort="high")
    plugin = DynamicRoutingPlugin(control)
    data = {"model": requested, "reasoning_effort": "low", "metadata": {"gateway_reasoning_effort": "max"}}
    asyncio.run(plugin.async_pre_call_hook({}, None, data, "acompletion"))
    control.update("codex-luna", reasoning_effort="medium")
    result = asyncio.run(plugin.async_pre_call_deployment_hook(data, "acompletion"))
    assert data["metadata"]["gateway_reasoning_effort"] == expected
    assert (result or data)["reasoning_effort"] == (expected or "low")
    assert data["reasoning_effort"] == "low"  # Hook copies the original.


def test_responses_override_preserves_other_reasoning_fields(control):
    plugin = DynamicRoutingPlugin(control)
    data = {"reasoning": {"effort": "medium", "summary": "auto"}, "metadata": {
        "gateway_reasoning_effort": "high",
    }}
    result = asyncio.run(plugin.async_pre_call_deployment_hook(data, "aresponses"))
    assert result["reasoning"] == {"effort": "high", "summary": "auto"}


def test_chat_override_preserves_reasoning_summary_and_original_request(control):
    plugin = DynamicRoutingPlugin(control)
    data = {"reasoning_effort": {"effort": "medium", "summary": "auto"},
            "metadata": {"gateway_reasoning_effort": "low"}}
    result = asyncio.run(plugin.async_pre_call_deployment_hook(data, "acompletion"))
    assert result["reasoning_effort"] == {"effort": "low", "summary": "auto"}
    assert data["reasoning_effort"] == {"effort": "medium", "summary": "auto"}


@pytest.mark.parametrize("provider_metadata", [None, {"label": "client-value"}])
def test_responses_policy_never_leaks_into_provider_metadata(control, provider_metadata):
    control.update("codex-luna", reasoning_effort="low")
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "default", "input": [{"role": "user", "content": "hello"}]}
    if provider_metadata is not None:
        data["metadata"] = provider_metadata.copy()
    asyncio.run(plugin.async_pre_call_hook({}, None, data, "aresponses"))
    assert data.get("metadata") == provider_metadata
    result = asyncio.run(plugin.async_pre_call_deployment_hook(data, "aresponses"))
    assert result["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("protocol", ["chat", "messages"])
@pytest.mark.parametrize("alias,model,effort", [("codex-luna", "gpt-6-luna", "high")])
def test_native_codex_bridge_transmits_selected_effort(control, monkeypatch, protocol, alias, model, effort):
    control.update(alias, reasoning_effort=effort)
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "default", "messages": [{"role": "user", "content": "hello"}]}
    asyncio.run(plugin.async_pre_call_hook({}, None, data, "acompletion"))
    monkeypatch.setattr(litellm, "callbacks", [plugin])
    captured = []

    async def endpoint(**kwargs):
        captured.append(kwargs)
        raise ValueError("captured provider request")

    monkeypatch.setattr(litellm, "aresponses", endpoint)
    with pytest.raises(Exception, match="captured provider request"):
        if protocol == "chat":
            asyncio.run(
                litellm.acompletion(
                    model=f"openai/responses/{model}",
                    messages=data["messages"],
                    metadata=data["metadata"],
                    api_key="fixture",
                    allowed_openai_params=["reasoning_effort"],
                    num_retries=0,
                )
            )
        else:
            asyncio.run(litellm.anthropic.acreate(model=f"openai/responses/{model}", messages=data["messages"], metadata=data["metadata"], max_tokens=100, api_key="fixture"))
    assert len(captured) == 1
    assert captured[0]["reasoning"]["effort"] == effort


@pytest.mark.parametrize("provider,model", [
    ("antigravity", "gemini-3.1-pro"),
    ("antigravity", "agy-opus-5-5"),
    ("antigravity", "agy-sonnet-5-5"),
])
def test_native_custom_provider_transmits_target_effort(control, monkeypatch, provider, model):
    plugin = DynamicRoutingPlugin(control)
    monkeypatch.setattr(litellm, "callbacks", [plugin])
    captured = []

    async def fake_agy(model, prompt, **kwargs):
        assert kwargs.get("json_schema") == antigravity.PLAIN_TEXT_SCHEMA
        captured.append((model, None))
        return json.dumps({"kind": "text", "text": "OK"}), Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    async def fake_codex(model, messages, **kwargs):
        captured.append((model, kwargs.get("reasoning_effort")))
        return "OK", Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    monkeypatch.setattr(antigravity, "invoke_agy", fake_agy)
    monkeypatch.setattr(litellm, "custom_provider_map", [
        {"provider": "antigravity", "custom_handler": antigravity.antigravity_handler},
    ])
    metadata = {"gateway_reasoning_effort": "high"}
    result = asyncio.run(litellm.anthropic.acreate(
        model=f"{provider}/{model}", messages=[{"role": "user", "content": "hello"}],
        max_tokens=100, metadata=metadata, allowed_openai_params=["reasoning_effort"],
    ))
    assert result["content"][0]["text"] == "OK"
    if provider == "antigravity":
        assert captured == [(f"{model}-high", None)]
    else:
        assert captured == [(model, "high")]


@pytest.mark.parametrize("model,effort,expected", [
    ("gemini-4-hydrogen", "medium", "gemini-4-hydrogen-medium"),
    ("gemini-4-argon", "medium", "gemini-4-argon-medium"),
    ("gemini-3.8-flash", "medium", "gemini-3.8-flash-medium"),
    ("gemini-3.7-flash", "low", "gemini-3.7-flash-low"),
    ("gemini-3.6-flash", "high", "gemini-3.6-flash-high"),
    ("gemini-3.1-pro", "low", "gemini-3.1-pro-low"),
    ("agy-opus-5-5-medium", "high", "agy-opus-5-5-high"),
    ("agy-sonnet-5-5-medium", "low", "agy-sonnet-5-5-low"),
])
def test_gemini_effort_selects_catalog_variant(model, effort, expected):
    assert antigravity.model_with_effort(model, effort) == expected
    with pytest.raises(ValueError, match="Unsupported"):
        antigravity.model_with_effort(model, "xhigh")
