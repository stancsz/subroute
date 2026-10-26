"""Retained Sift library adapter tests, outside normal gateway dispatch."""
from copy import deepcopy
import json
import pytest
from fastapi import HTTPException
from sift.litellm_proxy import compact_proxy_request

class ResponsesRouter:
    def _count_pre_call_check_tokens(self, *, messages, input, request_kwargs):
        return len(json.dumps({"input": input, "instructions": request_kwargs.get("instructions")}).split())


def test_responses_active_message_excerpts_are_counted_and_forwarded():
    data = {"model": "fixture", "input": [{"role": "user", "content": "evidence " * 50_000}]}
    before = deepcopy(data)
    result = compact_proxy_request(ResponsesRouter(), data, call_type="aresponses", target=4096)
    assert (result.get("litellm_metadata") or result["metadata"])["sift"]["forwarded_tokens"] <= 4096
    assert "lossy" in result["input"][0]["content"]
    assert len(result["input"][0]["content"]) < len(data["input"][0]["content"])
    assert data == before


def test_responses_opaque_reasoning_is_preserved_and_large_tool_payload_is_excerpted():
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
    data = {"model": "fixture", "input": [
        {"role": "user", "content": "Find the failure"}, reasoning,
        {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "log evidence " * 30_000},
    ]}
    before = deepcopy(data)
    result = compact_proxy_request(ResponsesRouter(), data, call_type="aresponses", target=4096)
    assert (result.get("litellm_metadata") or result["metadata"])["sift"]["forwarded_tokens"] <= 4096
    assert reasoning in result["input"]
    output = result["input"][-1]
    assert output["type"] == "function_call_output" and output["call_id"] == "call_1"
    assert "lossy" in output["output"]
    assert data == before


def test_unknown_namespace_tool_is_preserved_and_included_in_input_budget():
    data = {
        "model": "fixture",
        "input": "hello",
        "tools": [{
            "type": "namespace", "name": "outer", "tools": [{
                "type": "custom", "name": "unsupported",
                "description": "Report field " * 500,
            }],
        }],
    }
    before = deepcopy(data)

    router = ResponsesRouter()
    result = compact_proxy_request(router, data, call_type="aresponses", target=4096)
    audit = (result.get("litellm_metadata") or result["metadata"])["sift"]
    base_input = [{"role": "user", "content": data["input"]}]
    base_tokens = router._count_pre_call_check_tokens(
        messages=None, input=base_input, request_kwargs={**data, "input": base_input},
    )

    assert result["tools"] is data["tools"]
    assert audit["counting_responses_tools_opaque"] is True
    assert audit["counting_tool_floor_adjustment"] > 0
    assert audit["input_tokens"] == base_tokens + audit["counting_tool_floor_adjustment"]
    assert data == before


def test_oversized_opaque_namespace_tool_fails_input_budget_without_mutation():
    data = {
        "model": "fixture",
        "input": "hello",
        "tools": [{
            "type": "namespace", "name": "outer", "tools": [{
                "type": "custom", "name": "unsupported",
                "description": "Report field " * 5000,
            }],
        }],
    }
    before = deepcopy(data)

    with pytest.raises(HTTPException) as raised:
        compact_proxy_request(ResponsesRouter(), data, call_type="aresponses", target=1000)

    assert raised.value.status_code == 413
    assert data == before
