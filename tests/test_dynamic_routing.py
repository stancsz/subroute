import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app

import subroute.plugins.dynamic_router as dynamic_routing
from subroute.plugins.dynamic_router import (
    DynamicRoutingPlugin,
    RoutingControlPlane,
)


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
    litellm_params: {model: antigravity/gemini}
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


@pytest.mark.parametrize("protocol", ["acompletion", "aresponses", "anthropic_messages"])
def test_named_api_routes_keep_coding_prompts_on_the_selected_model(tmp_path, protocol):
    control = make_control_plane(tmp_path)
    control.update("desktop")
    content = [{"role": "user", "content": "Create a Docker image for this Python service."}]
    data = {"model": "minimax", "input" if protocol == "aresponses" else "messages": content}

    reply = asyncio.run(DynamicRoutingPlugin(control).async_pre_call_hook({}, None, data, protocol))

    assert reply["model"] == "minimax"
    assert "gateway_image_request" not in reply


def test_candidates_filter_virtual_and_non_selectable_models(tmp_path: Path):
    control = make_control_plane(tmp_path)

    assert [choice.model_id for choice in control.choices] == [
        "minimax",
        "desktop",
        "gemini-subscription",
    ]
    assert control.choices[0].display_name == "MiniMax M3"


def test_default_resolves_without_changing_explicit_models(tmp_path: Path):
    control = make_control_plane(tmp_path)
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "default", "metadata": {"client": "codex"}}

    assert run(plugin, data) is data
    assert data["model"] == "minimax"
    assert data["metadata"] == {
        "client": "codex",
        "gateway_reasoning_effort": None,
        "gateway_target_fast_mode": False,
        "routing": {
            "requested_model": "default",
            "resolved_model": "minimax",

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


def test_default_resolves_to_saved_target(tmp_path: Path):
    control = make_control_plane(tmp_path)
    control.update("desktop")
    data = {"model": "default", "messages": [{"role": "user", "content": "hello"}]}

    assert run(DynamicRoutingPlugin(control), data) is data
    assert data["model"] == "desktop"
    assert data["metadata"]["routing"] == {
        "requested_model": "default", "resolved_model": "desktop",

    }
    assert data["metadata"]["gateway_reasoning_effort"] is None


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
    assert migrated.snapshot().reasoning_effort is None
    assert not hasattr(migrated.snapshot(), "mode")
    persisted = json.loads(control.state_path.read_text(encoding="utf-8"))
    assert persisted["reasoning_effort"] is None
    assert not {"advisor_model", "advisor_reasoning_effort", "advisor_trigger", "advisor_sensitivity", "advisor_classifier"} & persisted.keys()

    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot() == migrated.snapshot()
    assert json.loads(control.state_path.read_text(encoding="utf-8")) == persisted


@pytest.mark.parametrize("retired, replacement", [
    ("codex-terra", "codex-subscription"),
    ("codex-reserve", "codex-luna"),
])
def test_saved_gpt_5_target_migrates_once_and_preserves_policy(tmp_path, retired, replacement):
    config_path = Path(__file__).parents[1] / "config/litellm.yaml"
    state_path = tmp_path / "state.json"
    saved = {
        "active_model": retired, "mode": "force", "policy_version": 12,
        "reasoning_effort": "high",
    }
    state_path.write_text(json.dumps(saved), encoding="utf-8")
    control = RoutingControlPlane(config_path, state_path)
    expected = {
        "active_model": replacement, "reasoning_effort": "high",
        "fast_mode": False,
    }
    assert json.loads(state_path.read_text(encoding="utf-8")) == expected
    assert control.resolve("default")[0] == replacement
    assert retired not in control.allowed_models
    with pytest.raises(ValueError, match="not selectable"):
        control.update(retired)
    restarted = RoutingControlPlane(config_path, state_path)
    assert restarted.snapshot() == control.snapshot()
    assert json.loads(state_path.read_text(encoding="utf-8")) == expected


def test_gpt_6_1_sol_target_is_selectable_and_preserves_effort_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIVE_MODEL", "codex-gpt-6.1-sol")
    config_path = Path(__file__).parents[1] / "config/litellm.yaml"
    state_path = tmp_path / "state.json"
    control = RoutingControlPlane(config_path, state_path)
    control.update("codex-gpt-6.1-sol", reasoning_effort="ultra")
    restarted = RoutingControlPlane(config_path, state_path)
    assert restarted.resolve("default")[0] == "codex-gpt-6.1-sol"
    assert restarted.snapshot().reasoning_effort == "ultra"
    assert "codex-gpt-6.1-sol" in restarted.allowed_models


def test_framework_routing_hook_does_not_add_advisor_guidance_or_tool(tmp_path, monkeypatch):
    import litellm
    from litellm.caching.caching import DualCache
    from litellm.proxy.utils import ProxyLogging
    from litellm.proxy._types import UserAPIKeyAuth

    control = make_control_plane(tmp_path)
    control.update("minimax")
    proxy = ProxyLogging(user_api_key_cache=DualCache())
    monkeypatch.setattr(litellm, "callbacks", [DynamicRoutingPlugin(control)])
    data = {"model": "default", "messages": [{"role": "user", "content": "hello"}]}
    result = asyncio.run(proxy.pre_call_hook(
        user_api_key_dict=UserAPIKeyAuth(), data=data, call_type="anthropic_messages",
    ))
    assert result["model"] == "minimax"
    assert not result.get("tools")
    assert result["messages"] == [{"role": "user", "content": "hello"}]
    assert "gateway_advisor" not in result.get("metadata", {})
    assert "gateway_advisor_decision" not in result.get("metadata", {})

    from litellm.llms.anthropic.pass_through.messages.interceptors.advisor import ANTHROPIC_ADVISOR_TOOL_TYPE

    with pytest.raises(HTTPException, match="retired") as error:
        asyncio.run(DynamicRoutingPlugin(control).async_pre_call_hook({}, None,
            {"model": "default", "tools": [{"type": ANTHROPIC_ADVISOR_TOOL_TYPE}]},
            "anthropic_messages"))
    assert error.value.status_code == 400


def test_update_is_validated_versioned_and_atomically_persisted(tmp_path: Path):
    control = make_control_plane(tmp_path)
    state = control.update("desktop")

    assert json.loads(control.state_path.read_text(encoding="utf-8")) == {
        "active_model": "desktop",

        "reasoning_effort": None, "fast_mode": False,
    }
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError, match="not selectable"):
        control.update("default")
    assert control.snapshot() == state


def test_control_routes_share_one_page_and_update_new_request_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    control = make_control_plane(tmp_path)
    monkeypatch.setattr(dynamic_routing, "control_plane", control)
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.get("/control")
    assert response.status_code == 200
    assert "Save model" in response.text
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

        "reasoning_effort": None, "fast_mode": False,
    }
    assert client.get("/api/active-model").json() == response.json()
    options = client.get("/api/routing-options")
    assert options.status_code == 200
    assert {item["model_id"] for item in options.json()["models"]} == {"minimax", "desktop", "gemini-subscription"}
    assert client.post("/api/advisor-model", json={}).status_code == 404


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
