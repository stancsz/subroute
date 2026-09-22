"""Policy persistence and real LiteLLM translation, without provider spending."""

import asyncio
import json
from pathlib import Path

import litellm
import pytest
from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app
from litellm.types.utils import Usage

from unified_llm_gateway.handlers import antigravity, codex_advisor
from unified_llm_gateway.plugins import dynamic_router
from unified_llm_gateway.plugins.dynamic_router import DynamicRoutingPlugin, RoutingControlPlane


ROOT = Path(__file__).parents[1]


@pytest.fixture
def control(tmp_path):
    return RoutingControlPlane(ROOT / "config/litellm.yaml", tmp_path / "state.json")


def test_reasoning_persists_independently_and_old_state_still_loads(control):
    control.update("codex-luna", "force", reasoning_effort="high")
    control.update_advisor("gemini-subscription-pro", reasoning_effort="low")
    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot() == control.snapshot()
    control.update("codex-luna", "alias")  # Existing API clients omit effort.
    assert control.snapshot().reasoning_effort == "high"
    control.update_advisor(None)
    assert control.snapshot().reasoning_effort == "high"
    assert control.snapshot().advisor_reasoning_effort is None
    control.update("desktop", "alias")
    assert control.snapshot().reasoning_effort is None
    raw = json.loads(control.state_path.read_text())
    raw.pop("reasoning_effort")
    raw.pop("advisor_reasoning_effort")
    control.state_path.write_text(json.dumps(raw))
    assert RoutingControlPlane(control.config_path, control.state_path).snapshot().reasoning_effort is None


def test_invalid_effort_fails_before_state_changes(control, monkeypatch):
    monkeypatch.setattr(dynamic_router, "control_plane", control)
    client = TestClient(app, client=("127.0.0.1", 50000))
    original = control.snapshot()
    for endpoint, body in [
        ("/api/active-model", {"model": "gemini-subscription-pro", "mode": "alias", "reasoning_effort": "medium"}),
        ("/api/active-model", {"model": "codex-luna", "mode": "alias", "reasoning_effort": "ultra"}),
        ("/api/advisor-model", {"advisor_model": None, "reasoning_effort": "high"}),
    ]:
        assert client.post(endpoint, json=body).status_code == 422
        assert control.snapshot() == original
    result = client.post("/api/active-model", json={"model": "codex-luna", "mode": "force", "reasoning_effort": "high"})
    assert result.status_code == 200
    assert result.json()["reasoning_effort"] == "high"
    assert client.post("/api/active-model", json={"model": "codex-luna", "mode": "force", "reasoning_effort": None}).json()["reasoning_effort"] is None


@pytest.mark.parametrize("mode,requested,expected", [
    ("force", "desktop", "high"), ("alias", "current", "high"),
    ("alias", "codex-luna", None), ("off", "current", None),
])
def test_effort_only_overrides_routed_requests_and_uses_one_snapshot(control, mode, requested, expected):
    control.update("codex-luna", mode, reasoning_effort="high")
    plugin = DynamicRoutingPlugin(control)
    data = {"model": requested, "reasoning_effort": "low", "metadata": {"gateway_reasoning_effort": "max"}}
    asyncio.run(plugin.async_pre_call_hook({}, None, data, "acompletion"))
    control.update("codex-luna", mode, reasoning_effort="medium")
    result = asyncio.run(plugin.async_pre_call_deployment_hook(data, "acompletion"))
    assert data["metadata"]["gateway_reasoning_effort"] == expected
    assert (result or data)["reasoning_effort"] == (expected or "low")
    assert data["reasoning_effort"] == "low"  # Hook copies the original.


@pytest.mark.parametrize("advisor_call,expected", [(False, "high"), (True, "low")])
def test_responses_override_preserves_other_reasoning_fields(control, advisor_call, expected):
    plugin = DynamicRoutingPlugin(control)
    data = {"reasoning": {"effort": "medium", "summary": "auto"}, "metadata": {
        "gateway_reasoning_effort": "high", "advisor_sub_call": advisor_call,
        "gateway_policy": {"advisor_reasoning_effort": "low"},
    }}
    result = asyncio.run(plugin.async_pre_call_deployment_hook(data, "aresponses"))
    assert result["reasoning"] == {"effort": expected, "summary": "auto"}


@pytest.mark.parametrize("provider_metadata", [None, {"label": "client-value"}])
def test_responses_policy_never_leaks_into_provider_metadata(control, provider_metadata):
    control.update("codex-luna", "force", reasoning_effort="low")
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "current", "input": [{"role": "user", "content": "hello"}]}
    if provider_metadata is not None:
        data["metadata"] = provider_metadata.copy()
    asyncio.run(plugin.async_pre_call_hook({}, None, data, "aresponses"))
    assert data.get("metadata") == provider_metadata
    assert data["litellm_metadata"]["gateway_policy"]["reasoning_effort"] == "low"
    result = asyncio.run(plugin.async_pre_call_deployment_hook(data, "aresponses"))
    assert result["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("protocol", ["chat", "messages"])
@pytest.mark.parametrize("alias,model,effort", [("codex-luna", "gpt-6-luna", "high")])
def test_native_codex_bridge_transmits_selected_effort(control, monkeypatch, protocol, alias, model, effort):
    control.update(alias, "force", reasoning_effort=effort)
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "current", "messages": [{"role": "user", "content": "hello"}]}
    asyncio.run(plugin.async_pre_call_hook({}, None, data, "acompletion"))
    monkeypatch.setattr(litellm, "callbacks", [plugin])
    captured = []

    async def endpoint(**kwargs):
        captured.append(kwargs)
        raise ValueError("captured provider request")

    monkeypatch.setattr(litellm, "aresponses", endpoint)
    with pytest.raises(Exception, match="captured provider request"):
        if protocol == "chat":
            asyncio.run(litellm.acompletion(model=f"openai/responses/{model}", messages=data["messages"], metadata=data["metadata"], api_key="fixture", num_retries=0))
        else:
            asyncio.run(litellm.anthropic.acreate(model=f"openai/responses/{model}", messages=data["messages"], metadata=data["metadata"], max_tokens=100, api_key="fixture"))
    assert len(captured) == 1
    assert captured[0]["reasoning"]["effort"] == effort


@pytest.mark.parametrize("provider,model,advisor_call", [
    ("antigravity", "gemini-3.1-pro", False),
    ("antigravity", "gemini-3.1-pro", True),
    ("codex-advisor", "gpt-6-sol", True),
])
def test_native_custom_provider_transmits_target_or_advisor_effort(control, monkeypatch, provider, model, advisor_call):
    plugin = DynamicRoutingPlugin(control)
    monkeypatch.setattr(litellm, "callbacks", [plugin])
    captured = []

    async def fake_agy(model, prompt):
        captured.append((model, None))
        return "OK", Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    async def fake_codex(model, messages, **kwargs):
        captured.append((model, kwargs.get("reasoning_effort")))
        return "OK", Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    monkeypatch.setattr(antigravity, "invoke_agy", fake_agy)
    monkeypatch.setattr(codex_advisor, "call_codex_streaming_collect", fake_codex)
    monkeypatch.setattr(litellm, "custom_provider_map", [
        {"provider": "antigravity", "custom_handler": antigravity.antigravity_handler},
        {"provider": "codex-advisor", "custom_handler": codex_advisor.codex_advisor_handler},
    ])
    metadata = {"gateway_reasoning_effort": "high", "advisor_sub_call": advisor_call,
                "gateway_policy": {"advisor_reasoning_effort": "low"}}
    result = asyncio.run(litellm.anthropic.acreate(
        model=f"{provider}/{model}", messages=[{"role": "user", "content": "hello"}],
        max_tokens=100, metadata=metadata, allowed_openai_params=["reasoning_effort"],
    ))
    assert result["content"][0]["text"] == "OK"
    if provider == "antigravity":
        assert captured == [(f"{model}-{'low' if advisor_call else 'high'}", None)]
    else:
        assert captured == [(model, "low")]


@pytest.mark.parametrize("model,effort,expected", [
    ("gemini-3.8-flash", "medium", "gemini-3.8-flash-medium"),
    ("gemini-3.7-flash", "low", "gemini-3.7-flash-low"),
    ("gemini-3.6-flash", "high", "gemini-3.6-flash-high"),
    ("gemini-3.1-pro", "low", "gemini-3.1-pro-low"),
])
def test_gemini_effort_selects_catalog_variant(model, effort, expected):
    assert antigravity.model_with_effort(model, effort) == expected
    with pytest.raises(ValueError, match="Unsupported"):
        antigravity.model_with_effort(model, "xhigh")


@pytest.mark.parametrize("alias,provider,model", [
    ("openai", "openai", "gpt-5.2-codex"),
    ("gemini-api", "gemini", "gemini-3-flash-preview"),
])
def test_api_effort_choices_survive_native_translation(control, alias, provider, model):
    from litellm.utils import get_optional_params

    choice = next(item for item in control.choices if item.model_id == alias)
    for effort in choice.reasoning_efforts:
        control.update(alias, "force", reasoning_effort=effort)
        params = get_optional_params(model=model, custom_llm_provider=provider, reasoning_effort=effort, drop_params=True)
        if provider == "gemini":
            assert params["thinkingConfig"]["thinkingLevel"] == effort
        else:
            assert params["reasoning_effort"] == effort


def test_routing_choices_follow_configured_sources_without_exposing_credentials(control, monkeypatch):
    monkeypatch.setattr(dynamic_router, "control_plane", control)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_AUTH_FILE", "fixture-auth-path")
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://fixture-bridge")
    client = TestClient(app, client=("127.0.0.1", 50000))
    result = client.get("/api/routing-options").json()
    choices = {item["model_id"]: item for item in result["models"]}
    assert choices["openai"]["configured"] is False
    assert choices["codex-luna"]["configured"] is True
    assert choices["codex-luna"]["provider"] == choices["openai"]["provider"] == "OpenAI"
    assert choices["codex-luna"]["access"] != choices["openai"]["access"]
    assert next(item for item in result["advisor_models"] if item["model_id"] == "gemini-subscription")["configured"] is True
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret-do-not-expose")
    response = client.get("/api/routing-options")
    assert next(item for item in response.json()["models"] if item["model_id"] == "openai")["configured"] is True
    assert "fixture-secret" not in response.text


@pytest.mark.parametrize("old,canonical", [("openai-guided", "openai"), ("openrouter-guided", "openrouter"), ("minimax-guided", "minimax")])
@pytest.mark.parametrize("advisor", [None, "codex-sol-advisor"])
def test_retired_model_state_migrates_without_changing_advisor(control, old, canonical, advisor):
    original = {"active_model": old, "mode": "force", "policy_version": 9,
                "advisor_model": advisor, "advisor_reasoning_effort": "high" if advisor else None}
    control.state_path.write_text(json.dumps(original), encoding="utf-8")
    restarted = RoutingControlPlane(control.config_path, control.state_path)
    state = restarted.snapshot()
    assert state.active_model == canonical
    assert state.advisor_model == advisor
    assert state.advisor_reasoning_effort == original["advisor_reasoning_effort"]
    assert state.policy_version == 10
    assert RoutingControlPlane(control.config_path, control.state_path).snapshot() == state
