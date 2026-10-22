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
    assert {k:v for k,v in data["metadata"].items() if not k.startswith("_gateway_analytics_")} == {
        "client": "codex",
        "gateway_reasoning_effort": None,
        "gateway_target_fast_mode": False,
        "gateway_policy": {
            "active_model": "minimax", "mode": "alias", "policy_version": 1,
            "advisor_model": "codex-gpt-6.1-sol-advisor",
        "reasoning_effort": None, "fast_mode": False, "advisor_reasoning_effort": "low", "advisor_trigger": "adaptive",
        "advisor_sensitivity": "balanced", "advisor_classifier": "ranked",
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
    passthrough = {"model": "default"}
    assert run(plugin, passthrough) is passthrough
    assert passthrough["model"] == "default"


def test_unknown_model_is_rejected_before_force_routing(tmp_path: Path):
    from fastapi import HTTPException

    control = make_control_plane(tmp_path)
    control.update("desktop", "force")
    data = {"model": "does-not-exist"}

    with pytest.raises(HTTPException) as error:
        run(DynamicRoutingPlugin(control), data)

    assert error.value.status_code == 404
    assert data["model"] == "does-not-exist"
    assert {k:v for k,v in data["metadata"].items() if not k.startswith("_gateway_analytics_")} == {}


def test_default_resolves_to_saved_target_even_when_alias_mode_is_off(tmp_path: Path):
    control = make_control_plane(tmp_path)
    control.update("desktop", "off")
    data = {"model": "default", "messages": [{"role": "user", "content": "hello"}]}

    assert run(DynamicRoutingPlugin(control), data) is data
    assert data["model"] == "desktop"
    assert data["metadata"]["routing"]["resolved_model"] == "desktop"
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


def test_routing_precedes_advisor_injection_for_plain_target(tmp_path: Path, monkeypatch):
    async def score(self, prompt):
        return .1

    from subroute.advisor_ranker import AdvisorRequestRanker
    from subroute.advisor_frequency import AdvisorFrequencyGate
    monkeypatch.setattr(AdvisorRequestRanker, "rank", score)
    monkeypatch.setattr(AdvisorFrequencyGate, "choose", lambda self, *args: {
        "consult": False, "score": .1, "target_rate": .5,
        "selected_count": 0, "eligible_count": 1,
    })
    control = make_control_plane(tmp_path)
    control.update("minimax", "alias")
    # Capture the intended advisor in the same policy snapshot as the target.
    # Changing the persisted policy after the router hook would leave this
    # request on its original snapshot and could invoke a live Gemini advisor.
    control.update_advisor("codex-gpt-6.1-sol-advisor")
    router = DynamicRoutingPlugin(control)
    advisor = AdvisorPlugin()
    data = {"model": "default", "messages": [{"role": "user", "content": "help"}]}

    asyncio.run(router.async_pre_call_hook({}, None, data, "anthropic_messages"))
    asyncio.run(advisor.async_pre_call_hook({}, None, data, "anthropic_messages"))

    assert data["model"] == "minimax"
    assert "tools" not in data
    assert data["metadata"]["gateway_advisor_decision"]["source"] == "ranked"
    assert data["metadata"]["gateway_advisor"]["model"] == "codex-gpt-6.1-sol-advisor"


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
    assert migrated.snapshot().advisor_reasoning_effort == "low"
    assert migrated.snapshot().advisor_sensitivity == "frequent"
    persisted = json.loads(control.state_path.read_text(encoding="utf-8"))
    assert persisted["reasoning_effort"] is None
    assert persisted["policy_version"] == 13

    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot() == migrated.snapshot()
    assert json.loads(control.state_path.read_text(encoding="utf-8")) == persisted


def test_default_desk_uses_luna_target_and_sol_low_without_locking_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIVE_MODEL", raising=False)
    monkeypatch.delenv("ADVISOR_MODEL", raising=False)
    config_path = Path(__file__).parents[1] / "config/litellm.yaml"
    state_path = tmp_path / "state.json"
    control = RoutingControlPlane(config_path, state_path)
    assert control.snapshot().active_model == "codex-luna"
    assert control.snapshot().advisor_model == "codex-gpt-6.1-sol-advisor"
    assert control.snapshot().advisor_reasoning_effort == "low"
    for model in (
        "codex-sol-advisor", "codex-astra-advisor", "codex-gpt-6.1-sol-advisor",
        "codex-luna-advisor",
    ):
        control.update_advisor(model, reasoning_effort="medium")
        restarted = RoutingControlPlane(config_path, state_path)
        assert restarted.snapshot().advisor_model == model
        assert restarted.snapshot().advisor_reasoning_effort == "medium"


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
        "reasoning_effort": "high", "advisor_reasoning_effort": "low",
    }
    state_path.write_text(json.dumps(saved), encoding="utf-8")
    control = RoutingControlPlane(config_path, state_path)
    expected = {
        **saved, "active_model": replacement, "policy_version": 13,
        "advisor_trigger": "adaptive", "advisor_sensitivity": "balanced", "advisor_classifier": "ranked",
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
    from subroute.advisor_ranker import AdvisorRequestRanker
    from subroute.advisor_frequency import AdvisorFrequencyGate
    async def score(self, prompt):
        return .1
    monkeypatch.setattr(AdvisorRequestRanker, "rank", score)
    monkeypatch.setattr(AdvisorFrequencyGate, "choose", lambda self, *args: {
        "consult": False, "score": .1, "target_rate": .5,
        "selected_count": 0, "eligible_count": 1,
    })
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
        data={"model": "default", "messages": [{"role": "user", "content": "hello"}]},
        call_type="anthropic_messages",
    ))
    assert result["model"] == "minimax"
    assert "tools" not in result
    assert result["metadata"]["gateway_advisor"]["model"] == "codex-gpt-6.1-sol-advisor"
    assert result["metadata"]["gateway_advisor_decision"]["source"] == "ranked"
    assert result["metadata"]["gateway_policy"]["policy_version"] == control.snapshot().policy_version


def test_update_is_validated_versioned_and_atomically_persisted(tmp_path: Path):
    control = make_control_plane(tmp_path)
    state = control.update("desktop", "force")

    assert state.policy_version == 2
    assert json.loads(control.state_path.read_text(encoding="utf-8")) == {
        "active_model": "desktop",
        "mode": "force",
        "policy_version": 2,
        "advisor_model": "codex-gpt-6.1-sol-advisor",
        "reasoning_effort": None, "fast_mode": False, "advisor_reasoning_effort": "low", "advisor_trigger": "adaptive",
        "advisor_sensitivity": "balanced", "advisor_classifier": "ranked",
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
        "reasoning_effort": None, "fast_mode": False, "advisor_reasoning_effort": "low", "advisor_trigger": "adaptive",
        "advisor_sensitivity": "balanced", "advisor_classifier": "ranked",
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
    adaptive = client.post(
        "/api/advisor-model",
        json={
            "advisor_model": "codex-luna-advisor",
            "advisor_sensitivity": "conservative",
        },
        headers={"origin": "http://testserver"},
    )
    assert adaptive.status_code == 200
    assert adaptive.json()["advisor_trigger"] == "adaptive"
    assert adaptive.json()["advisor_sensitivity"] == "conservative"
    assert adaptive.json()["advisor_classifier"] == "ranked"
    assert RoutingControlPlane(control.config_path, control.state_path).snapshot().advisor_trigger == "adaptive"
    invalid = client.post(
        "/api/advisor-model",
        json={"advisor_model": "codex-luna-advisor", "advisor_trigger": "random"},
        headers={"origin": "http://testserver"},
    )
    assert invalid.status_code == 422
    assert control.snapshot().advisor_trigger == "adaptive"
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
        "/api/active-model", json={"model": "default", "mode": "alias"}
    )
    assert response.status_code == 422
    assert "not selectable" in response.json()["detail"]


@pytest.mark.parametrize("trigger", ["model", "hybrid", "always", "adaptive"])
@pytest.mark.parametrize("classifier", ["local", "rules", "ranked"])
def test_saved_legacy_selection_migrates_once_and_preserves_other_settings(tmp_path, trigger, classifier):
    control = make_control_plane(tmp_path)
    saved = json.loads(control.state_path.read_text())
    saved.update(policy_version=17, active_model="desktop", mode="force",
                 advisor_model="codex-luna-advisor", advisor_reasoning_effort="high",
                 advisor_sensitivity="frequent", advisor_trigger=trigger,
                 advisor_classifier=classifier)
    control.state_path.write_text(json.dumps(saved))
    migrated = RoutingControlPlane(control.config_path, control.state_path)
    actual = json.loads(control.state_path.read_text())
    assert actual == {**saved, "advisor_trigger": "adaptive", "advisor_classifier": "ranked",
                      "policy_version": 17 if (trigger, classifier) == ("adaptive", "ranked") else 18}
    assert migrated.snapshot().advisor_trigger == "adaptive"
    assert migrated.snapshot().advisor_classifier == "ranked"
    restarted = RoutingControlPlane(control.config_path, control.state_path)
    assert restarted.snapshot() == migrated.snapshot()
    assert json.loads(control.state_path.read_text()) == actual


@pytest.mark.parametrize("field,value", [
    ("advisor_trigger", "model"), ("advisor_trigger", "always"),
    ("advisor_trigger", "adaptive"), ("advisor_classifier", "rules"),
    ("advisor_classifier", "local"), ("advisor_classifier", "ranked"),
])
def test_control_api_rejects_removed_selection_writes_without_policy_change(tmp_path, monkeypatch, field, value):
    control = make_control_plane(tmp_path)
    monkeypatch.setattr(dynamic_routing, "control_plane", control)
    before = control.snapshot()
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.post("/api/advisor-model", json={"advisor_model": "codex-luna-advisor", field: value})
    assert response.status_code == 422
    assert control.snapshot() == before


def test_retired_subcall_metadata_cannot_bypass_authoritative_routing_and_ranking(tmp_path, monkeypatch):
    from subroute.advisor_ranker import AdvisorRequestRanker
    from subroute.advisor_frequency import AdvisorFrequencyGate
    calls = []
    async def score(self, prompt):
        calls.append(prompt)
        return .1
    monkeypatch.setattr(AdvisorRequestRanker, "rank", score)
    monkeypatch.setattr(AdvisorFrequencyGate, "choose", lambda self, *args: {
        "consult": False, "score": .1, "target_rate": .5,
        "selected_count": 0, "eligible_count": 1,
    })
    control = make_control_plane(tmp_path)
    control.update_advisor("codex-luna-advisor")
    data = {"model": "default", "messages": [{"role": "user", "content": "Review this"}],
            "metadata": {"advisor_sub_call": True, "gateway_policy": {"advisor_model": None}}}
    asyncio.run(DynamicRoutingPlugin(control).async_pre_call_hook(None, None, data, "acompletion"))
    asyncio.run(AdvisorPlugin().async_pre_call_hook(None, None, data, "acompletion"))
    assert data["model"] == "minimax"
    assert data["metadata"]["gateway_policy"]["advisor_model"] == "codex-luna-advisor"
    assert len(calls) == 1
    assert data["metadata"]["gateway_advisor_decision"]["source"] == "ranked"
