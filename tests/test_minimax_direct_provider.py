import json
from pathlib import Path
import asyncio
from copy import deepcopy

import httpx
import litellm
import pytest
import yaml
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from subroute.plugins.dynamic_router import DynamicRoutingPlugin


ROOT = Path(__file__).resolve().parents[1]
DIRECT_MODEL = "minimax/MiniMax-M3.1-Flash"


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("effort", ["medium", "high", "max"])
def test_configured_direct_alias_reaches_native_endpoint(protocol, effort, monkeypatch):
    config = yaml.safe_load((ROOT / "config/litellm.yaml").read_text(encoding="utf-8"))
    entry = next(e for e in config["model_list"] if e["model_name"] == "minimax-flash-direct")
    params = entry["litellm_params"]
    assert params["model"] == DIRECT_MODEL
    assert params["api_key"] == "os.environ/MINIMAX_API_KEY"
    assert "api_base" not in params
    assert params["allowed_openai_params"] == ["reasoning_effort"]
    assert entry["model_info"]["supports_reasoning"] is True
    assert entry["model_info"]["supports_adaptive_thinking"] is True
    assert entry["model_info"]["supports_output_config"] is True
    monkeypatch.setattr(litellm, "model_cost", deepcopy(litellm.model_cost))
    litellm.Router(model_list=[{**entry, "litellm_params": {**params, "api_key": "fixture"}}])
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={
                "id": "chatcmpl-fixture", "object": "chat.completion", "created": 1700000000,
                "model": "MiniMax-M3.1-Flash",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": "WIRE_OK", "reasoning_content": "fixture reasoning"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            })
        if request.url.path == "/anthropic/v1/messages":
            return httpx.Response(200, json={
                "id": "msg_fixture", "type": "message", "role": "assistant",
                "model": "MiniMax-M3.1-Flash", "content": [{"type": "text", "text": "WIRE_OK"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            })
        return httpx.Response(404, json={"error": {"message": "Wrong native endpoint", "type": "invalid_request_error"}})

    async def check():
        kwargs = {**params, "api_key": "fixture", "metadata": {}}
        if effort != "medium":
            kwargs["reasoning_effort"] = effort
        call_type = {"chat": "acompletion", "responses": "aresponses", "messages": "aanthropic_messages"}[protocol]
        if protocol == "responses":
            kwargs.update(input="Reply WIRE_OK", max_output_tokens=4096)
        else:
            kwargs.update(messages=[{"role": "user", "content": "Reply WIRE_OK"}], max_tokens=4096)
        plugin = DynamicRoutingPlugin.__new__(DynamicRoutingPlugin)
        kwargs = await plugin.async_pre_call_deployment_hook(kwargs, call_type) or kwargs
        client = AsyncHTTPHandler(transport=httpx.MockTransport(respond))
        try:
            if protocol == "chat":
                response = await litellm.acompletion(**kwargs, client=client, num_retries=0)
                assert response.choices[0].finish_reason == "stop"
                assert response.choices[0].message.content == "WIRE_OK"
                assert response.usage.prompt_tokens == 3
                assert response.usage.completion_tokens == 2
            elif protocol == "responses":
                response = await litellm.aresponses(**kwargs, client=client, num_retries=0)
                assert response.status == "completed"
                assert response.usage.input_tokens == 3
                assert response.usage.output_tokens == 2
            else:
                response = await litellm.anthropic.messages.acreate(**kwargs, client=client, num_retries=0)
                assert response["stop_reason"] == "end_turn"
                assert response["usage"]["input_tokens"] == 3
                assert response["usage"]["output_tokens"] == 2
            assert len(seen) == 1
            path, body = seen[0]
            expected_path = {"chat": "/v1/chat/completions", "responses": "/v1/chat/completions",
                             "messages": "/anthropic/v1/messages"}[protocol]
            assert path == expected_path
            assert body["model"] == "MiniMax-M3.1-Flash"
            assert body.get("max_tokens", body.get("max_completion_tokens", body.get("max_output_tokens"))) == 4096
            if protocol == "messages":
                assert body["output_config"]["effort"] == effort
            else:
                assert body["reasoning_effort"] == effort
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize(("call_type", "control"), [
    ("acompletion", {"thinking": {"type": "enabled", "budget_tokens": 1500}}),
    ("aresponses", {"output_config": {"budget_tokens": 1500}}),
    ("aresponses", {"reasoning": {"max_tokens": 1500, "summary": "auto"}}),
])
def test_direct_flash_preserves_explicit_thinking_and_budget(call_type, control):
    # Keep native Anthropic controls intact instead of layering a default effort on them.
    plugin = DynamicRoutingPlugin.__new__(DynamicRoutingPlugin)
    request = {"model": DIRECT_MODEL, "metadata": {}, **control}
    original = json.loads(json.dumps(request))
    result = asyncio.run(plugin.async_pre_call_deployment_hook(request, call_type))
    assert result is None
    assert request == original


def test_old_minimax_m3_does_not_receive_flash_effort_default():
    plugin = DynamicRoutingPlugin.__new__(DynamicRoutingPlugin)
    request = {"model": "minimax/MiniMax-M3", "metadata": {}}
    result = asyncio.run(plugin.async_pre_call_deployment_hook(request, "acompletion"))
    assert result is None
    assert "reasoning_effort" not in request
