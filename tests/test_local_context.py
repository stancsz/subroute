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


def test_chat_completions_system_order_is_not_rewritten():
    messages = [{"role": "user", "content": "Question"}, {"role": "system", "content": "Later instruction"}]
    data = {"model": "qwen3.8-27b", "messages": messages}
    assert frontload_anthropic_system_messages(data, "acompletion")["messages"] is messages


def test_native_sift_hook_owns_only_local_model_and_follows_translation():
    assert isinstance(sift_qwen_hook, CustomLogger)
    assert sift_qwen_hook.model_targets == {"qwen3.8-27b": 65_536}
    system = {"role": "system", "content": "Follow the original requirement"}
    old = {"role": "user", "content": "Old log\n" + "routine output\n" * 2000}
    current = {"role": "user", "content": "What happened?"}
    data = {"model": "qwen3.8-27b", "messages": [old, system, current]}
    frontload_anthropic_system_messages(data, "anthropic_messages")
    result = compact_proxy_request(CountingRouter(), data, call_type="anthropic_messages", target=600)
    assert result["messages"][0] == system
    assert result["messages"][-1] == current
    assert result["metadata"]["sift"]["forwarded_tokens"] <= 600


def test_qwen_sift_target_can_be_selected_per_request_below_64k_ceiling():
    for target in (65_536, 49_152, 32_768, 24_576, 16_384):
        request = {"metadata": {"sift_target_tokens": target, "trace_id": "keep-me"}}

        assert sift_qwen_hook.request_target(request, 65_536) == target
        assert request["metadata"] == {"trace_id": "keep-me"}


def test_qwen_sift_target_rejects_non_integer_or_over_ceiling_values():
    from fastapi import HTTPException
    import pytest

    for value in (True, 0, 16_384.0, 65_537):
        request = {"metadata": {"sift_target_tokens": value}}
        with pytest.raises(HTTPException) as error:
            sift_qwen_hook.request_target(request, 65_536)
        assert error.value.status_code == 400
