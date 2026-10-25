import asyncio
from copy import deepcopy
import logging
import uuid

import pytest

from sift.litellm_proxy import SiftLiteLLMHook

from subroute.plugins.expert_sift import (
    EXPERT_INPUT_LIMIT,
    EXPERT_MODELS,
    CODEX_EXPERT_OUTPUT_LIMIT,
    EXPERT_SIFT_TARGET,
    ExpertSiftHook,
)


def test_interleaved_sift_requests_log_their_own_ids_and_strip_provider_headers(monkeypatch, caplog):
    from litellm.proxy import proxy_server
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
    from litellm.proxy.auth.user_api_key_auth import LITELLM_PROXY_MASTER_KEY_ALIAS

    class CountingRouter:
        @staticmethod
        def _count_pre_call_check_tokens(*, messages, input, request_kwargs):
            return sum(len(str(message.get("content", ""))) for message in messages) // 4

    monkeypatch.setattr(proxy_server, "llm_router", CountingRouter())
    hook = ExpertSiftHook()
    hook.model_targets["gemini-3.8-flash-advisor"] = 512
    caplog.set_level(logging.INFO, logger="LiteLLM Proxy")
    auth = UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN,
                          api_key=LITELLM_PROXY_MASTER_KEY_ALIAS)
    ids = [(str(uuid.uuid4()), str(uuid.uuid4())) for _ in range(2)]
    requests = []
    for root_id, attempt_id in ids:
        requests.append({
            "model": "gemini-3.8-flash-advisor",
            "messages": [
                {"role": "user", "content": f"old context {index} " + "x" * 300}
                for index in range(8)
            ] + [{"role": "user", "content": "Keep this latest question."}],
            "proxy_server_request": {"headers": {
                "x-subroute-request-id": root_id,
                "x-subroute-attempt-id": attempt_id,
                "authorization": "Bearer fixture-only",
            }},
            "extra_headers": {
                "X-Subroute-Request-ID": "forged",
                "x-client-marker": "preserved",
            },
            "_subroute_sift_correlation": {"request_id": "forged", "advisor_attempt_id": "forged"},
        })
    originals = [deepcopy(request) for request in requests]

    async def scenario():
        return await asyncio.gather(*(
            hook.async_pre_call_hook(auth, None, request, "aanthropic_messages")
            for request in requests
        ))

    results = asyncio.run(scenario())
    assert [request["proxy_server_request"]["headers"] for request in requests] == [
        original["proxy_server_request"]["headers"] for original in originals
    ]
    assert [result["messages"][-1]["content"] for result in results] == [
        "Keep this latest question.", "Keep this latest question.",
    ]
    for result in results:
        assert result["extra_headers"] == {"x-client-marker": "preserved"}
        assert "_subroute_sift_correlation" not in result
        assert all(name.lower() not in {
            "x-subroute-request-id", "x-subroute-attempt-id",
        } for name in result["proxy_server_request"]["headers"])

    sift_lines = [record.getMessage() for record in caplog.records if "sift_request" in record.getMessage()]
    assert len(sift_lines) == 2, sift_lines
    assert all("forged" not in line for line in sift_lines)
    for root_id, attempt_id in ids:
        assert any(f"request_id={root_id}" in line and f"advisor_attempt_id={attempt_id}" in line for line in sift_lines)
    for line in sift_lines:
        matching = [pair for pair in ids if pair[0] in line or pair[1] in line]
        assert len(matching) == 1
        assert all(value in line for value in matching[0])


def test_untrusted_auth_cannot_claim_valid_correlation_ids(monkeypatch, caplog):
    from litellm.proxy._types import UserAPIKeyAuth
    from subroute.plugins.expert_sift import ExpertSiftHook

    async def unchanged(self, user_api_key_dict, cache, data, call_type):
        assert "_subroute_sift_correlation" not in data
        assert not any(name.lower().startswith("x-subroute-")
                       for name in data["proxy_server_request"]["headers"])
        return data

    monkeypatch.setattr(SiftLiteLLMHook, "async_pre_call_hook", unchanged)
    caplog.set_level(logging.INFO, logger="LiteLLM Proxy")
    request = {
        "model": "gemini-3.8-flash-advisor",
        "messages": [{"role": "user", "content": "Review this."}],
        "proxy_server_request": {"headers": {
            "x-subroute-request-id": str(uuid.uuid4()),
            "x-subroute-attempt-id": str(uuid.uuid4()),
        }},
    }

    result = asyncio.run(ExpertSiftHook().async_pre_call_hook(
        UserAPIKeyAuth(api_key="fixture-virtual-key"), None, request, "aanthropic_messages",
    ))

    assert result is not None
    assert "_subroute_sift_correlation" not in result
    assert not any("sift_request" in record.getMessage() and "request_id=" in record.getMessage()
                   for record in caplog.records)


@pytest.mark.parametrize("outcome", ["exception", "cancel"])
def test_correlation_marker_is_cleared_when_sift_does_not_complete(monkeypatch, outcome):
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
    from litellm.proxy.auth.user_api_key_auth import LITELLM_PROXY_MASTER_KEY_ALIAS

    async def interrupted(self, user_api_key_dict, cache, data, call_type):
        assert data["_subroute_sift_correlation"]["request_id"] == request_id
        if outcome == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(SiftLiteLLMHook, "async_pre_call_hook", interrupted)
    request_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    request = {
        "model": "gemini-3.8-flash-advisor",
        "messages": [{"role": "user", "content": "Review this."}],
        "proxy_server_request": {"headers": {
            "x-subroute-request-id": request_id,
            "x-subroute-attempt-id": attempt_id,
        }},
    }
    auth = UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN,
                          api_key=LITELLM_PROXY_MASTER_KEY_ALIAS)

    with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
        asyncio.run(ExpertSiftHook().async_pre_call_hook(auth, None, request, "aanthropic_messages"))

    assert "_subroute_sift_correlation" not in request
    assert "_subroute_sift_correlation" not in request["proxy_server_request"]


@pytest.mark.parametrize("proxy_headers,extra_headers", [
    ({}, {}),
    ({"X-Subroute-Request-ID": "bad", "x-subroute-attempt-id": str(uuid.uuid4())}, {}),
    ({"x-subroute-request-id": str(uuid.uuid4())}, {"X-Subroute-Attempt-ID": str(uuid.uuid4())}),
])
def test_absent_or_malformed_correlation_is_ignored_and_stripped(monkeypatch, proxy_headers, extra_headers):
    async def unchanged(self, user_api_key_dict, cache, data, call_type):
        assert "_subroute_sift_correlation" not in data
        return data

    monkeypatch.setattr(SiftLiteLLMHook, "async_pre_call_hook", unchanged)
    request = {
        "model": "gemini-3.8-flash-advisor",
        "messages": [{"role": "user", "content": "Review this."}],
        "proxy_server_request": {"headers": proxy_headers},
        "extra_headers": extra_headers,
    }

    result = asyncio.run(ExpertSiftHook().async_pre_call_hook({}, None, request, "aanthropic_messages"))

    assert result is not None
    assert not any(name.lower() in {"x-subroute-request-id", "x-subroute-attempt-id"}
                   for name in result["proxy_server_request"]["headers"])
    assert not any(name.lower() in {"x-subroute-request-id", "x-subroute-attempt-id"}
                   for name in result["extra_headers"])
    assert "_subroute_sift_correlation" not in result


def test_expert_sift_keeps_compacted_input_under_configured_budget():
    hook = ExpertSiftHook()
    assert EXPERT_SIFT_TARGET == 25_600
    assert hook.max_target == EXPERT_INPUT_LIMIT == 32_000
    assert set(hook.model_targets) == set(EXPERT_MODELS)


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

    async def compact(self, *args, **kwargs):
        seen.append(kwargs)
        return args[2]

    monkeypatch.setattr(SiftLiteLLMHook, "async_pre_call_hook", compact)
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
    hook.model_targets["codex-luna-advisor"] = 256
    hook.max_target = 512
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


def test_response_headers_expose_only_expert_server_audit(monkeypatch):
    import litellm
    from litellm.caching.caching import DualCache
    from litellm.proxy._types import UserAPIKeyAuth
    from litellm.proxy.utils import ProxyLogging

    hook = ExpertSiftHook()
    monkeypatch.setattr(litellm, "callbacks", [hook])
    monkeypatch.setattr(ProxyLogging, "_callback_capabilities_cache", {})
    proxy = ProxyLogging(user_api_key_cache=DualCache())
    data = {"model": "codex-luna-advisor", "metadata": {"sift": {
        "compacted": True, "input_tokens": 36_000, "forwarded_tokens": 25_000,
    }}}
    key, response, headers = UserAPIKeyAuth(), object(), {"x-test": "1"}
    assert asyncio.run(proxy.post_call_response_headers_hook(data, key, response, headers)) == {
        "x-sift-compacted": "true",
        "x-sift-estimated-input-tokens": "36000",
        "x-sift-estimated-forwarded-tokens": "25000",
    }

    ordinary = {"model": "codex-luna", "metadata": {"sift": data["metadata"]["sift"]}}
    assert asyncio.run(proxy.post_call_response_headers_hook(ordinary, key, response)) == {}
    assert asyncio.run(proxy.post_call_response_headers_hook({"model": "codex-luna-advisor"}, key, response)) == {}
    assert asyncio.run(proxy.post_call_response_headers_hook(
        {"model": "codex-luna-advisor", "metadata": {}}, key, response,
    )) == {}


def test_multimodal_expert_request_is_preserved_for_native_admission(monkeypatch):
    hook = ExpertSiftHook()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Sift must not rewrite multimodal expert content")

    monkeypatch.setattr(SiftLiteLLMHook, "async_pre_call_hook", forbidden)
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
