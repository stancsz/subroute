import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app

import subroute.plugins.dynamic_router as dynamic_routing
from subroute.plugins.dynamic_router import (
    DynamicRoutingPlugin,
    RoutingControlPlane,
)
from subroute.plugins.advisor_plugin import ADVISOR_TOOL_TYPE, AdvisorPlugin


def make_control_plane(tmp_path: Path, include_speech_models: bool = False) -> RoutingControlPlane:
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


def test_alias_mode_resolves_current_but_preserves_explicit_models(tmp_path: Path):
    control = make_control_plane(tmp_path)
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "current", "metadata": {"client": "codex"}}

    assert run(plugin, data) is data
    assert data["model"] == "minimax"
    assert data["metadata"] == {
        "client": "codex",
        "gateway_reasoning_effort": None,
        "gateway_policy": {
            "active_model": "minimax", "mode": "alias", "policy_version": 1,
            "advisor_model": "codex-luna-advisor",
        "reasoning_effort": None, "advisor_reasoning_effort": "high",
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

    for alias in ("default",):
        compatible = {"model": alias}
        run(plugin, compatible)
        assert compatible["model"] == "minimax"

    automatic = {"model": "auto"}
    run(plugin, automatic)
    assert automatic["model"] == "auto"
    assert automatic["metadata"]["gateway_reasoning_effort"] is None
    assert "routing" not in automatic["metadata"]


def test_force_and_off_modes_have_explicit_semantics(tmp_path: Path):
    control = make_control_plane(tmp_path)
    plugin = DynamicRoutingPlugin(control)

    force = control.update("desktop", "force")
    data = {"model": "minimax"}
    run(plugin, data)
    assert data["model"] == "desktop"
    assert data["metadata"]["routing"]["policy_version"] == force.policy_version

    control.update("minimax", "off")
    passthrough = {"model": "current"}
    assert run(plugin, passthrough) is passthrough
    assert passthrough["model"] == "current"


def test_unknown_model_is_rejected_before_force_routing(tmp_path: Path):
    from fastapi import HTTPException

    control = make_control_plane(tmp_path)
    control.update("desktop", "force")
    data = {"model": "does-not-exist"}

    with pytest.raises(HTTPException) as error:
        run(DynamicRoutingPlugin(control), data)

    assert error.value.status_code == 404
    assert data["model"] == "does-not-exist"
    assert data["metadata"] == {}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/v1/skills", None),
        ("post", "/v1/rerank", {"model": "current", "query": "q", "documents": ["d"]}),
        ("post", "/v1/embeddings", {"model": "current", "input": "x"}),
    ],
)
def test_unconfigured_provider_operations_return_client_errors(method, path, body):
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = getattr(client, method)(path, json=body) if body is not None else client.get(path)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_operation"


def test_routing_precedes_advisor_injection_for_plain_target(tmp_path: Path):
    control = make_control_plane(tmp_path)
    control.update("minimax", "alias")
    # Capture the intended advisor in the same policy snapshot as the target.
    # Changing the persisted policy after the router hook would leave this
    # request on its original snapshot and could invoke a live Gemini advisor.
    control.update_advisor("codex-gpt-6.1-sol-advisor")
    router = DynamicRoutingPlugin(control)
    advisor = AdvisorPlugin()
    data = {"model": "current", "messages": [{"role": "user", "content": "help"}]}

    asyncio.run(router.async_pre_call_hook({}, None, data, "anthropic_messages"))
    asyncio.run(advisor.async_pre_call_hook({}, None, data, "anthropic_messages"))

    assert data["model"] == "minimax"
    assert data["tools"][0]["type"] == ADVISOR_TOOL_TYPE
    assert data["tools"][0]["model"] == "codex-gpt-6.1-sol-advisor"


def test_persisted_advisor_wins_over_startup_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ADVISOR_MODEL", "codex-gpt-6.1-sol-advisor")
    control = make_control_plane(tmp_path)
    assert control.snapshot().advisor_model == "codex-gpt-6.1-sol-advisor"
    control.update_advisor("codex-sol-advisor")
    monkeypatch.setenv("ADVISOR_MODEL", "gemini-subscription")
    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot().advisor_model == "codex-sol-advisor"


def test_default_desk_uses_openrouter_and_luna_high_without_locking_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIVE_MODEL", raising=False)
    monkeypatch.delenv("ADVISOR_MODEL", raising=False)
    config_path = Path(__file__).parents[1] / "config/litellm.yaml"
    state_path = tmp_path / "state.json"
    control = RoutingControlPlane(config_path, state_path)
    assert control.snapshot().active_model == "openrouter"
    assert control.snapshot().advisor_model == "codex-luna-advisor"
    assert control.snapshot().advisor_reasoning_effort == "high"
    for model in (
        "codex-sol-advisor", "codex-astra-advisor", "codex-gpt-6.1-sol-advisor",
        "codex-luna-advisor",
    ):
        control.update_advisor(model, reasoning_effort="medium")
        restarted = RoutingControlPlane(config_path, state_path)
        assert restarted.snapshot().advisor_model == model
        assert restarted.snapshot().advisor_reasoning_effort == "medium"


def test_advisor_is_optional_and_requests_skip_injection(tmp_path: Path):
    control = make_control_plane(tmp_path)
    disabled = control.update_advisor(None)

    assert disabled.advisor_model is None
    assert json.loads(control.state_path.read_text(encoding="utf-8"))["advisor_model"] is None

    router = DynamicRoutingPlugin(control)
    advisor = AdvisorPlugin()
    data = {"model": "minimax", "messages": [{"role": "user", "content": "help"}]}
    asyncio.run(router.async_pre_call_hook({}, None, data, "anthropic_messages"))
    asyncio.run(advisor.async_pre_call_hook({}, None, data, "anthropic_messages"))

    assert data["metadata"]["gateway_policy"]["advisor_model"] is None
    assert "tools" not in data


def test_framework_runs_routing_before_advisor(tmp_path, monkeypatch):
    import litellm
    from litellm.caching.caching import DualCache
    from litellm.proxy.utils import ProxyLogging
    from litellm.proxy._types import UserAPIKeyAuth

    control = make_control_plane(tmp_path)
    control.update("minimax", "alias")
    control.update_advisor("codex-gpt-6.1-sol-advisor")
    proxy = ProxyLogging(user_api_key_cache=DualCache())
    monkeypatch.setattr(litellm, "callbacks", [DynamicRoutingPlugin(control), AdvisorPlugin()])
    result = asyncio.run(proxy.pre_call_hook(
        user_api_key_dict=UserAPIKeyAuth(),
        data={"model": "current", "messages": [{"role": "user", "content": "hello"}]},
        call_type="anthropic_messages",
    ))
    assert result["model"] == "minimax"
    assert result["tools"][0]["model"] == "codex-gpt-6.1-sol-advisor"
    assert result["metadata"]["gateway_policy"]["policy_version"] == control.snapshot().policy_version


def test_update_is_validated_versioned_and_atomically_persisted(tmp_path: Path):
    control = make_control_plane(tmp_path)
    state = control.update("desktop", "force")

    assert state.policy_version == 2
    assert json.loads(control.state_path.read_text(encoding="utf-8")) == {
        "active_model": "desktop",
        "mode": "force",
        "policy_version": 2,
        "advisor_model": "codex-luna-advisor",
        "reasoning_effort": None, "advisor_reasoning_effort": "high",
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

    response = client.get("/control")
    assert response.status_code == 200
    assert "Connections" in response.text
    assert "Routing desk" in response.text
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
        "advisor_model": "codex-luna-advisor",
        "reasoning_effort": None, "advisor_reasoning_effort": "high",
    }
    assert client.get("/api/active-model").json() == response.json()
    options = client.get("/api/routing-options")
    assert options.status_code == 200
    assert options.json()["client_api_key_required"] is False
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
    no_advisor = client.post(
        "/api/advisor-model",
        json={"advisor_model": None},
        headers={"origin": "http://testserver"},
    )
    assert no_advisor.status_code == 200
    assert no_advisor.json()["advisor_model"] is None


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
