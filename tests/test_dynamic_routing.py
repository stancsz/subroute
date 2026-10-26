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
    # Respect mode leaves the caller's model for LiteLLM, including aliases.
    passthrough = {"model": "desktop"}
    assert run(plugin, passthrough) is passthrough
    assert passthrough["model"] == "desktop"
    selected = {"model": "default"}
    run(plugin, selected)
    assert selected["model"] == "default"
    unknown = {"model": "vendor/custom-model"}
    run(plugin, unknown)
    assert unknown["model"] == "vendor/custom-model"

    control.update("desktop", "force")
    automatic = {"model": "auto"}
    run(plugin, automatic)
    assert automatic["model"] == "desktop"


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


@pytest.mark.parametrize("mode", ["alias", "force", "off"])
@pytest.mark.parametrize("call_type", ["acompletion", "aresponses", "anthropic_messages"])
def test_unknown_model_uses_saved_default(tmp_path: Path, mode, call_type):
    control = make_control_plane(tmp_path)
    state = control.update("desktop", mode)
    data = {"model": "does-not-exist", "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}],
            "max_tokens": 123}
    if call_type == "aresponses":
        data["input"] = data.pop("messages")
    import copy
    original = copy.deepcopy(data)
    asyncio.run(DynamicRoutingPlugin(control).async_pre_call_hook({}, None, data, call_type))
    expected_model = "does-not-exist" if mode == "off" else "desktop"
    assert data["model"] == expected_model
    assert {key: data[key] for key in original if key != "model"} == {
        key: value for key, value in original.items() if key != "model"}
    metadata = data.get("litellm_metadata", data.get("metadata"))
    if mode == "off":
        assert metadata["routing"] == {"requested_model": "does-not-exist"}
    else:
        assert metadata["routing"] == {"requested_model": "does-not-exist", "resolved_model": "desktop",
                                       "mode": "default_fallback", "policy_version": state.policy_version}
    assert control.snapshot() == state


@pytest.mark.parametrize("requested", ["MiniMax-M3", "minimax/MiniMax-M3", "qwen", "ollama/qwen"])
def test_provider_model_names_resolve_to_configured_deployment(tmp_path, requested):
    control = make_control_plane(tmp_path)
    control.update("desktop", "off")
    data = {"model": requested}
    run(DynamicRoutingPlugin(control), data)
    assert data["model"] == requested
    assert data["metadata"]["routing"]["requested_model"] == requested


@pytest.mark.parametrize("requested", [None, "", "  ", 42, ["minimax"]])
def test_invalid_model_names_remain_validation_errors(tmp_path, requested):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        run(DynamicRoutingPlugin(make_control_plane(tmp_path)), {"model": requested})
    assert error.value.status_code == 400


def test_private_provider_names_do_not_expose_advisor_routes(tmp_path):
    control = make_control_plane(tmp_path)
    for requested in ["codex-advisor/gpt-6.1-sol", "gpt-6.1-sol", "openai/hidden", "hidden"]:
        data = {"model": requested}
        run(DynamicRoutingPlugin(control), data)
        assert data["model"] == control.snapshot().active_model


def test_ambiguous_provider_id_and_fallback_follow_current_default(tmp_path):
    control = make_control_plane(tmp_path)
    config = control.config_path
    config.write_text(config.read_text(encoding="utf-8") + """  - model_name: other-minimax
    litellm_params: {model: another/MiniMax-M3}
""", encoding="utf-8")
    control = RoutingControlPlane(config, control.state_path)
    plugin = DynamicRoutingPlugin(control)
    for selected in ["minimax", "other-minimax", "desktop"]:
        control.update(selected, "alias")
        for requested in ["MiniMax-M3", "unavailable-model"]:
            data = {"model": requested}
            run(plugin, data)
            assert data["model"] == selected
    # A fully qualified name remains unambiguous even when bare IDs collide.
    data = {"model": "minimax/MiniMax-M3"}
    run(plugin, data)
    assert data["model"] == "minimax"


def test_provider_speech_id_preserves_speech_route(tmp_path):
    control = make_control_plane(tmp_path, include_speech_models=True)
    data = {"model": "speech-2.6-hd", "input": "hello"}
    asyncio.run(DynamicRoutingPlugin(control).async_pre_call_hook({}, None, data, "aspeech"))
    assert data["model"] == "minimax-tts"
    assert data["disable_fallbacks"] is True
    assert data["metadata"]["routing"]["mode"] == "speech_synthesis"


def test_litellm_passthrough_ignores_saved_target_and_reasoning_policy(tmp_path):
    control = RoutingControlPlane(
        Path(__file__).parents[1] / "config/litellm.yaml", tmp_path / "policy.json")
    control.update("codex-luna", "off", reasoning_effort="high", fast_mode=True)
    for requested in ["gpt-6-luna", "unknown"]:
        data = {"model": requested, "reasoning_effort": "low"}
        run(DynamicRoutingPlugin(control), data)
        assert data["model"] == requested
        assert data["reasoning_effort"] == "low"
        assert data["metadata"]["gateway_reasoning_effort"] is None
        assert data["metadata"]["gateway_target_fast_mode"] is False


def test_unknown_audio_model_uses_existing_compatible_route(tmp_path, monkeypatch):
    import subroute.audio
    monkeypatch.setattr(subroute.audio, "audio_route", lambda data, call_type: True)
    data = {"model": "unavailable-audio-model"}
    run(DynamicRoutingPlugin(make_control_plane(tmp_path)), data)
    assert data["model"] == "gemini-subscription"
    assert data["disable_fallbacks"] is True


def test_pass_through_mode_leaves_default_alias_for_litellm(tmp_path: Path):
    control = make_control_plane(tmp_path)
    control.update("desktop", "off")
    data = {"model": "default", "messages": [{"role": "user", "content": "hello"}]}

    assert run(DynamicRoutingPlugin(control), data) is data
    assert data["model"] == "default"
    assert data["metadata"]["routing"] == {"requested_model": "default"}
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

    async def prepare_request():
        await router.async_pre_call_hook({}, None, data, "anthropic_messages")
        hook_result = await advisor.async_pre_call_hook({}, None, data, "anthropic_messages")
        request = await advisor.async_pre_request_hook(
            data["model"], data["messages"], {"tools": data.get("tools", [])},
        )
        return hook_result, request

    hook_result, request = asyncio.run(prepare_request())

    assert hook_result is None
    assert data["model"] == "minimax"
    assert "tools" not in data
    assert len(request["tools"]) == 1
    assert request["tools"][0]["type"].startswith("advisor_")
    assert request["tools"][0]["model"] == "experts/codex-gpt-6.1-sol-advisor"
    assert request["tools"][0]["max_uses"] == 1
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


def test_framework_routing_hook_defers_advisor_tool_to_pre_request(tmp_path, monkeypatch):
    import litellm
    from litellm.caching.caching import DualCache
    from litellm.proxy.utils import ProxyLogging
    from litellm.proxy._types import UserAPIKeyAuth

    control = make_control_plane(tmp_path)
    control.update("minimax", "alias")
    proxy = ProxyLogging(user_api_key_cache=DualCache())
    advisor = AdvisorPlugin()
    monkeypatch.setattr(litellm, "callbacks", [DynamicRoutingPlugin(control), advisor])
    data = {"model": "default", "messages": [{"role": "user", "content": "hello"}]}

    async def prepare_request():
        result = await proxy.pre_call_hook(
            user_api_key_dict=UserAPIKeyAuth(), data=data, call_type="anthropic_messages",
        )
        request = await advisor.async_pre_request_hook(
            result["model"], result["messages"], {"tools": result.get("tools", [])},
        )
        return result, request

    result, request = asyncio.run(prepare_request())
    assert result["model"] == "minimax"
    assert "tools" not in result
    assert len(request["tools"]) == 1
    assert request["tools"][0]["type"].startswith("advisor_")
    assert request["tools"][0]["max_uses"] == 1
    assert result["messages"] == [{"role": "user", "content": "hello"}]
    assert "gateway_advisor" not in result.get("metadata", {})
    assert "gateway_advisor_decision" not in result.get("metadata", {})


@pytest.mark.parametrize(("pre_output", "has_original_exception"), [
    (True, False),
    (False, True),
    (False, False),
])
def test_responses_fallback_boundary_blocks_replay_after_output(
    monkeypatch, pre_output, has_original_exception,
):
    from litellm import Router

    original_calls = []

    async def original(self, error, *args, **kwargs):
        original_calls.append((self, error, args, kwargs))
        yield "fallback event"

    monkeypatch.setattr(Router, "_aresponses_fallback_attempt", original)
    monkeypatch.setattr(Router, "_subroute_responses_pre_output_only", False, raising=False)
    dynamic_routing._install_responses_fallback_boundary()
    guarded = Router._aresponses_fallback_attempt

    error = RuntimeError("stream fallback boundary")
    error.is_pre_first_chunk = pre_output
    error.original_exception = ValueError("provider stream failed") if has_original_exception else None
    sentinel = object()

    async def collect():
        return [event async for event in guarded(sentinel, error, "source", marker="preserved")]

    if pre_output:
        assert asyncio.run(collect()) == ["fallback event"]
        assert len(original_calls) == 1
        assert original_calls[0] == (sentinel, error, ("source",), {"marker": "preserved"})
    elif has_original_exception:
        with pytest.raises(ValueError, match="provider stream failed") as raised:
            asyncio.run(collect())
        assert raised.value is error.original_exception
        assert original_calls == []
    else:
        with pytest.raises(RuntimeError, match="stream fallback boundary") as raised:
            asyncio.run(collect())
        assert raised.value is error
        assert original_calls == []


def test_responses_fallback_boundary_rejects_incompatible_router_signature(monkeypatch):
    from litellm import Router

    async def incompatible(self, error):
        yield error

    monkeypatch.setattr(Router, "_aresponses_fallback_attempt", incompatible)
    monkeypatch.setattr(Router, "_subroute_responses_pre_output_only", False, raising=False)

    with pytest.raises(RuntimeError, match="incompatible LiteLLM Responses fallback hook"):
        dynamic_routing._install_responses_fallback_boundary()

    assert Router._aresponses_fallback_attempt is incompatible
    assert Router._subroute_responses_pre_output_only is False


def test_responses_fallback_boundary_rejects_missing_router_hook(monkeypatch):
    from litellm import Router

    monkeypatch.delattr(Router, "_aresponses_fallback_attempt")
    monkeypatch.setattr(Router, "_subroute_responses_pre_output_only", False, raising=False)

    with pytest.raises(RuntimeError, match="incompatible LiteLLM Responses fallback hook"):
        dynamic_routing._install_responses_fallback_boundary()

    assert not hasattr(Router, "_aresponses_fallback_attempt")
    assert Router._subroute_responses_pre_output_only is False


def test_responses_fallback_boundary_rejects_coroutine_router_hook(monkeypatch):
    from litellm import Router

    async def incompatible(self, *args, **kwargs):
        return None

    monkeypatch.setattr(Router, "_aresponses_fallback_attempt", incompatible)
    monkeypatch.setattr(Router, "_subroute_responses_pre_output_only", False, raising=False)

    with pytest.raises(RuntimeError, match="expected an async generator"):
        dynamic_routing._install_responses_fallback_boundary()

    assert Router._aresponses_fallback_attempt is incompatible
    assert Router._subroute_responses_pre_output_only is False




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
