"""Local Qwen translation and native Sift callback ownership."""

import json

from litellm.integrations.custom_logger import CustomLogger

from sift.litellm_proxy import compact_proxy_request
from subroute.local_context import frontload_anthropic_system_messages
from subroute.sift_plugin import sift_qwen_hook


class CountingRouter:
    def _count_pre_call_check_tokens(self, *, messages, input, request_kwargs):
        payload = json.dumps({"messages": messages, "tools": request_kwargs.get("tools")}, ensure_ascii=False)
        return len(payload.split())


def test_claude_system_entry_is_frontloaded_without_changing_roles_or_content():
    user = {"role": "user", "content": [{"type": "text", "text": "Fix the file"}]}
    system = {"role": "system", "content": [{"type": "text", "text": "Use the tools"}]}
    data = {"model": "qwen3.8-27b", "messages": [user, system], "system": "Base instruction"}
    result = frontload_anthropic_system_messages(data, "anthropic_messages")
    assert result["messages"] == [system, user]
    assert result["messages"][0] == system
    assert result["system"] == "Base instruction"
    assert result["metadata"]["subroute_local_system_translation"] == "initial_system_prefix"


def test_developer_instruction_frontloads_without_losing_existing_metadata():
    # Derived from the useful boundary found by the local Claude worker.
    user = {"role": "user", "content": "Question"}
    developer = {"role": "developer", "content": "Use the tools"}
    data = {"model": "qwen3.8-27b", "messages": [user, developer], "metadata": {"trace": "keep"}}

    result = frontload_anthropic_system_messages(data, "aanthropic_messages")

    assert result["messages"] == [developer, user]
    assert result["metadata"] == {"trace": "keep", "subroute_local_system_translation": "initial_system_prefix"}


def test_chat_completions_system_order_is_not_rewritten():
    messages = [{"role": "user", "content": "Question"}, {"role": "system", "content": "Later instruction"}]
    data = {"model": "qwen3.8-27b", "messages": messages}
    assert frontload_anthropic_system_messages(data, "acompletion")["messages"] is messages


def test_native_sift_hook_owns_only_local_model_and_follows_translation():
    assert isinstance(sift_qwen_hook, CustomLogger)
    assert sift_qwen_hook.model_targets == {"qwen3.8-27b": 32_768}
    system = {"role": "system", "content": "Follow the original requirement"}
    old = {"role": "user", "content": "Old log\n" + "routine output\n" * 2000}
    current = {"role": "user", "content": "What happened?"}
    data = {"model": "qwen3.8-27b", "messages": [old, system, current]}
    frontload_anthropic_system_messages(data, "anthropic_messages")
    result = compact_proxy_request(CountingRouter(), data, call_type="anthropic_messages", target=600)
    assert result["messages"][0] == system
    assert result["messages"][-1] == current
    assert result["metadata"]["sift"]["forwarded_tokens"] <= 600


def test_qwen_sift_target_can_be_selected_per_request_below_32k_ceiling():
    for target in (32_768, 24_576, 16_384):
        request = {"metadata": {"sift_target_tokens": target, "trace_id": "keep-me"}}

        assert sift_qwen_hook.request_target(request, 32_768) == target
        assert request["metadata"] == {"trace_id": "keep-me"}


def test_qwen_sift_target_rejects_non_integer_or_over_ceiling_values():
    from fastapi import HTTPException
    import pytest

    for value in (True, 0, 16_384.0, 32_769, 65_536):
        request = {"metadata": {"sift_target_tokens": value}}
        with pytest.raises(HTTPException) as error:
            sift_qwen_hook.request_target(request, 32_768)
        assert error.value.status_code == 400


def test_four_qwen_compactions_remain_bounded_when_a_caller_is_cancelled(monkeypatch):
    import asyncio
    import threading

    from fastapi import HTTPException
    import pytest
    import sift.litellm_proxy
    from subroute.sift_plugin import QwenContextRecovery

    entered = [threading.Event() for _ in range(4)]
    release = threading.Event()

    def compact(router, data, **kwargs):
        entered[data["worker"]].set()
        assert release.wait(5), "test did not release the compaction workers"
        return data

    monkeypatch.setattr(sift.litellm_proxy, "compact_proxy_request", compact)

    async def exercise():
        hook = QwenContextRecovery()
        tasks = [asyncio.create_task(hook._compact(
            None, {"worker": index}, call_type="acompletion", target=32768,
        )) for index in range(4)]
        try:
            for event in entered:
                assert await asyncio.to_thread(event.wait, 3)
            with pytest.raises(HTTPException) as fifth:
                await hook._compact(None, {"worker": 0}, call_type="acompletion", target=32768)
            assert fifth.value.status_code == 429
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            # Cancellation must not release a permit held by an executing thread.
            with pytest.raises(HTTPException) as replacement:
                await hook._compact(None, {"worker": 0}, call_type="acompletion", target=32768)
            assert replacement.value.status_code == 429
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())


def test_qwen_output_budget_is_rejected_before_compaction_and_not_clamped(monkeypatch):
    import asyncio

    from fastapi import HTTPException
    from litellm.proxy import proxy_server
    import pytest
    from subroute.sift_plugin import QwenContextRecovery

    class Router:
        def get_configured_token_limits(self, model):
            assert model == "qwen3.8-27b"
            return 32768, 4096

    monkeypatch.setattr(proxy_server, "llm_router", Router())
    hook = QwenContextRecovery()
    compacted = []

    async def compact(router, data, **kwargs):
        compacted.append(data.copy())
        return {**data, "metadata": {"sift": {"forwarded_tokens": 1}}}

    monkeypatch.setattr(hook, "_compact", compact)
    for protocol, field in (("acompletion", "max_tokens"),
                            ("acompletion", "max_completion_tokens"),
                            ("aanthropic_messages", "max_tokens"),
                            ("aresponses", "max_output_tokens")):
        boundary = {"model": "qwen3.8-27b", field: 4096, "tool_choice": "required"}
        result = asyncio.run(hook.async_pre_call_hook(None, None, boundary, protocol))
        assert result[field] == boundary[field]
        assert result["tool_choice"] == "required"
        assert boundary[field] == 4096
        before = len(compacted)
        for value in (4097, 8192, 0, -1, True, 4096.0):
            request = {"model": "qwen3.8-27b", field: value}
            with pytest.raises(HTTPException) as error:
                asyncio.run(hook.async_pre_call_hook(None, None, request, protocol))
            assert error.value.status_code == 400
            assert request[field] == value
        assert len(compacted) == before
    omitted = {"model": "qwen3.8-27b"}
    result = asyncio.run(hook.async_pre_call_hook(None, None, omitted, "acompletion"))
    assert result["model"] == omitted["model"]
    assert omitted == {"model": "qwen3.8-27b"}


def test_qwen_named_tool_choice_is_rejected_instead_of_silently_using_auto():
    import asyncio

    from fastapi import HTTPException
    import pytest
    from subroute.sift_plugin import QwenContextRecovery

    hook = QwenContextRecovery()
    for protocol, choice in (
        ("acompletion", {"type": "function", "function": {"name": "add_numbers"}}),
        ("aresponses", {"type": "function", "name": "add_numbers"}),
        ("aanthropic_messages", {"type": "tool", "name": "add_numbers"}),
    ):
        request = {"model": "qwen3.8-27b", "tool_choice": choice}
        with pytest.raises(HTTPException) as error:
            asyncio.run(hook.async_pre_call_hook(None, None, request, protocol))
        assert error.value.status_code == 400
        assert request["tool_choice"] == choice
        # Other routes retain their native protocol behavior.
        request["model"] = "other-model"
        assert asyncio.run(hook.async_pre_call_hook(None, None, request, protocol)) is None
