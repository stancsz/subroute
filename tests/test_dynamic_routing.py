import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app

import subroute.plugins.dynamic_router as dynamic_routing
from subroute.plugins.dynamic_router import (
    DynamicRoutingPlugin,
    RoutingControlPlane,
)
from subroute.plugins.advisor_plugin import AdvisorPlugin


def make_control_plane(tmp_path: Path, include_speech_models: bool = False) -> RoutingControlPlane:
    config = tmp_path / "litellm.yaml"
    config.write_text(
        """model_list:
  - model_name: default
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
  - model_name: codex-gpt-6.1-sol-advisor
    model_info: {selectable: false, advisor_selectable: true, reasoning_efforts: [low, medium, high]}
    litellm_params: {model: codex-advisor/gpt-6.1-sol}
  - model_name: codex-sol-advisor
    model_info: {selectable: false, advisor_selectable: true}
    litellm_params: {model: codex-advisor/sol}
  - model_name: codex-astra-advisor
    model_info: {selectable: false, advisor_selectable: true}
    litellm_params: {model: codex-advisor/astra}
  - model_name: codex-luna-advisor
    model_info: {selectable: false, advisor_selectable: true, reasoning_efforts: [low, medium, high, xhigh, max]}
    litellm_params: {model: codex-advisor/luna}
""",
        encoding="utf-8",
    )
    if include_speech_models:
        config.write_text(
            config.read_text(encoding="utf-8")
            + "  - model_name: mimo-v2.5-asr\n"
              "    model_info: {capabilities: [speech-recognition]}\n"
              "    litellm_params: {model: openai/mimo-v2.5-asr}\n"
              "  - model_name: mimo-v2.5-tts\n"
              "    model_info: {capabilities: [speech-synthesis]}\n"
              "    litellm_params: {model: openai/mimo-v2.5-tts}\n"
              "  - model_name: minimax-tts\n"
              "    model_info: {capabilities: [speech-synthesis]}\n"
              "    litellm_params: {model: minimax/speech-2.6-hd}\n",
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
        "codex-gpt-6.1-sol-advisor",
        "codex-sol-advisor",
        "codex-astra-advisor",
        "codex-luna-advisor",
    ]
    assert control.choices[0].display_name == "MiniMax M3"
    assert control.choices[0].capabilities == ("tools", "204k")


def test_alias_mode_resolves_default_but_preserves_explicit_models(tmp_path: Path):
    control = make_control_plane(tmp_path)
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "default", "metadata": {"client": "codex"}}

    assert run(plugin, data) is data
    assert data["model"] == "minimax"
    assert data["metadata"] == {
        "client": "codex",
        "gateway_reasoning_effort": None,
        "gateway_target_fast_mode": False,
        "gateway_policy": {
            "active_model": "minimax", "mode": "alias", "policy_version": 1,
            "advisor_model": "codex-gpt-6.1-sol-advisor",
        "reasoning_effort": None, "fast_mode": False,
        },
        "routing": {
            "requested_model": "default",
            "resolved_model": "minimax",
            "mode": "alias",
            "policy_version": 1,
        },
    }
    explicit = {"model": "desktop"}
    assert run(plugin, explicit) is explicit
    assert explicit["model"] == "desktop"

    for alias in ("default",):
        compatible = {"model": alias}
        run(plugin, compatible)
        assert compatible["model"] == "minimax"

    automatic = {"model": "auto"}
    run(plugin, automatic)
    assert automatic["model"] == "auto"
    assert automatic["metadata"]["gateway_reasoning_effort"] is None
    assert automatic["metadata"]["routing"] == {"requested_model": "auto"}


def test_force_and_off_modes_have_explicit_semantics(tmp_path: Path):
    control = make_control_plane(tmp_path)
    plugin = DynamicRoutingPlugin(control)

    force = control.update("desktop", "force")
    data = {"model": "minimax"}
    run(plugin, data)
    assert data["model"] == "desktop"
    assert data["metadata"]["routing"]["policy_version"] == force.policy_version

    control.update("minimax", "off")
    # Explicit provider selections pass through in off mode; the stable
    # default alias always follows the saved target in every mode.
    passthrough = {"model": "desktop"}
    assert run(plugin, passthrough) is passthrough
    assert passthrough["model"] == "desktop"
    selected = {"model": "default"}
    run(plugin, selected)
    assert selected["model"] == "minimax"


@pytest.mark.parametrize("case", [
    {"tool_choice": {"type": "function", "function": {"name": "lookup"}}},
    {"max_output_tokens": 8192},
])
def test_qwen_compatibility_uses_native_tools_route_without_mutating_request(
    tmp_path: Path, monkeypatch, case: dict,
):
    from litellm.proxy import proxy_server

    class LimitsRouter:
        def get_configured_token_limits(self, model):
            assert model == "qwen3.8-27b"
            return 32_768, 4096

    monkeypatch.setattr(proxy_server, "llm_router", LimitsRouter())
    plugin = DynamicRoutingPlugin(RoutingControlPlane(
        Path(__file__).parents[1] / "config" / "litellm.yaml", tmp_path / "policy.json",
    ))
    data = {
        "model": "qwen3.8-27b",
        "messages": [{"role": "user", "content": "keep the complete request"}],
        **case,
    }
    original = {key: value.copy() if isinstance(value, dict) else value
                for key, value in data.items()}

    assert run(plugin, data) is data
    assert data["model"] == "mimo-v2.6-pro"
    assert data["messages"] == original["messages"]
    assert data.get("tool_choice") == original.get("tool_choice")
    assert data.get("max_output_tokens") == original.get("max_output_tokens")
    assert data["metadata"]["routing"]["requested_model"] == "qwen3.8-27b"
    assert data["metadata"]["routing"]["resolved_model"] == "mimo-v2.6-pro"
    assert data["metadata"]["routing"]["mode"] == "qwen_native_compatibility"


def test_unknown_model_is_rejected_before_force_routing(tmp_path: Path):
    from fastapi import HTTPException

    control = make_control_plane(tmp_path)
    control.update("desktop", "force")
    data = {"model": "does-not-exist"}

    with pytest.raises(HTTPException) as error:
        run(DynamicRoutingPlugin(control), data)

    assert error.value.status_code == 404
    assert data["model"] == "does-not-exist"
    assert data["metadata"]["routing"] == {"requested_model": "does-not-exist"}


def test_default_resolves_to_saved_target_even_when_alias_mode_is_off(tmp_path: Path):
    control = make_control_plane(tmp_path)
    control.update("desktop", "off")
    data = {"model": "default", "messages": [{"role": "user", "content": "hello"}]}

    assert run(DynamicRoutingPlugin(control), data) is data
    assert data["model"] == "desktop"
    assert data["metadata"]["routing"] == {
        "requested_model": "default", "resolved_model": "desktop",
        "mode": "off", "policy_version": control.snapshot().policy_version,
    }
    assert data["metadata"]["gateway_reasoning_effort"] is None
    assert data["metadata"]["gateway_policy"]["active_model"] == "desktop"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/v1/skills", None),
        ("post", "/v1/rerank", {"model": "default", "query": "q", "documents": ["d"]}),
        ("post", "/v1/embeddings", {"model": "default", "input": "x"}),
    ],
)
def test_unconfigured_provider_operations_return_client_errors(method, path, body):
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = getattr(client, method)(path, json=body) if body is not None else client.get(path)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_operation"


def test_routing_does_not_consult_advisor_automatically(tmp_path):
    control = make_control_plane(tmp_path)
    control.update("minimax", "alias")
    control.update_advisor("codex-gpt-6.1-sol-advisor")
    router = DynamicRoutingPlugin(control)
    advisor = AdvisorPlugin()
    data = {"model": "default", "messages": [{"role": "user", "content": "help"}]}

    asyncio.run(router.async_pre_call_hook({}, None, data, "anthropic_messages"))
    asyncio.run(advisor.async_pre_call_hook({}, None, data, "anthropic_messages"))

    assert data["model"] == "minimax"
    assert len(data["tools"]) == 1
    assert data["tools"][0]["type"].startswith("advisor_")
    assert data["tools"][0]["model"] == "experts/codex-gpt-6.1-sol-advisor"
    assert data["tools"][0]["max_uses"] == 1
    assert data["messages"] == [{"role": "user", "content": "help"}]
    assert "gateway_advisor" not in data.get("metadata", {})




def test_persisted_advisor_wins_over_startup_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ADVISOR_MODEL", "codex-gpt-6.1-sol-advisor")
    control = make_control_plane(tmp_path)
    assert control.snapshot().advisor_model == "codex-gpt-6.1-sol-advisor"
    control.update_advisor("codex-sol-advisor")
    monkeypatch.setenv("ADVISOR_MODEL", "gemini-subscription")
    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot().advisor_model == "codex-sol-advisor"


def test_removed_active_reasoning_effort_migrates_without_changing_route_policy(tmp_path):
    control = make_control_plane(tmp_path)
    saved = {
        "active_model": "minimax", "mode": "force", "policy_version": 12,
        "advisor_model": "codex-gpt-6.1-sol-advisor", "reasoning_effort": "high",
        "fast_mode": False, "advisor_reasoning_effort": "low",
        "advisor_trigger": "adaptive", "advisor_sensitivity": "frequent",
        "advisor_classifier": "ranked",
    }
    control.state_path.write_text(json.dumps(saved), encoding="utf-8")

    migrated = RoutingControlPlane(control.config_path, control.state_path)

    assert migrated.snapshot().active_model == "minimax"
    assert migrated.snapshot().mode == "force"
    assert migrated.snapshot().policy_version == 13
    assert migrated.snapshot().reasoning_effort is None
    assert migrated.snapshot().advisor_model == saved["advisor_model"]
    persisted = json.loads(control.state_path.read_text(encoding="utf-8"))
    assert persisted["reasoning_effort"] is None
    assert persisted["policy_version"] == 13
    assert not {"advisor_reasoning_effort", "advisor_trigger", "advisor_sensitivity", "advisor_classifier"} & persisted.keys()

    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot() == migrated.snapshot()
    assert json.loads(control.state_path.read_text(encoding="utf-8")) == persisted


def test_default_desk_uses_luna_target_and_preserves_advisor_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIVE_MODEL", raising=False)
    monkeypatch.delenv("ADVISOR_MODEL", raising=False)
    config_path = Path(__file__).parents[1] / "config/litellm.yaml"
    state_path = tmp_path / "state.json"
    control = RoutingControlPlane(config_path, state_path)
    assert control.snapshot().active_model == "codex-luna"
    assert control.snapshot().advisor_model == "codex-gpt-6.1-sol-advisor"
    for model in (
        "codex-sol-advisor", "codex-astra-advisor", "codex-gpt-6.1-sol-advisor",
        "codex-luna-advisor",
    ):
        control.update_advisor(model)
        restarted = RoutingControlPlane(config_path, state_path)
        assert restarted.snapshot().advisor_model == model


@pytest.mark.parametrize("retired, replacement", [
    ("codex-terra", "codex-subscription"),
    ("codex-reserve", "codex-luna"),
])
def test_saved_gpt_5_target_migrates_once_and_preserves_policy(tmp_path, retired, replacement):
    config_path = Path(__file__).parents[1] / "config/litellm.yaml"
    state_path = tmp_path / "state.json"
    saved = {
        "active_model": retired, "mode": "force", "policy_version": 12,
        "advisor_model": "codex-gpt-6.1-sol-advisor",
        "reasoning_effort": "high",
    }
    state_path.write_text(json.dumps(saved), encoding="utf-8")
    control = RoutingControlPlane(config_path, state_path)
    expected = {
        **saved, "active_model": replacement, "policy_version": 13,
        "fast_mode": False,
    }
    assert json.loads(state_path.read_text(encoding="utf-8")) == expected
    assert control.resolve("default")[0] == replacement
    assert retired not in control.allowed_models
    with pytest.raises(ValueError, match="not selectable"):
        control.update(retired, "force")
    restarted = RoutingControlPlane(config_path, state_path)
    assert restarted.snapshot() == control.snapshot()
    assert json.loads(state_path.read_text(encoding="utf-8")) == expected


def test_gpt_6_1_sol_target_is_selectable_and_preserves_effort_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIVE_MODEL", "codex-gpt-6.1-sol")
    config_path = Path(__file__).parents[1] / "config/litellm.yaml"
    state_path = tmp_path / "state.json"
    control = RoutingControlPlane(config_path, state_path)
    control.update("codex-gpt-6.1-sol", "force", reasoning_effort="ultra")
    restarted = RoutingControlPlane(config_path, state_path)
    assert restarted.resolve("default")[0] == "codex-gpt-6.1-sol"
    assert restarted.snapshot().reasoning_effort == "ultra"
    assert "codex-gpt-6.1-sol" not in restarted.advisor_models


def test_advisor_cannot_be_cleared(tmp_path: Path):
    control = make_control_plane(tmp_path)
    original = control.snapshot()

    with pytest.raises(ValueError, match="an advisor model must remain selected"):
        control.update_advisor(None)

    assert control.snapshot() == original
    assert json.loads(control.state_path.read_text(encoding="utf-8"))["advisor_model"] == (
        original.advisor_model
    )


def test_framework_routing_hook_does_not_add_advisor_guidance_or_tool(tmp_path, monkeypatch):
    import litellm
    from litellm.caching.caching import DualCache
    from litellm.proxy.utils import ProxyLogging
    from litellm.proxy._types import UserAPIKeyAuth

    control = make_control_plane(tmp_path)
    control.update("minimax", "alias")
    proxy = ProxyLogging(user_api_key_cache=DualCache())
    monkeypatch.setattr(litellm, "callbacks", [DynamicRoutingPlugin(control), AdvisorPlugin()])
    data = {"model": "default", "messages": [{"role": "user", "content": "hello"}]}
    result = asyncio.run(proxy.pre_call_hook(
        user_api_key_dict=UserAPIKeyAuth(), data=data, call_type="anthropic_messages",
    ))
    assert result["model"] == "minimax"
    assert len(result["tools"]) == 1
    assert result["tools"][0]["type"].startswith("advisor_")
    assert result["tools"][0]["max_uses"] == 1
    assert result["messages"] == [{"role": "user", "content": "hello"}]
    assert "gateway_advisor" not in result.get("metadata", {})
    assert "gateway_advisor_decision" not in result.get("metadata", {})




def test_update_is_validated_versioned_and_atomically_persisted(tmp_path: Path):
    control = make_control_plane(tmp_path)
    state = control.update("desktop", "force")

    assert state.policy_version == 2
    assert json.loads(control.state_path.read_text(encoding="utf-8")) == {
        "active_model": "desktop",
        "mode": "force",
        "policy_version": 2,
        "advisor_model": "codex-gpt-6.1-sol-advisor",
        "reasoning_effort": None, "fast_mode": False,
    }
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError, match="not selectable"):
        control.update("default", "alias")
    assert control.snapshot() == state


def test_control_routes_share_one_page_and_update_new_request_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    control = make_control_plane(tmp_path)
    monkeypatch.setattr(dynamic_routing, "control_plane", control)
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.get("/control")
    assert response.status_code == 200
    assert "Connections" in response.text
    assert "Routing policy" in response.text
    assert "/control/app.js" in response.text

    assert client.get("/m").status_code == 404
    assert client.get("/s").status_code == 404

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
        "advisor_model": "codex-gpt-6.1-sol-advisor",
        "reasoning_effort": None, "fast_mode": False,
    }
    assert client.get("/api/active-model").json() == response.json()
    options = client.get("/api/routing-options")
    assert options.status_code == 200
    assert options.json()["client_api_key_required"] is bool(os.getenv("GATEWAY_MASTER_KEY"))
    assert {item["model_id"] for item in options.json()["models"]} == {"minimax", "desktop"}
    advisor = client.post(
        "/api/advisor-model",
        json={"advisor_model": "codex-gpt-6.1-sol-advisor"},
        headers={"origin": "http://testserver"},
    )
    assert advisor.status_code == 200
    assert advisor.json()["advisor_model"] == "codex-gpt-6.1-sol-advisor"
    luna_advisor = client.post(
        "/api/advisor-model",
        json={"advisor_model": "codex-luna-advisor"},
        headers={"origin": "http://testserver"},
    )
    assert luna_advisor.status_code == 200
    assert luna_advisor.json()["advisor_model"] == "codex-luna-advisor"
    invalid = client.post(
        "/api/advisor-model",
        json={"advisor_model": "codex-luna-advisor", "reasoning_effort": "high"},
        headers={"origin": "http://testserver"},
    )
    assert invalid.status_code == 422
    assert control.snapshot().advisor_model == "codex-luna-advisor"
    no_advisor = client.post(
        "/api/advisor-model",
        json={"advisor_model": None},
        headers={"origin": "http://testserver"},
    )
    assert no_advisor.status_code == 422
    assert control.snapshot().advisor_model == "codex-luna-advisor"


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
        "/api/active-model", json={"model": "default", "mode": "alias"}
    )
    assert response.status_code == 422
    assert "not selectable" in response.json()["detail"]


def test_saved_legacy_advisor_policy_is_removed_once(tmp_path):
    control = make_control_plane(tmp_path)
    saved = json.loads(control.state_path.read_text())
    saved.update(policy_version=17, active_model="desktop", mode="force",
                 advisor_model="codex-luna-advisor", advisor_reasoning_effort="high",
                 advisor_sensitivity="frequent", advisor_trigger="adaptive",
                 advisor_classifier="ranked")
    control.state_path.write_text(json.dumps(saved))
    migrated = RoutingControlPlane(control.config_path, control.state_path)
    actual = json.loads(control.state_path.read_text())
    assert actual == {"active_model": "desktop", "mode": "force", "policy_version": 18,
                      "advisor_model": "codex-luna-advisor", "reasoning_effort": None,
                      "fast_mode": False}
    assert migrated.snapshot().advisor_model == "codex-luna-advisor"
    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot() == migrated.snapshot()
    assert json.loads(control.state_path.read_text()) == actual




def test_retired_subcall_metadata_cannot_change_advisor_selection(tmp_path):
    control = make_control_plane(tmp_path)
    control.update_advisor("codex-luna-advisor")
    data = {"model": "default", "messages": [{"role": "user", "content": "Review this"}],
            "metadata": {"advisor_sub_call": True, "gateway_policy": {"advisor_model": None}}}
    asyncio.run(DynamicRoutingPlugin(control).async_pre_call_hook(None, None, data, "acompletion"))
    asyncio.run(AdvisorPlugin().async_pre_call_hook(None, None, data, "acompletion"))
    assert data["model"] == "minimax"
    assert data["metadata"]["gateway_policy"]["advisor_model"] == "codex-luna-advisor"
    assert "tools" not in data
    assert "gateway_advisor_decision" not in data["metadata"]
