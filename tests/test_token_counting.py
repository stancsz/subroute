import asyncio
from copy import deepcopy
from importlib.metadata import version

import litellm
import pytest
from litellm import Router

from subroute.token_counting import install_tool_schema_counting_fix


@pytest.mark.parametrize("shape", ["chat", "anthropic", "responses"])
def test_unconstrained_nested_arrays_count_without_mutating_provider_tools(shape, caplog):
    install_tool_schema_counting_fix()
    schema = {"type": "object", "properties": {
        "values": {"type": "array"},
        "nested": {"type": "array", "items": {"type": "object", "properties": {
            "other": {"type": "array"},
        }}},
    }}
    function = {"name": "inspect_values", "parameters": schema}
    tool = ({"name": function["name"], "input_schema": schema} if shape == "anthropic" else
            {"type": "function", **function} if shape == "responses" else
            {"type": "function", "function": function})
    tools = [tool]
    before = deepcopy(tools)
    messages = [{"role": "user", "content": "Inspect the values"}]
    kwargs = {"tools": tools}
    if shape == "anthropic":
        kwargs["system"] = "Keep every value"
    if shape == "responses":
        kwargs["instructions"] = "Keep every value"
    router = Router(model_list=[{"model_name": "fixture", "litellm_params": {
        "model": "openai/gpt-4o-mini", "api_key": "fixture-only"},
        "model_info": {"max_input_tokens": 20}}], enable_pre_call_checks=True)
    # 1.103.0 performs its router check off-loop; 1.101.0 has the same
    # formatter bug but no async pre-count helper.
    if version("litellm") == "1.103.0":
        count = asyncio.run(router._acount_pre_call_check_tokens(
            "fixture", router.model_list, None if shape == "responses" else messages,
            "Inspect the values" if shape == "responses" else None, kwargs,
        ))
    else:
        count = litellm.token_counter(messages=messages, tools=tools)
    assert isinstance(count, int) and count > 20
    if version("litellm") == "1.103.0":
        with pytest.raises(litellm.ContextWindowExceededError):
            asyncio.run(router.async_get_available_deployment(
                model="fixture", request_kwargs=kwargs,
                messages=None if shape == "responses" else messages,
                input="Inspect the values" if shape == "responses" else None,
            ))
    assert "failed to count tokens" not in caplog.text
    assert tools == before


def test_constrained_schema_count_is_preserved_and_installation_is_idempotent():
    tools = [{"type": "function", "function": {"name": "read", "parameters": {
        "type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
    }}}]
    messages = [{"role": "user", "content": "Read files"}]
    expected = litellm.token_counter(messages=messages, tools=tools)
    install_tool_schema_counting_fix()
    install_tool_schema_counting_fix()
    assert litellm.token_counter(messages=messages, tools=tools) == expected
