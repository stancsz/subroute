"""Target Sift is an optimization; provider limits remain the admission boundary."""

import asyncio
from copy import deepcopy
import json

from fastapi import HTTPException
import pytest

from subroute.sift_plugin import TargetContextRecovery
from sift.litellm_proxy import compact_proxy_request


class Router:
    def _count_pre_call_check_tokens(self, *, messages, input, request_kwargs):
        return 100


class ResponsesRouter:
    def _count_pre_call_check_tokens(self, *, messages, input, request_kwargs):
        return len(json.dumps({"input": input, "instructions": request_kwargs.get("instructions")}).split())


def request(target=None):
    metadata = {"trace_id": "kept"}
    if target is not None:
        metadata["sift_target_tokens"] = target
    return {"model": "openrouter", "metadata": metadata,
            "messages": [{"role": "user", "content": "Continue the task."}]}


def run(monkeypatch, hook, data):
    from litellm.proxy import proxy_server

    monkeypatch.setattr(proxy_server, "llm_router", Router())
    return asyncio.run(hook.async_pre_call_hook(None, None, data, "acompletion"))


def test_selected_target_accepts_compaction_up_to_two_times_target(monkeypatch):
    hook = TargetContextRecovery()

    async def compact(router, data, *, call_type, target, max_target):
        assert target == 32_768
        assert max_target == 65_536
        return {**data, "litellm_metadata": {"sift": {
            "compacted": True, "input_tokens": 100_000,
            "forwarded_tokens": 65_536,
        }}}

    hook._compact = compact
    result = run(monkeypatch, hook, request(32_768))
    assert result["litellm_metadata"]["sift"]["forwarded_tokens"] == 65_536


def test_result_over_two_times_target_soft_fails_to_original_request(monkeypatch):
    hook = TargetContextRecovery()
    original = request(32_768)

    async def compact(router, data, *, call_type, target, max_target):
        return {**data, "litellm_metadata": {"sift": {
            "compacted": True, "input_tokens": 100_000,
            "forwarded_tokens": 65_537,
        }}}

    hook._compact = compact
    result = run(monkeypatch, hook, original)
    assert result["messages"] == original["messages"]
    assert result["metadata"]["trace_id"] == "kept"
    audit = result["metadata"]["sift"]
    assert audit["soft_failed"] is True
    assert audit["reason"] == "sift_result_over_two_x_target"
    assert audit["max_forwarded_tokens"] == 65_536


@pytest.mark.parametrize("status,reason,soft", [
    (422, "unsupported_content", True),
    (413, "raw_limit", False),
])
def test_compaction_failure_passes_through_except_bounded_intake_rejection(
    monkeypatch, status, reason, soft,
):
    hook = TargetContextRecovery()

    async def compact(router, data, *, call_type, target, max_target):
        raise HTTPException(status, detail={"error": {
            "code": "sift_compaction_failed", "reason": reason,
            "input_tokens": 200_000,
        }})

    hook._compact = compact
    original = request()
    if not soft:
        with pytest.raises(HTTPException) as error:
            run(monkeypatch, hook, original)
        assert error.value.status_code == 413
        return

    result = run(monkeypatch, hook, original)
    assert result["messages"] == original["messages"]
    assert result["metadata"]["sift"]["soft_failed"] is True
    assert result["metadata"]["sift"]["input_tokens"] == 200_000


def test_large_chat_request_compacts_with_reminder_in_instruction_prefix(monkeypatch):
    from litellm.proxy import proxy_server

    class WordRouter:
        def _count_pre_call_check_tokens(self, *, messages, input, request_kwargs):
            return len(json.dumps({"messages": messages, "system": request_kwargs.get("system")}).split())

    monkeypatch.setattr(proxy_server, "llm_router", WordRouter())
    data = {"model": "openrouter", "system": "required policy " * 40_000,
            "metadata": {"sift_target_tokens": 65_536},
            "messages": [{"role": "user", "content": "Earlier task"},
                         {"role": "assistant", "content": "history " * 150_000},
                         {"role": "user", "content": "Continue the task"}]}
    before = deepcopy(data)
    result = asyncio.run(TargetContextRecovery().async_pre_call_hook(None, None, data, "acompletion"))
    audit = result["metadata"]["sift"]
    assert audit.get("soft_failed") is not True
    assert audit["compacted"] is True
    assert 65_536 < audit["forwarded_tokens"] <= 131_072
    assert audit["effective_target_tokens"] == 131_072
    assert result["system"] == data["system"]
    assert result["messages"][0]["role"] == "developer"
    assert result["messages"][-1] == data["messages"][-1]
    assert data == before


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
