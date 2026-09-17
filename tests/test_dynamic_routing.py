import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app

import unified_llm_gateway.plugins.dynamic_router as dynamic_routing
from unified_llm_gateway.plugins.dynamic_router import (
    DynamicRoutingPlugin,
    RoutingControlPlane,
)
from unified_llm_gateway.plugins.advisor_plugin import ADVISOR_TOOL_TYPE, AdvisorPlugin


def make_control_plane(tmp_path: Path) -> RoutingControlPlane:
    config = tmp_path / "litellm.yaml"
    config.write_text(
        """model_list:
  - model_name: current
    model_info: {selectable: true}
    litellm_params: {model: openai/virtual}
  - model_name: minimax
    model_info: {display_name: MiniMax M3, capabilities: [tools, 204k]}
    litellm_params: {model: minimax/MiniMax-M3}
  - model_name: hidden
    model_info: {selectable: false}
    litellm_params: {model: openai/hidden}
  - model_name: desktop
    litellm_params: {model: ollama/qwen}
  - model_name: gemini-subscription
    model_info: {selectable: false, advisor_selectable: true}
    litellm_params: {model: antigravity/gemini}
  - model_name: codex-terra-advisor
    model_info: {selectable: false, advisor_selectable: true}
    litellm_params: {model: codex-advisor/terra}
  - model_name: codex-sol-advisor
    model_info: {selectable: false, advisor_selectable: true}
    litellm_params: {model: codex-advisor/sol}
  - model_name: codex-astra-advisor
    model_info: {selectable: false, advisor_selectable: true}
    litellm_params: {model: codex-advisor/astra}
""",
        encoding="utf-8",
    )
    return RoutingControlPlane(config, tmp_path / "routing-state.json")


def run(plugin: DynamicRoutingPlugin, data: dict):
    return asyncio.run(plugin.async_pre_call_hook({}, None, data, "acompletion"))


def test_candidates_filter_virtual_and_non_selectable_models(tmp_path: Path):
    control = make_control_plane(tmp_path)

    assert [choice.model_id for choice in control.choices] == [
        "minimax",
        "desktop",
        "gemini-subscription",
        "codex-terra-advisor",
        "codex-sol-advisor",
        "codex-astra-advisor",
    ]
    assert control.choices[0].display_name == "MiniMax M3"
    assert control.choices[0].capabilities == ("tools", "204k")


def test_alias_mode_only_resolves_current_and_preserves_trace(tmp_path: Path):
    control = make_control_plane(tmp_path)
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "current", "metadata": {"client": "codex"}}

    assert run(plugin, data) is data
    assert data["model"] == "minimax"
    assert data["metadata"] == {
        "client": "codex",
        "gateway_policy": {
            "active_model": "minimax", "mode": "alias", "policy_version": 1,
            "advisor_model": "gemini-subscription",
        },
        "routing": {
            "requested_model": "current",
            "resolved_model": "minimax",
            "mode": "alias",
            "policy_version": 1,
        },
    }
    explicit = {"model": "desktop"}
    assert run(plugin, explicit) is explicit
    assert explicit["model"] == "desktop"

    for alias in ("default", "auto"):
        compatible = {"model": alias}
        run(plugin, compatible)
        assert compatible["model"] == "minimax"


def test_force_and_off_modes_have_explicit_semantics(tmp_path: Path):
    control = make_control_plane(tmp_path)
    plugin = DynamicRoutingPlugin(control)

    force = control.update("desktop", "force")
    data = {"model": "some-client-model"}
    run(plugin, data)
    assert data["model"] == "desktop"
    assert data["metadata"]["routing"]["policy_version"] == force.policy_version

    control.update("minimax", "off")
    passthrough = {"model": "current"}
    assert run(plugin, passthrough) is passthrough
    assert passthrough["model"] == "current"


def test_routing_precedes_advisor_injection_for_guided_target(tmp_path: Path):
    control = make_control_plane(tmp_path)
    control.allowed_models = frozenset({*control.allowed_models, "minimax-guided"})
    control.update("minimax-guided", "alias")
    router = DynamicRoutingPlugin(control)
    advisor = AdvisorPlugin(target_model_aliases=frozenset({"minimax-guided"}))
    data = {"model": "current", "messages": [{"role": "user", "content": "help"}]}

    asyncio.run(router.async_pre_call_hook({}, None, data, "anthropic_messages"))
    control.update_advisor("codex-terra-advisor")
    asyncio.run(advisor.async_pre_call_hook({}, None, data, "anthropic_messages"))

    assert data["model"] == "minimax-guided"
    assert data["tools"][0]["type"] == ADVISOR_TOOL_TYPE
    assert data["tools"][0]["model"] == "gemini-subscription"


def test_persisted_advisor_wins_over_startup_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ADVISOR_MODEL", "codex-terra-advisor")
    control = make_control_plane(tmp_path)
    assert control.snapshot().advisor_model == "codex-terra-advisor"
    control.update_advisor("codex-sol-advisor")
    monkeypatch.setenv("ADVISOR_MODEL", "gemini-subscription")
    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot().advisor_model == "codex-sol-advisor"


def test_framework_runs_routing_before_advisor(tmp_path, monkeypatch):
    import litellm
    from litellm.caching.caching import DualCache
    from litellm.proxy.utils import ProxyLogging
    from litellm.proxy._types import UserAPIKeyAuth

    control = make_control_plane(tmp_path)
    control.allowed_models = frozenset({*control.allowed_models, "minimax-guided"})
    control.update("minimax-guided", "alias")
    control.update_advisor("codex-terra-advisor")
    proxy = ProxyLogging(user_api_key_cache=DualCache())
    monkeypatch.setattr(litellm, "callbacks", [DynamicRoutingPlugin(control), AdvisorPlugin()])
    result = asyncio.run(proxy.pre_call_hook(
        user_api_key_dict=UserAPIKeyAuth(),
        data={"model": "current", "messages": [{"role": "user", "content": "hello"}]},
        call_type="anthropic_messages",
    ))
    assert result["model"] == "minimax-guided"
    assert result["tools"][0]["model"] == "codex-terra-advisor"
    assert result["metadata"]["gateway_policy"]["policy_version"] == control.snapshot().policy_version


def test_update_is_validated_versioned_and_atomically_persisted(tmp_path: Path):
    control = make_control_plane(tmp_path)
    state = control.update("desktop", "force")

    assert state.policy_version == 2
    assert json.loads(control.state_path.read_text(encoding="utf-8")) == {
        "active_model": "desktop",
        "mode": "force",
        "policy_version": 2,
        "advisor_model": "gemini-subscription",
    }
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError, match="not selectable"):
        control.update("current", "alias")
    assert control.snapshot() == state


def test_control_routes_share_one_page_and_update_new_request_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    control = make_control_plane(tmp_path)
    monkeypatch.setattr(dynamic_routing, "control_plane", control)
    client = TestClient(app, client=("127.0.0.1", 50000))

    for path in ("/m", "/s"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Changes apply to new requests only." in response.text
        assert 'href="/ui"' in response.text
        assert "MiniMax M3 [tools, 204k]" in response.text

    response = client.post(
        "/api/active-model",
        json={"model": "desktop", "mode": "force"},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "active_model": "desktop",
        "mode": "force",
        "policy_version": 2,
        "advisor_model": "gemini-subscription",
    }
    assert client.get("/api/active-model").json() == response.json()
    advisor = client.post(
        "/api/advisor-model",
        json={"advisor_model": "codex-terra-advisor"},
        headers={"origin": "http://testserver"},
    )
    assert advisor.status_code == 200
    assert advisor.json()["advisor_model"] == "codex-terra-advisor"


def test_control_api_rejects_cross_origin_non_json_and_unknown_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    control = make_control_plane(tmp_path)
    monkeypatch.setattr(dynamic_routing, "control_plane", control)
    client = TestClient(app, client=("127.0.0.1", 50000))

    assert client.post(
        "/api/active-model",
        json={"model": "desktop", "mode": "alias"},
        headers={"origin": "https://attacker.example"},
    ).status_code == 403
    assert client.post(
        "/api/active-model",
        content='{"model":"desktop","mode":"alias"}',
        headers={"content-type": "text/plain"},
    ).status_code == 415
    response = client.post(
        "/api/active-model", json={"model": "current", "mode": "alias"}
    )
    assert response.status_code == 422
    assert "not selectable" in response.json()["detail"]
