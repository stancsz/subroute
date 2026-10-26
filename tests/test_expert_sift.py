import asyncio
from copy import deepcopy
import logging
import threading
import time
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
    experts_master = UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN,
        api_key=LITELLM_PROXY_MASTER_KEY_ALIAS,
    )
    hook.model_targets["gemini-3.8-flash-advisor"] = 512
    caplog.set_level(logging.INFO, logger="LiteLLM Proxy")
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
            hook.async_pre_call_hook(experts_master, None, request, "aanthropic_messages")
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
    assert all("x-subroute-request-id" not in line and "x-subroute-attempt-id" not in line for line in sift_lines)
    for root_id, attempt_id in ids:
        assert any(f"request_id={root_id}" in line and f"advisor_attempt_id={attempt_id}" in line for line in sift_lines)
    for line in sift_lines:
        assert sum(root_id in line or attempt_id in line for root_id, attempt_id in ids) == 1
        assert any(root_id in line and attempt_id in line for root_id, attempt_id in ids)


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


@pytest.mark.parametrize("auth_kind", ["virtual_key", "other_admin_key"])
def test_valid_correlation_requires_expert_master_auth(monkeypatch, auth_kind):
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

    seen = []

    async def capture(self, user_api_key_dict, cache, data, call_type):
        seen.append(data.get("_subroute_sift_correlation"))
        return data

    monkeypatch.setattr(SiftLiteLLMHook, "async_pre_call_hook", capture)
    root_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    auth = UserAPIKeyAuth(
        api_key="fixture-only",
        user_role=(LitellmUserRoles.INTERNAL_USER if auth_kind == "virtual_key"
                   else LitellmUserRoles.PROXY_ADMIN),
    )
    request = {
        "model": "gemini-3.8-flash-advisor",
        "messages": [{"role": "user", "content": "Review this request."}],
        "proxy_server_request": {"headers": {
            "x-subroute-request-id": root_id,
            "x-subroute-attempt-id": attempt_id,
        }},
    }

    result = asyncio.run(ExpertSiftHook().async_pre_call_hook(
        auth, None, request, "aanthropic_messages",
    ))

    assert seen == [None]
    assert "_subroute_sift_correlation" not in result
    assert result["proxy_server_request"]["headers"] == {}
    assert request["proxy_server_request"]["headers"] == {
        "x-subroute-request-id": root_id,
        "x-subroute-attempt-id": attempt_id,
    }


@pytest.mark.parametrize("duplicate_header", [
    "X-Subroute-Request-ID",
    "X-Subroute-Attempt-ID",
])
def test_duplicate_case_variant_correlation_header_is_ignored(monkeypatch, duplicate_header):
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
    from litellm.proxy.auth.user_api_key_auth import LITELLM_PROXY_MASTER_KEY_ALIAS

    seen = []

    async def capture(self, user_api_key_dict, cache, data, call_type):
        seen.append(data.get("_subroute_sift_correlation"))
        return data

    monkeypatch.setattr(SiftLiteLLMHook, "async_pre_call_hook", capture)
    root_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    headers = {
        "x-subroute-request-id": root_id,
        "x-subroute-attempt-id": attempt_id,
        duplicate_header: str(uuid.uuid4()),
    }
    admin = UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN,
        api_key=LITELLM_PROXY_MASTER_KEY_ALIAS,
    )
    request = {
        "model": "gemini-3.8-flash-advisor",
        "messages": [{"role": "user", "content": "Review this request."}],
        "proxy_server_request": {"headers": headers},
    }

    result = asyncio.run(ExpertSiftHook().async_pre_call_hook(
        admin, None, request, "aanthropic_messages",
    ))

    assert seen == [None]
    assert "_subroute_sift_correlation" not in result
    assert result["proxy_server_request"]["headers"] == {}
    assert request["proxy_server_request"]["headers"] == headers


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


@pytest.mark.parametrize(("reason", "status"), [
    ("unsupported_content", 422),
    ("invalid", 400),
])
def test_compaction_failure_reports_count_without_a_success_audit(monkeypatch, reason, status):
    from fastapi import HTTPException
    from litellm.proxy import proxy_server
    from sift.core import CompactionError
    import sift.litellm_proxy as sift_proxy

    class CountingRouter:
        @staticmethod
        def _count_pre_call_check_tokens(*, messages, input, request_kwargs):
            return 137

    monkeypatch.setattr(proxy_server, "llm_router", CountingRouter())

    def fail_after_count(messages, count, **kwargs):
        assert count(messages) == 137
        raise CompactionError("fixture compaction failure", code=reason)

    monkeypatch.setattr(sift_proxy, "compact_messages", fail_after_count)
    request = {
        "model": "gemini-3.8-flash-advisor",
        "messages": [{"role": "user", "content": "Keep this expert request."}],
        "metadata": {"keep": "caller metadata"},
    }
    original_messages = deepcopy(request["messages"])

    with pytest.raises(HTTPException) as raised:
        asyncio.run(ExpertSiftHook().async_pre_call_hook({}, None, request, "aanthropic_messages"))

    assert raised.value.status_code == status
    assert raised.value.detail["error"]["code"] == "sift_compaction_failed"
    assert raised.value.detail["error"]["reason"] == reason
    assert raised.value.detail["error"]["input_tokens"] == 137
    assert raised.value.detail["error"]["target_tokens"] == EXPERT_SIFT_TARGET
    assert request["messages"] == original_messages
    assert "sift" not in request["metadata"]


@pytest.mark.parametrize("trusted_master", [False, True])
def test_compaction_failure_logs_only_authenticated_correlation_ids(
    monkeypatch, caplog, trusted_master,
):
    from fastapi import HTTPException
    from litellm.proxy import proxy_server
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
    from litellm.proxy.auth.user_api_key_auth import LITELLM_PROXY_MASTER_KEY_ALIAS
    from sift.core import CompactionError
    import sift.litellm_proxy as sift_proxy

    class CountingRouter:
        @staticmethod
        def _count_pre_call_check_tokens(*, messages, input, request_kwargs):
            return 137

    monkeypatch.setattr(proxy_server, "llm_router", CountingRouter())

    def fail_compaction(messages, count, **kwargs):
        assert count(messages) == 137
        raise CompactionError("fixture compaction failure", code="unsupported_content")

    monkeypatch.setattr(sift_proxy, "compact_messages", fail_compaction)
    caplog.set_level(logging.WARNING, logger="LiteLLM Proxy")
    root_id, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    auth = UserAPIKeyAuth(
        api_key=(LITELLM_PROXY_MASTER_KEY_ALIAS if trusted_master else "fixture-virtual-key"),
        user_role=(LitellmUserRoles.PROXY_ADMIN if trusted_master else LitellmUserRoles.INTERNAL_USER),
    )
    request = {
        "model": "gemini-3.8-flash-advisor",
        "messages": [{"role": "user", "content": "Keep this request."}],
        "proxy_server_request": {"headers": {
            "x-subroute-request-id": root_id,
            "x-subroute-attempt-id": attempt_id,
        }},
    }

    with pytest.raises(HTTPException) as raised:
        asyncio.run(ExpertSiftHook().async_pre_call_hook(
            auth, None, request, "aanthropic_messages",
        ))

    assert raised.value.status_code == 422
    failure_logs = [record.getMessage() for record in caplog.records
                    if "sift_compaction_failed" in record.getMessage()]
    assert len(failure_logs) == 1, failure_logs
    if trusted_master:
        assert f"request_id={root_id}" in failure_logs[0]
        assert f"advisor_attempt_id={attempt_id}" in failure_logs[0]
    else:
        assert "request_id=-" in failure_logs[0]
        assert "advisor_attempt_id=-" in failure_logs[0]
        assert root_id not in failure_logs[0]
        assert attempt_id not in failure_logs[0]
    assert "input_tokens=137" in failure_logs[0]


def test_irreducible_expert_input_fails_after_one_relaxation_with_count(monkeypatch):
    from fastapi import HTTPException
    from litellm.proxy import proxy_server
    import sift.litellm_proxy as sift_proxy
    from sift.core import compact_messages as original_compact_messages

    class CountingRouter:
        @staticmethod
        def _count_pre_call_check_tokens(*, messages, input, request_kwargs):
            instructions = request_kwargs.get("system", "")
            return len(instructions) // 4 + sum(
                len(str(message.get("content", ""))) // 4 for message in messages or []
            )

    monkeypatch.setattr(proxy_server, "llm_router", CountingRouter())
    targets = []

    def record_target(messages, count, **kwargs):
        targets.append(kwargs["target"])
        return original_compact_messages(messages, count, **kwargs)

    monkeypatch.setattr(sift_proxy, "compact_messages", record_target)
    hook = ExpertSiftHook()
    instruction = "x" * ((32_000 + 1) * 4)
    request = {
        "model": "gemini-3.8-flash-advisor",
        "system": instruction,
        "messages": [{"role": "user", "content": "Review."}],
    }

    with pytest.raises(HTTPException) as raised:
        asyncio.run(hook.async_pre_call_hook({}, None, request, "aanthropic_messages"))

    error = raised.value.detail["error"]
    assert raised.value.status_code == 413
    assert error["code"] == "sift_compaction_failed"
    assert error["reason"] == "active_too_large"
    assert error["input_tokens"] == 32_002
    assert error["target_tokens"] == EXPERT_SIFT_TARGET
    assert targets == [EXPERT_SIFT_TARGET, EXPERT_INPUT_LIMIT]
    assert request["system"] == instruction
    assert request["messages"] == [{"role": "user", "content": "Review."}]
    assert "sift" not in request.get("metadata", {})


def test_busy_compaction_is_rejected_with_retry_hint(monkeypatch):
    from litellm.proxy._types import ProxyException
    from litellm.proxy import proxy_server

    monkeypatch.setattr(proxy_server, "llm_router", object())
    hook = ExpertSiftHook()
    request = {
        "model": "gemini-3.8-flash-advisor",
        "messages": [{"role": "user", "content": "Review."}],
        "metadata": {"keep": "caller metadata"},
    }

    acquired = 0
    try:
        for _ in range(2):
            assert hook._slots.acquire(blocking=False)
            acquired += 1
        with pytest.raises(ProxyException) as raised:
            asyncio.run(hook.async_pre_call_hook({}, None, request, "aanthropic_messages"))
    finally:
        for _ in range(acquired):
            hook._slots.release()

    assert raised.value.code == "429"
    assert raised.value.headers["Retry-After"] == "1"
    assert "sift" not in request["metadata"]


def test_cancelled_compaction_keeps_its_slot_until_worker_finishes(monkeypatch):
    from litellm.proxy import proxy_server
    import sift.litellm_proxy as sift_proxy

    started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    worker_timed_out = threading.Event()

    def blocking_compact(router, request, **kwargs):
        started.set()
        if not release_worker.wait(5):
            worker_timed_out.set()
        worker_finished.set()
        return request

    monkeypatch.setattr(proxy_server, "llm_router", object())
    monkeypatch.setattr(sift_proxy, "compact_proxy_request", blocking_compact)
    hook = ExpertSiftHook()

    async def scenario():
        task = asyncio.create_task(hook.async_pre_call_hook(
            {}, None,
            {"model": "gemini-3.8-flash-advisor", "messages": [{"role": "user", "content": "Review."}]},
            "aanthropic_messages",
        ))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            test_slot = hook._slots.acquire(blocking=False)
            second_slot = hook._slots.acquire(blocking=False)
            try:
                assert test_slot
                assert not second_slot
            finally:
                if second_slot:
                    hook._slots.release()
                if test_slot:
                    hook._slots.release()
        finally:
            release_worker.set()

        assert await asyncio.to_thread(worker_finished.wait, 3)
        assert not worker_timed_out.is_set()
        deadline = time.monotonic() + 1
        available = 0
        while available < 2 and time.monotonic() < deadline:
            if hook._slots.acquire(blocking=False):
                available += 1
            else:
                await asyncio.sleep(0.01)
        assert available == 2
        for _ in range(available):
            hook._slots.release()

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", ["chat", "messages", "responses"])
def test_experts_http_compacts_before_single_provider_dispatch(monkeypatch, protocol):
    from litellm.proxy import proxy_server
    from litellm.types.utils import Usage

    from test_codex_proxy_http import _configured_proxy
    from subroute.handlers.codex_advisor import codex_advisor_handler

    hook = ExpertSiftHook()
    hook.model_targets["codex-luna-advisor"] = 2_048
    hook.max_target = 4_096
    provider_calls = []

    async def capture_provider(model, messages, *, reasoning_effort=None, max_output_tokens=None):
        provider_count = proxy_server.llm_router._count_pre_call_check_tokens(
            messages=messages,
            input=None,
            request_kwargs={"model": "codex-luna-advisor", "messages": messages},
        )
        provider_calls.append({
            "model": model,
            "messages": deepcopy(messages),
            "input_tokens": provider_count,
            "reasoning_effort": reasoning_effort,
            "max_output_tokens": max_output_tokens,
        })
        return "fixture answer", Usage(prompt_tokens=12, completion_tokens=3, total_tokens=15)

    monkeypatch.setattr(
        "subroute.handlers.codex_advisor.call_codex_streaming_collect",
        capture_provider,
    )
    model = {
        "model_name": "codex-luna-advisor",
        "model_info": {"context_window": 32_000, "max_input_tokens": 32_000},
        "litellm_params": {
            "model": "codex-advisor/gpt-6-luna",
            "api_key": "fixture-key",
            "allowed_openai_params": ["reasoning_effort"],
        },
    }
    history = []
    for index in range(20):
        history.extend([
            {"role": "user", "content": f"Historical record {index}: " + "detail " * 80},
            {"role": "assistant", "content": f"Earlier result {index}: " + "summary " * 24},
        ])
    messages = [
        {"role": "system", "content": "Keep the safety instructions."},
        *history,
        {"role": "user", "content": "Answer this latest question exactly."},
    ]
    if protocol == "messages":
        path = "/v1/messages"
        request_data = {
            "model": "codex-luna-advisor",
            "system": messages[0]["content"],
            "messages": messages[1:],
            "max_tokens": 64,
        }
        headers = {"anthropic-version": "2023-06-01"}
    elif protocol == "chat":
        path = "/v1/chat/completions"
        request_data = {
            "model": "codex-luna-advisor",
            "messages": messages,
            "max_completion_tokens": 64,
        }
        headers = {}
    else:
        path = "/v1/responses"
        request_data = {
            "model": "codex-luna-advisor",
            "instructions": messages[0]["content"],
            "input": [
                {
                    "type": "message",
                    "role": message["role"],
                    "content": [{
                        "type": "output_text" if message["role"] == "assistant" else "input_text",
                        "text": message["content"],
                    }],
                }
                for message in messages[1:]
            ],
            "max_output_tokens": 64,
            "store": False,
        }
        headers = {}
    with _configured_proxy(
        provider_model_list=[model],
        additional_custom_providers=[{
            "provider": "codex-advisor",
            "custom_handler": codex_advisor_handler,
        }],
        additional_callbacks=[hook],
        enable_pre_call_checks=True,
    ) as (client, *_):
        input = request_data.get("input")
        counter_messages = request_data.get("messages")
        input_tokens = proxy_server.llm_router._count_pre_call_check_tokens(
            messages=counter_messages,
            input=input,
            request_kwargs=request_data,
        )
        response = client.post(
            path, json=request_data, headers=headers,
        )

    assert response.status_code == 200, response.text
    assert "fixture answer" in response.text
    assert len(provider_calls) == 1
    provider_messages = provider_calls[0]["messages"]
    assert len(provider_messages) < len(messages)
    assert provider_messages[0] == messages[0]
    latest_content = provider_messages[-1].get("content")
    if isinstance(latest_content, list):
        latest_text = "".join(
            block.get("text", "") for block in latest_content
            if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"}
        )
    else:
        latest_text = latest_content
    assert latest_text == messages[-1]["content"]
    assert provider_calls[0]["max_output_tokens"] == 64
    assert response.headers["x-sift-compacted"] == "true"
    forwarded_tokens = int(response.headers["x-sift-estimated-forwarded-tokens"])
    assert int(response.headers["x-sift-estimated-input-tokens"]) == input_tokens
    assert provider_calls[0]["input_tokens"] == forwarded_tokens
    assert input_tokens > forwarded_tokens
    assert forwarded_tokens <= hook.max_target


@pytest.mark.parametrize("protocol", ["chat", "messages", "responses"])
@pytest.mark.parametrize("failure", ["unsupported", "irreducible", "error", "busy"])
def test_experts_http_compaction_failure_stops_before_codex_provider_dispatch(
    monkeypatch, caplog, protocol, failure,
):
    from test_codex_proxy_http import _configured_proxy
    from sift.core import CompactionError
    from subroute.handlers.codex_advisor import codex_advisor_handler

    hook = ExpertSiftHook()
    provider_calls = []
    counted_tokens = []

    failure_reasons = {
        "unsupported": "unsupported_content",
        "irreducible": "active_too_large",
        "error": "invalid",
    }
    expected_reasons = failure_reasons
    expected_statuses = {
        "unsupported": 422,
        "irreducible": 413,
        "error": 400,
        "busy": 429,
    }

    def fail_compaction(messages, count, **kwargs):
        counted_tokens.append(count(messages))
        reason = failure_reasons[failure]
        raise CompactionError(f"fixture {failure} input", code=reason)

    async def capture_provider(model, messages, *, reasoning_effort=None, max_output_tokens=None):
        provider_calls.append(model)
        raise AssertionError("provider dispatch must not follow Sift failure")

    if failure != "busy":
        monkeypatch.setattr("sift.litellm_proxy.compact_messages", fail_compaction)
    else:
        monkeypatch.setattr(
            "sift.litellm_proxy.compact_messages",
            lambda *args, **kwargs: pytest.fail("busy admission must reject before compaction"),
        )
    monkeypatch.setattr(
        "subroute.handlers.codex_advisor.call_codex_streaming_collect",
        capture_provider,
    )
    model = {
        "model_name": "codex-luna-advisor",
        "model_info": {"context_window": 32_000, "max_input_tokens": 32_000},
        "litellm_params": {
            "model": "codex-advisor/gpt-6-luna",
            "api_key": "fixture-key",
            "allowed_openai_params": ["reasoning_effort"],
        },
    }
    history = [{"role": "user", "content": "Review this fixture."}]
    if protocol == "messages":
        path = "/v1/messages"
        payload = {"model": model["model_name"], "messages": history, "max_tokens": 64}
        headers = {"anthropic-version": "2023-06-01"}
    elif protocol == "chat":
        path = "/v1/chat/completions"
        payload = {"model": model["model_name"], "messages": history, "max_completion_tokens": 64}
        headers = {}
    else:
        path = "/v1/responses"
        payload = {
            "model": model["model_name"],
            "input": [{"role": "user", "content": "Review this fixture."}],
            "max_output_tokens": 64,
            "store": False,
        }
        headers = {}

    occupied_slots = 0
    if failure == "busy":
        for _ in range(2):
            assert hook._slots.acquire(blocking=False)
            occupied_slots += 1
    try:
        with _configured_proxy(
            provider_model_list=[model],
            additional_custom_providers=[{
                "provider": "codex-advisor",
                "custom_handler": codex_advisor_handler,
            }],
            additional_callbacks=[hook],
            enable_pre_call_checks=True,
        ) as (client, *_):
            response = client.post(path, json=payload, headers=headers)
    finally:
        for _ in range(occupied_slots):
            hook._slots.release()

    assert response.status_code == expected_statuses[failure], response.text
    error = response.json()["error"]
    assert not any(
        "sift_request protocol=" in record.getMessage()
        for record in caplog.records
    )
    if failure == "busy":
        expected_busy_type = "rate_limit_error" if protocol == "messages" else "invalid_request_error"
        assert error["type"] == expected_busy_type
        assert "Sift is busy" in error["message"]
        assert response.headers.get("retry-after") == "1"
        assert counted_tokens == []
        assert not any(name.lower().startswith("x-sift-") for name in response.headers)
        assert provider_calls == []
        return

    messages_error_types = {
        "unsupported": "api_error",
        "irreducible": "request_too_large",
        "error": "invalid_request_error",
    }
    expected_error_type = (
        messages_error_types[failure] if protocol == "messages"
        else "invalid_request_error"
    )
    assert error["type"] == expected_error_type
    reason = expected_reasons[failure]
    assert error["message"] == f"fixture {failure} input"
    failure_logs = [record.getMessage() for record in caplog.records
                    if "sift_compaction_failed" in record.getMessage()]
    assert any(
        f"reason={reason}" in message
        and f"input_tokens={counted_tokens[0]}" in message
        and "target=25600" in message
        for message in failure_logs
    ), failure_logs
    assert len(counted_tokens) == (2 if failure == "irreducible" else 1)
    assert provider_calls == []
    assert not any(name.lower().startswith("x-sift-") for name in response.headers)
