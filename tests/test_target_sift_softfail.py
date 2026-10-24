"""Retained Sift library adapter tests, outside normal gateway dispatch."""
from copy import deepcopy
import json
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
