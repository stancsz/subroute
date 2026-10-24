import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import litellm
import yaml
import pytest

from subroute.plugins.dynamic_router import DynamicRoutingPlugin


ROOT = Path(__file__).resolve().parents[1]
FLASH_MODEL = "openrouter/minimax/minimax-m3.1-flash"
DIRECT_FLASH_MODEL = "minimax/MiniMax-M3.1-Flash"
FLASH_ALIASES = {"auto", "openrouter", "openrouter-flash", "minimax-flash"}


@contextmanager
def _mini_max_wire_fixture():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            if self.path.endswith("/messages"):
                body = {"id": "msg_fixture", "type": "message", "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}], "model": "fixture",
                        "stop_reason": "end_turn", "stop_sequence": None,
                        "usage": {"input_tokens": 3, "output_tokens": 2}}
            else:
                body = {"id": "chatcmpl_fixture", "object": "chat.completion", "created": 1,
                        "model": "fixture", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"}}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_flash_aliases_keep_reasoning_visible_and_advertise_efforts():
    config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text(encoding="utf-8"))
    models = {entry["model_name"]: entry for entry in config["model_list"]}
    for alias in FLASH_ALIASES:
        entry = models[alias]
        assert entry["litellm_params"]["model"] == FLASH_MODEL
        assert entry["litellm_params"]["allowed_openai_params"] == ["reasoning"]
        assert entry["model_info"]["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert models["minimax"]["litellm_params"]["model"] == "minimax/MiniMax-M3"
    direct = models["minimax-flash-direct"]
    assert direct["litellm_params"]["model"] == DIRECT_FLASH_MODEL
    assert direct["litellm_params"]["api_key"] == "os.environ/MINIMAX_API_KEY"
    assert "api_base" not in direct["litellm_params"]
    assert direct["litellm_params"]["allowed_openai_params"] == ["reasoning_effort"]
    assert direct["model_info"]["provider"] == "MiniMax"
    assert direct["model_info"]["source_id"] == "minimax"
    assert direct["model_info"]["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max"]


@pytest.fixture
def plugin():
    return DynamicRoutingPlugin.__new__(DynamicRoutingPlugin)


@pytest.mark.parametrize(("call_type", "expected"), [
    ("acompletion", {"reasoning": {"effort": "medium"}}),
    ("aresponses", {"reasoning": {"effort": "medium"}}),
    ("aanthropic_messages", {"reasoning": {"effort": "medium"}}),
])
def test_flash_gets_medium_default_at_protocol_hook(plugin, call_type, expected):
    request = {
        "model": FLASH_MODEL,
        "messages": [{"role": "assistant", "content": "continuing", "reasoning_content": "plan", "tool_calls": [{"id": "t1"}]}],
        "metadata": {},
    }
    original_messages = deepcopy(request["messages"])
    result = asyncio.run(plugin.async_pre_call_deployment_hook(request, call_type))
    assert result is not None
    assert {key: result[key] for key in expected} == expected
    assert request["messages"] == original_messages
    assert result.get("reasoning_effort") is None


@pytest.mark.parametrize(("caller_kwargs", "expected_reasoning"), [
    ({"reasoning_effort": "high"}, {"effort": "high"}),
    ({"reasoning_effort": {"effort": "high", "summary": "auto"}}, {"effort": "high", "summary": "auto"}),
    ({"reasoning": {"effort": "high", "summary": "auto"}}, {"effort": "high", "summary": "auto"}),
])
def test_explicit_caller_effort_maps_to_native_reasoning(plugin, caller_kwargs, expected_reasoning):
    request = {**caller_kwargs, "model": FLASH_MODEL, "metadata": {}}
    result = asyncio.run(plugin.async_pre_call_deployment_hook(request, "aresponses"))
    assert result["reasoning"] == expected_reasoning
    assert "reasoning_effort" not in result


@pytest.mark.parametrize("caller_kwargs", [
    {"reasoning": {"max_tokens": 1500}},
    {"reasoning": {"max_tokens": 1500, "summary": "auto"}},
    {"output_config": {"budget_tokens": 1500}},
    {"thinking": {"type": "enabled", "budget_tokens": 1500}},
])
def test_explicit_effort_or_reasoning_budget_beats_default(plugin, caller_kwargs):
    original = {**caller_kwargs, "model": FLASH_MODEL, "metadata": {}}
    result = asyncio.run(plugin.async_pre_call_deployment_hook(original, "aresponses"))
    assert result is None
    assert original == {**caller_kwargs, "model": FLASH_MODEL, "metadata": {}}


@pytest.mark.parametrize(("carrier", "value"), [
    ("reasoning", "off"),
    ("reasoning", ["malformed"]),
    ("output_config", False),
])
def test_malformed_effort_carrier_is_not_repaired(plugin, carrier, value):
    original = {"model": FLASH_MODEL, "metadata": {}, carrier: value}
    before = deepcopy(original)
    assert asyncio.run(plugin.async_pre_call_deployment_hook(original, "aresponses")) is None
    assert original == before


def test_routing_desk_effort_still_overrides_caller_and_keeps_other_fields(plugin):
    original = {
        "model": FLASH_MODEL,
        "reasoning": {"effort": "low", "summary": "auto", "max_tokens": 900},
        "messages": [{"role": "assistant", "content": "", "reasoning_content": "private", "tool_calls": [{"id": "t1"}]}],
        "metadata": {"gateway_reasoning_effort": "high"},
    }
    result = asyncio.run(plugin.async_pre_call_deployment_hook(original, "aresponses"))
    assert result["reasoning"] == {"effort": "high", "summary": "auto", "max_tokens": 900}
    assert result["messages"] == original["messages"]
    assert original["reasoning"]["effort"] == "low"


@pytest.mark.parametrize("model", ["openai/gpt-6-luna", "minimax/MiniMax-M3", "codex-advisor/gpt-6.1-sol"])
def test_default_does_not_affect_other_models_or_advisor(plugin, model):
    request = {"model": model, "metadata": {"advisor_sub_call": True}}
    assert asyncio.run(plugin.async_pre_call_deployment_hook(request, "acompletion")) is None


def test_fallback_attempt_model_identity_does_not_inherit_flash_default(plugin):
    request = {
        "model": "openai/gpt-6-luna",
        "metadata": {"routing": {"resolved_model": "minimax-flash"}},
    }
    assert asyncio.run(plugin.async_pre_call_deployment_hook(request, "acompletion")) is None


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize(("overrides", "effort"), [
    ({}, "medium"),
    ({"reasoning_effort": "high"}, "high"),
    ({"reasoning_effort": "max"}, "max"),
])
def test_pinned_proxy_emits_native_reasoning_for_flash(protocol, overrides, effort, tmp_path):
    from test_thinking_format import provider_server
    from test_codex_proxy_http import _configured_proxy, _request

    config = tmp_path / "models.yaml"
    config.write_text(
        "model_list:\n  - model_name: codex-luna\n"
        f"    litellm_params: {{model: {FLASH_MODEL}}}\n"
        "  - model_name: codex-gpt-6.1-sol-advisor\n"
        "    model_info: {selectable: false, advisor_selectable: true}\n"
        "    litellm_params: {model: codex-advisor/gpt-6.1-sol}\n",
        encoding="utf-8",
    )
    state = tmp_path / "state.json"
    state.write_text(
        '{"active_model":"codex-luna","mode":"off",'
        '"advisor_model":"codex-gpt-6.1-sol-advisor"}', encoding="utf-8"
    )
    from subroute.plugins.dynamic_router import RoutingControlPlane
    plugin = DynamicRoutingPlugin(RoutingControlPlane(config, state))
    with provider_server(False, "reasoning") as (base, calls):
        deployments = [{
            "model_name": "codex-luna",
            "litellm_params": {
                "model": FLASH_MODEL, "api_base": base + "/v1", "api_key": "fixture",
                "allowed_openai_params": ["reasoning"],
            },
        }]
        with patch.object(litellm, "use_chat_completions_url_for_anthropic_messages", True), \
                _configured_proxy(provider_model_list=deployments, dynamic_routing_plugin=plugin) as (client, *_):
            path, payload = _request(protocol, False)
            payload.update(overrides)
            payload["max_tokens"] = 1777
            response = client.post(path, json=payload)
    # The shared fixture returns a Chat Completion body, which LiteLLM cannot
    # parse as a Responses response (it lacks created_at). The outbound HTTP
    # request is still captured after pinned-package validation and is the
    # contract under test for this Responses case.
    if protocol != "responses":
        assert response.status_code == 200, response.text[:1000]
    assert len(calls) == 1
    sent = calls[0][1]
    assert sent["reasoning"]["effort"] == effort
    assert "reasoning_effort" not in sent
    if "max_tokens" in overrides.get("reasoning", {}):
        assert sent["reasoning"]["max_tokens"] == 1700
        assert sent["reasoning"]["summary"] == "auto"


@pytest.mark.parametrize(("metadata", "caller", "expected"), [
    ({}, {"reasoning_effort": "high"}, "high"),
    ({"gateway_reasoning_effort": "max"}, {"reasoning_effort": "low"}, "max"),
])
def test_direct_flash_effort_precedence_is_preserved(plugin, metadata, caller, expected):
    request = {"model": DIRECT_FLASH_MODEL, "metadata": metadata, **caller}
    result = asyncio.run(plugin.async_pre_call_deployment_hook(request, "acompletion"))
    assert result["reasoning_effort"] == expected


def test_provider_replay_keeps_assistant_reasoning_and_tool_history(tmp_path):
    from test_thinking_format import provider_server
    from test_codex_proxy_http import _configured_proxy, _request
    from subroute.plugins.dynamic_router import RoutingControlPlane

    config = tmp_path / "models.yaml"
    config.write_text(
        "model_list:\n  - model_name: codex-luna\n"
        f"    litellm_params: {{model: {FLASH_MODEL}}}\n"
        "  - model_name: codex-gpt-6.1-sol-advisor\n"
        "    model_info: {selectable: false, advisor_selectable: true}\n"
        "    litellm_params: {model: codex-advisor/gpt-6.1-sol}\n",
        encoding="utf-8",
    )
    state = tmp_path / "state.json"
    state.write_text(
        '{"active_model":"codex-luna","mode":"off",'
        '"advisor_model":"codex-gpt-6.1-sol-advisor"}', encoding="utf-8"
    )
    plugin = DynamicRoutingPlugin(RoutingControlPlane(config, state))
    history = [
        {"role": "assistant", "content": "partial", "reasoning_content": "retain this", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":1}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]
    with provider_server(False, "reasoning") as (base, calls):
        deployments = [{"model_name": "codex-luna", "litellm_params": {
            "model": FLASH_MODEL, "api_base": base + "/v1", "api_key": "fixture",
            "allowed_openai_params": ["reasoning"],
        }}]
        with patch.object(litellm, "use_chat_completions_url_for_anthropic_messages", True), \
                _configured_proxy(provider_model_list=deployments, dynamic_routing_plugin=plugin) as (client, *_):
                path, payload = _request("chat", False)
                payload["messages"] = history
                response = client.post(path, json=payload)
    assert response.status_code == 200, response.text[:1000]
    assert len(calls) == 1
    assert calls[0][1]["messages"] == history
