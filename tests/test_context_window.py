"""Exercise the installed LiteLLM checks, without making provider calls."""

from pathlib import Path

import litellm
import asyncio
import pytest
import yaml


def router():
    settings = yaml.safe_load((Path(__file__).parents[1] / "config/litellm.yaml").read_text())
    return litellm.Router(model_list=[{
        "model_name": "context-fixture",
        "model_info": settings["model_list"][0]["model_info"],
        "litellm_params": {"model": "openai/gpt-4o", "api_key": "fixture-only"},
    }], enable_pre_call_checks=True, num_retries=0, fallbacks=[])


def test_native_check_accepts_the_boundary_and_rejects_the_next_token():
    r = router()
    request = dict(model="context-fixture", healthy_deployments=r.model_list,
                   messages=[{"role": "user", "content": "ok"}])
    assert r._pre_call_checks(**request, input_token_count=256000) == r.model_list
    with pytest.raises(litellm.ContextWindowExceededError):
        r._pre_call_checks(**request, input_token_count=256001)


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
def test_native_counter_includes_instruction_and_tool_overhead(protocol):
    r = router()
    messages = [{"role": "user", "content": "ok"}]
    extras = {}
    response_input = None
    if protocol == "responses":
        messages = None
        response_input = "ok"
        extras["instructions"] = " policy" * 2000
    elif protocol == "messages":
        extras["system"] = [{"type": "text", "text": " policy" * 2000}]
    else:
        messages.insert(0, {"role": "system", "content": " policy" * 2000})
    count = r._count_pre_call_check_tokens(messages=messages, input=response_input, request_kwargs=extras)
    assert count > 2000
    tools = [{"type": "function", "function": {
        "name": "echo", "description": " schema" * 2000,
        "parameters": {"type": "object", "properties": {}},
    }}]
    extras["tools"] = tools if protocol != "messages" else [{
        "name": "echo", "description": " schema" * 2000,
        "input_schema": {"type": "object", "properties": {}},
    }]
    full_count = r._count_pre_call_check_tokens(messages=messages, input=response_input, request_kwargs=extras)
    assert full_count > count + 1000


def test_oversized_messages_fail_before_an_advisor_consultation(monkeypatch):
    from litellm.proxy import proxy_server
    from subroute.plugins import advisor_plugin

    r = router()
    # Keep this request small; use the native fixture limit to exercise ordering.
    r.model_list[0]["model_info"]["max_input_tokens"] = 1000
    monkeypatch.setattr(proxy_server, "llm_router", r)
    monkeypatch.setattr(r, "get_configured_token_limits", lambda model: (1000, None))

    async def unexpected_consultation(*args, **kwargs):
        pytest.fail("The oversized request reached the provider")

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", unexpected_consultation)
    data = {
        "model": "codex-luna", "messages": [{"role": "user", "content": "ok"}],
        "system": " policy" * 2000,
        "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
    }
    with pytest.raises(litellm.ContextWindowExceededError, match="advisor was not called"):
        asyncio.run(advisor_plugin.AdvisorPlugin().async_pre_call_hook(None, None, data, "aanthropic_messages"))
