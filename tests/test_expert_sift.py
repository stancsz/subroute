import asyncio

import pytest

from subroute.plugins.expert_sift import (
    EXPERT_INPUT_LIMIT,
    EXPERT_MODELS,
    CODEX_EXPERT_OUTPUT_LIMIT,
    EXPERT_SIFT_TARGET,
    ExpertSiftHook,
)


def test_expert_sift_keeps_compacted_input_under_configured_budget():
    hook = ExpertSiftHook()
    assert EXPERT_SIFT_TARGET == 25_600
    assert hook.sift.max_target == EXPERT_INPUT_LIMIT == 32_000
    assert set(hook.sift.model_targets) == set(EXPERT_MODELS)


def test_expert_calls_are_short_by_default_and_respect_lower_client_limits():
    hook = ExpertSiftHook()
    chat = {"model": "codex-luna-advisor", "messages": []}
    hook._bound_output(chat, "acompletion")
    assert chat["max_completion_tokens"] == CODEX_EXPERT_OUTPUT_LIMIT

    anthropic = {"max_tokens": 64}
    hook._bound_output(anthropic, "aanthropic_messages")
    assert anthropic["max_tokens"] == 64

    responses = {"max_output_tokens": 5000}
    hook._bound_output(responses, "aresponses")
    assert responses["max_output_tokens"] == CODEX_EXPERT_OUTPUT_LIMIT


def test_exact_expert_input_limit_is_independent_of_codex_output_limit(monkeypatch):
    from litellm.proxy import proxy_server

    class CountingRouter:
        @staticmethod
        def _count_pre_call_check_tokens(*, messages, input, request_kwargs):
            return sum(len(str(message.get("content", ""))) for message in messages) // 4

    monkeypatch.setattr(proxy_server, "llm_router", CountingRouter())
    hook = ExpertSiftHook()
    content = "x" * (EXPERT_INPUT_LIMIT * 4)
    request = {
        "model": "codex-luna-advisor",
        "messages": [{"role": "user", "content": content}],
    }

    result = asyncio.run(hook.async_pre_call_hook({}, None, request, "aanthropic_messages"))

    assert result is not None
    assert request["messages"][0]["content"] == content
    assert result["max_tokens"] == CODEX_EXPERT_OUTPUT_LIMIT
    assert result["metadata"]["sift"]["input_tokens"] == EXPERT_INPUT_LIMIT
    assert result["metadata"]["sift"]["forwarded_tokens"] <= EXPERT_INPUT_LIMIT


def test_over_limit_expert_input_is_compacted_without_mutating_target(monkeypatch):
    from litellm.proxy import proxy_server

    class CountingRouter:
        @staticmethod
        def _count_pre_call_check_tokens(*, messages, input, request_kwargs):
            return sum(len(str(message.get("content", ""))) for message in messages) // 4

    monkeypatch.setattr(proxy_server, "llm_router", CountingRouter())
    hook = ExpertSiftHook()
    content = "x" * ((EXPERT_INPUT_LIMIT + 1_024) * 4)
    request = {
        "model": "gemini-4-argon-advisor",
        "messages": [{"role": "user", "content": content}],
    }

    result = asyncio.run(hook.async_pre_call_hook({}, None, request, "acompletion"))

    assert result is not None
    assert request["messages"][0]["content"] == content
    assert result["metadata"]["sift"]["input_tokens"] == EXPERT_INPUT_LIMIT + 1_024
    assert result["metadata"]["sift"]["compacted"] is True
    assert result["metadata"]["sift"]["forwarded_tokens"] <= EXPERT_INPUT_LIMIT


def test_expert_hook_compacts_only_allowlisted_4040_models(monkeypatch):
    hook = ExpertSiftHook()
    seen = []

    async def compact(*args, **kwargs):
        seen.append(kwargs)
        return args[2]

    monkeypatch.setattr(hook.sift, "async_pre_call_hook", compact)
    request = {"model": "codex-luna-advisor", "messages": [{"role": "user", "content": "Review."}]}
    assert asyncio.run(hook.async_pre_call_hook({}, None, request, "aanthropic_messages")) is request
    assert len(seen) == 1

    assert request["max_tokens"] == CODEX_EXPERT_OUTPUT_LIMIT
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "low"}

    explicit = {"model": "codex-luna-advisor", "messages": [], "output_config": {"effort": "high"}}
    asyncio.run(hook.async_pre_call_hook({}, None, explicit, "aanthropic_messages"))
    assert explicit["output_config"] == {"effort": "high"}

    gemini = {"model": "gemini-4-argon-advisor", "messages": []}
    asyncio.run(hook.async_pre_call_hook({}, None, gemini, "acompletion"))
    assert "max_tokens" not in gemini

    ordinary = {"model": "codex-luna", "messages": []}
    assert asyncio.run(hook.async_pre_call_hook({}, None, ordinary, "acompletion")) is None
    assert len(seen) == 3


def test_experts_hook_compacts_a_private_copy_before_provider_dispatch(monkeypatch):
    from litellm.proxy import proxy_server

    class CountingRouter:
        @staticmethod
        def _count_pre_call_check_tokens(*, messages, input, request_kwargs):
            return sum(len(str(message.get("content", ""))) for message in messages) // 4

    monkeypatch.setattr(proxy_server, "llm_router", CountingRouter())
    hook = ExpertSiftHook()
    hook.sift.model_targets["codex-luna-advisor"] = 256
    hook.sift.max_target = 512
    request = {
        "model": "codex-luna-advisor",
        "messages": [
            {"role": "user", "content": f"old context {index} " + "x" * 300}
            for index in range(8)
        ] + [{"role": "user", "content": "Keep this latest question."}],
    }
    original_messages = request["messages"]

    result = asyncio.run(hook.async_pre_call_hook({}, None, request, "aanthropic_messages"))

    assert result is not request
    assert request["messages"] is original_messages
    assert len(result["messages"]) < len(original_messages)
    assert result["messages"][-1]["content"] == "Keep this latest question."
    assert result["metadata"]["sift"]["compacted"] is True
    assert result["metadata"]["sift"]["forwarded_tokens"] <= 512
    audit = result["metadata"]["sift"]
    assert asyncio.run(hook.async_post_call_response_headers_hook(result, {}, object())) == {
        "x-sift-compacted": "true",
        "x-sift-estimated-input-tokens": str(audit["input_tokens"]),
        "x-sift-estimated-forwarded-tokens": str(audit["forwarded_tokens"]),
    }


@pytest.mark.parametrize("carrier", ["metadata", "litellm_metadata"])
def test_multimodal_bypass_does_not_echo_caller_sift_audit(carrier):
    hook = ExpertSiftHook()
    forged = {"compacted": True, "input_tokens": 40_000, "forwarded_tokens": 100}
    request = {
        "model": "gemini-3.8-flash-advisor",
        "messages": [{"role": "user", "content": [{
            "type": "image_url", "image_url": {"url": "fixture-only"},
        }]}],
        carrier: {"sift": forged, "keep": "caller metadata"},
    }
    result = asyncio.run(hook.async_pre_call_hook({}, None, request, "acompletion"))
    assert result is not request
    assert result["messages"] is request["messages"]
    assert result[carrier] == {"keep": "caller metadata"}
    assert request[carrier]["sift"] is forged
    assert asyncio.run(hook.async_post_call_response_headers_hook(result, {}, object())) is None

    ordinary = {**request, "model": "codex-luna"}
    assert asyncio.run(hook.async_pre_call_hook({}, None, ordinary, "acompletion")) is None
    assert ordinary[carrier]["sift"] is forged
    assert asyncio.run(hook.async_post_call_response_headers_hook(ordinary, {}, object())) is None


def test_response_header_hook_forwards_native_sift_audit_only(monkeypatch):
    hook = ExpertSiftHook()
    data = {"model": "codex-luna-advisor", "metadata": {"sift": {
        "compacted": True, "input_tokens": 36_000, "forwarded_tokens": 25_000,
    }}}
    key, response, headers, info = object(), object(), {"x-test": "1"}, {"call": 1}
    native_hook = hook.sift.async_post_call_response_headers_hook
    seen = []

    async def native(*args):
        seen.append(args)
        return await native_hook(*args)

    monkeypatch.setattr(hook.sift, "async_post_call_response_headers_hook", native)
    assert asyncio.run(hook.async_post_call_response_headers_hook(data, key, response, headers, info)) == {
        "x-sift-compacted": "true",
        "x-sift-estimated-input-tokens": "36000",
        "x-sift-estimated-forwarded-tokens": "25000",
    }
    assert seen == [(data, key, response, headers, info)]

    ordinary = {"model": "codex-luna", "metadata": {"sift": data["metadata"]["sift"]}}
    assert asyncio.run(hook.async_post_call_response_headers_hook(ordinary, key, response)) is None
    assert asyncio.run(hook.async_post_call_response_headers_hook({"model": "codex-luna-advisor"}, key, response)) is None
    assert asyncio.run(hook.async_post_call_response_headers_hook(
        {"model": "codex-luna-advisor", "metadata": {}}, key, response,
    )) is None


def test_multimodal_expert_request_is_preserved_for_native_admission(monkeypatch):
    hook = ExpertSiftHook()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Sift must not rewrite multimodal expert content")

    monkeypatch.setattr(hook.sift, "async_pre_call_hook", forbidden)
    request = {
        "model": "gemini-4-argon-advisor",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Review this."},
            {"type": "image", "source": {"type": "base64", "data": "opaque"}},
        ]}],
    }
    original_messages = request["messages"]
    assert asyncio.run(hook.async_pre_call_hook({}, None, request, "aanthropic_messages")) is request
    assert request["messages"] is original_messages
    assert "max_tokens" not in request
