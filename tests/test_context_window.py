"""Exercise the installed LiteLLM checks, without making provider calls."""

import asyncio
import json
from pathlib import Path

import litellm
import pytest
import yaml


def router():
    root = Path(__file__).parents[1]
    settings = yaml.safe_load((root / "config/litellm.yaml").read_text())
    active_model = json.loads((root / "config/active_model.json").read_text())["active_model"]
    deployment = next(item for item in settings["model_list"] if item["model_name"] == active_model)
    return litellm.Router(model_list=[{
        "model_name": "context-fixture",
        "model_info": deployment["model_info"],
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
