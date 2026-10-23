"""Automatic recovery preserves active work and never calls a provider itself."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from subroute.sift_plugin import AutoContextRecovery


class Router:
    info = {"max_input_tokens": 256_000, "context_window": 256_000,
            "auto_compact_token_limit": 204_800}

    def get_deployment_by_model_group_name(self, *, model_group_name):
        assert model_group_name == "auto"
        return SimpleNamespace(model_info=self.info)

    def _count_pre_call_check_tokens(self, *, messages, input, request_kwargs):
        return len(json.dumps({"messages": messages, "system": request_kwargs.get("system"),
                               "tools": request_kwargs.get("tools")}).split())


def run(monkeypatch, data, call_type="acompletion", router=None):
    from litellm.proxy import proxy_server

    monkeypatch.setattr(proxy_server, "llm_router", router or Router())
    return asyncio.run(AutoContextRecovery().async_pre_call_hook(None, None, data, call_type))


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_oversized_tool_history_recovers_without_changing_instructions_or_latest_cycle(monkeypatch, protocol):
    messages = [{"role": "user", "content": "Fix test_original_failure and report the newest failing test."}]
    for index in range(8):
        tool_id = f"read_{index}"
        text = "routine output " * 18_000 + f"\nFAILED: test_original_failure_{index}"
        if protocol == "openai":
            messages.extend([
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": tool_id, "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": tool_id, "content": text},
            ])
        else:
            messages.extend([
                {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id,
                                                     "name": "Read", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                "content": text}]},
            ])
    data = {"model": "auto", "max_tokens": 65_536, "system": "Do not change accounting rules.",
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}], "messages": messages}
    before = deepcopy(data)
    result = run(monkeypatch, data, "acompletion" if protocol == "openai" else "anthropic_messages")
    audit = result["metadata"]["sift"]
    assert audit["compacted"] is True
    assert audit["input_tokens"] > 256_000
    assert audit["forwarded_tokens"] + data["max_tokens"] + 8192 <= 256_000
    assert result["messages"][0] == messages[0]
    assert result["messages"][-2:] == messages[-2:]
    assert "test_original_failure_0" in json.dumps(result["messages"])
    assert result["system"] == data["system"]
    assert result["tools"] == data["tools"]
    assert result["max_tokens"] == data["max_tokens"]
    assert data == before


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_oversized_current_tool_result_is_excerpted_with_protocol_structure_preserved(monkeypatch, protocol):
    tool_id = "read_current"
    small_tool_id = "read_small"
    task = {"role": "user", "content": "Find test_context_pruning_recovery and explain the failure."}
    call = (
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": tool_id, "type": "function", "function": {"name": "Read", "arguments": "{}"}},
            {"id": small_tool_id, "type": "function", "function": {"name": "Read", "arguments": "{}"}},
        ]}
        if protocol == "openai" else
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": "Read", "input": {}},
            {"type": "tool_use", "id": small_tool_id, "name": "Read", "input": {}},
        ]}
    )
    text = "routine output " * 105_000 + "\nFAILED: test_context_pruning_recovery"
    result_message = (
        {"role": "tool", "tool_call_id": tool_id, "content": text}
        if protocol == "openai" else
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                        "content": text},
                                       {"type": "tool_result", "tool_use_id": small_tool_id,
                                        "content": "small result stays exact"}]}
    )
    messages = [task, call, result_message]
    if protocol == "openai":
        messages.append({"role": "tool", "tool_call_id": small_tool_id,
                         "content": "small result stays exact"})
    data = {"model": "auto", "max_tokens": 512, "system": "Keep the current task intact.",
            "messages": messages}
    before = deepcopy(data)

    result = run(monkeypatch, data, "acompletion" if protocol == "openai" else "anthropic_messages")

    audit = result["metadata"]["sift"]
    assert audit["compacted"] is True
    assert audit["active_tool_results_compacted"] == 1
    assert audit["forwarded_tokens"] <= AutoContextRecovery.input_target(Router(), data)
    assert result["messages"][0] == task
    assert result["messages"][1] == call
    if protocol == "openai":
        excerpt = result["messages"][2]["content"]
        assert result["messages"][2]["tool_call_id"] == tool_id
        assert result["messages"][3] == messages[3]
    else:
        block = result["messages"][2]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == tool_id
        excerpt = block["content"]
        assert result["messages"][2]["content"][1] == messages[2]["content"][1]
    assert "incomplete, lossy excerpt" in excerpt
    assert "FAILED: test_context_pruning_recovery" in excerpt
    assert len(excerpt) < len(text)
    assert result["system"] == data["system"]
    assert result["max_tokens"] == data["max_tokens"]
    assert data == before


def test_small_request_retains_exact_message_objects_and_output_limit(monkeypatch):
    data = {"model": "auto", "max_tokens": 512, "messages": [{"role": "user", "content": "Hello"}]}
    result = run(monkeypatch, data)
    assert result["messages"] is data["messages"]
    assert result["metadata"]["sift"]["compacted"] is False
    assert result["max_tokens"] == 512


@pytest.mark.parametrize("changes", [
    {"model": "codex-luna"}, {"model": "qwen3.8-27b"}, {"model": "openrouter"},
])
def test_explicit_routes_are_not_compacted(monkeypatch, changes):
    assert run(monkeypatch, {"model": "auto", **changes}) is None


def test_responses_are_not_silently_translated_or_compacted(monkeypatch):
    assert run(monkeypatch, {"model": "auto", "input": "text"}, "aresponses") is None


@pytest.mark.parametrize("output", [True, "512", -1, 0, 300_000])
def test_invalid_or_impossible_output_limits_fail_before_compaction(monkeypatch, output):
    with pytest.raises(HTTPException) as error:
        run(monkeypatch, {"model": "auto", "max_tokens": output, "messages": []})
    assert error.value.status_code == 400


def test_missing_output_cap_reserves_provider_headroom():
    assert AutoContextRecovery.input_target(Router(), {}) == 256_000 - 65_536 - 8192
    assert AutoContextRecovery.input_target(Router(), {"max_completion_tokens": 100_000}) == 147_808


def test_budget_uses_real_litellm_deployment_metadata_instead_of_model_id_lookup():
    from litellm import Router as LiteLLMRouter

    router = LiteLLMRouter(model_list=[{
        "model_name": "auto", "model_info": Router.info,
        "litellm_params": {"model": "openai/fixture", "api_key": "test"},
    }])
    assert AutoContextRecovery.input_target(router, {"max_tokens": 512}) == 196_608


@pytest.mark.parametrize("content,code", [
    ("unshrinkable " * 210_000, "outbound_context_limit_exceeded"),
    ([{"type": "image_url", "image_url": {"url": "data:image/png;base64,opaque"}},
      {"type": "text", "text": "large " * 210_000}], "unsupported_context_content"),
], ids=["active-turn", "opaque-media"])
def test_unsafe_content_fails_with_actionable_contract(monkeypatch, caplog, content, code):
    data = {"model": "auto", "max_tokens": 512,
            "messages": [{"role": "user", "content": content}]}
    before = deepcopy(data)
    with pytest.raises(HTTPException) as error:
        run(monkeypatch, data)
    assert error.value.status_code == 400
    assert error.value.detail["error"]["code"] == code
    expected_reason = "active_too_large" if code == "outbound_context_limit_exceeded" else "unsupported_content"
    assert f"reason={expected_reason}" in caplog.text
    assert "input_tokens=" in caplog.text and "target=" in caplog.text
    assert data == before


def test_intake_above_one_million_is_rejected_before_history_scanning(monkeypatch):
    class HugeRouter(Router):
        def _count_pre_call_check_tokens(self, **kwargs):
            return 1_000_001

    with pytest.raises(HTTPException) as error:
        run(monkeypatch, {"model": "auto", "max_tokens": 512, "messages": []}, router=HugeRouter())
    assert error.value.status_code == 400
    assert error.value.detail["error"]["code"] == "outbound_context_limit_exceeded"


@pytest.mark.parametrize("protected", ["system", "tools"])
def test_required_instructions_and_tools_count_against_the_same_budget(monkeypatch, protected):
    data = {"model": "auto", "max_tokens": 512, "messages": [
        {"role": "user", "content": "Earlier task"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "Current task"},
    ]}
    text = "required instruction " * 110_000
    data[protected] = text if protected == "system" else [{"name": "Read", "description": text}]
    before = deepcopy(data)
    with pytest.raises(HTTPException) as error:
        run(monkeypatch, data)
    assert error.value.status_code == 400
    assert error.value.detail["error"]["code"] == "outbound_context_limit_exceeded"
    assert data == before


def test_recovery_exposes_audit_and_rejects_concurrent_overload(monkeypatch):
    from litellm.proxy import proxy_server

    hook = AutoContextRecovery()
    monkeypatch.setattr(proxy_server, "llm_router", Router())
    assert hook._slots.acquire(blocking=False)
    assert hook._slots.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(hook.async_pre_call_hook(None, None,
                        {"model": "auto", "max_tokens": 512, "messages": []}, "acompletion"))
        assert error.value.status_code == 429
        assert error.value.headers["Retry-After"] == "1"
    finally:
        hook._slots.release()
        hook._slots.release()
    headers = asyncio.run(hook.async_post_call_response_headers_hook(
        {"model": "auto", "metadata": {"sift": {"compacted": True, "input_tokens": 657490,
                                                 "forwarded_tokens": 12000}}}, None, None))
    assert headers["x-sift-compacted"] == "true"
    assert headers["x-sift-estimated-forwarded-tokens"] == "12000"
