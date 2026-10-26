import asyncio
import json

import httpx
import litellm
import pytest
from subroute import usage_accounting as accounting
from test_usage_accounting import MemoryStore


def test_failed_expert_transport_does_not_use_global_executor_fallback(monkeypatch):
    attempts = []

    async def fail_provider(**kwargs):
        attempts.append(kwargs["model"])
        raise RuntimeError("4040 unavailable")

    monkeypatch.setattr(litellm, "anthropic_messages", fail_provider)
    router = litellm.Router(
        model_list=[
            {
                "model_name": "experts/codex-luna-advisor",
                "litellm_params": {
                    "model": "anthropic/codex-luna-advisor",
                    "api_base": "http://127.0.0.1:9/v1",
                    "api_key": "fixture",
                },
            },
            {
                "model_name": "codex-luna",
                "litellm_params": {"model": "anthropic/fallback", "api_key": "fixture"},
            },
        ],
        fallbacks=[{"experts/codex-luna-advisor": []}],
        default_fallbacks=["codex-luna"],
        enable_pre_call_checks=False,
        num_retries=0,
    )
    with pytest.raises(RuntimeError, match="4040 unavailable"):
        asyncio.run(router.aanthropic_messages(
            model="experts/codex-luna-advisor",
            messages=[{"role": "user", "content": "Review."}],
            max_tokens=32,
        ))
    assert attempts == ["anthropic/codex-luna-advisor"]


@pytest.mark.parametrize("expert_base_url", [
    "http://experts:4040",
    "http://experts-staging:4040",
])
def test_anthropic_expert_transport_builds_single_v1_path(monkeypatch, expert_base_url):
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
    import uuid

    requests = []
    store = MemoryStore()
    plugin = accounting.UsageAccounting(store)
    root_id = str(uuid.uuid4())
    monkeypatch.setenv("EXPERTS_BASE_URL", expert_base_url)

    async def capture(self, url, data=None, json=None, **kwargs):
        import json as json_codec

        body = json if json is not None else json_codec.loads(data)
        requests.append((str(url), body, dict(kwargs.get("headers") or {})))
        request = httpx.Request("POST", str(url))
        return httpx.Response(200, request=request, json={
            "id": "msg_fixture", "type": "message", "role": "assistant",
            "model": "codex-luna-advisor", "content": [{"type": "text", "text": "fixture"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })

    monkeypatch.setattr(AsyncHTTPHandler, "post", capture)
    router = litellm.Router(
        model_list=[{
            "model_name": "experts/codex-luna-advisor",
            "litellm_params": {
                "model": "anthropic/codex-luna-advisor",
                    "api_base": expert_base_url,
                "api_key": "fixture-only",
            },
        }],
        num_retries=0,
        fallbacks=[],
    )

    async def scenario():
        trace_token = accounting.trace_context.set(
            accounting.Trace(root_id, "staging", "local-master", local_master=True)
        )
        native_token = accounting.native_advisor_context.set(True)
        role_token = accounting.role_context.set(None)
        try:
            deployment = {
                "model": "anthropic/codex-luna-advisor",
                "api_base": expert_base_url,
                "metadata": {"advisor_sub_call": True},
            }
            await plugin.async_pre_call_deployment_hook(deployment, "anthropic_messages")
            attempt_id = deployment["metadata"][accounting.TAG]
            await router.aanthropic_messages(
                model="experts/codex-luna-advisor",
                messages=[{"role": "user", "content": "Review."}],
                max_tokens=64,
                extra_headers=deployment["extra_headers"],
                metadata=deployment["metadata"],
            )
            return attempt_id
        finally:
            accounting.role_context.reset(role_token)
            accounting.native_advisor_context.reset(native_token)
            accounting.trace_context.reset(trace_token)

    attempt_id = asyncio.run(scenario())

    assert len(requests) == 1
    url, body, headers = requests[0]
    assert url == f"{expert_base_url}/v1/messages"
    # LiteLLM's native Advisor tool loop forwards only its own sub-call metadata.
    # Expert defaults therefore come from the 4040 deployment config.
    assert body["metadata"] == {}
    assert headers["x-subroute-request-id"] == root_id
    assert headers["x-subroute-attempt-id"] == attempt_id
    assert store.rows[attempt_id]["trace"].request_id == root_id
    assert "x-subroute-request-id" not in json.dumps(body)
    assert "x-subroute-attempt-id" not in json.dumps(body)


def test_gateway_advisor_ids_cross_experts_http_and_reach_sift(monkeypatch, caplog):
    import logging
    import re
    import uuid
    from copy import deepcopy

    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
    from litellm.proxy import proxy_server
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
    from litellm.proxy.auth.user_api_key_auth import LITELLM_PROXY_MASTER_KEY_ALIAS
    from litellm.types.utils import Usage
    from subroute.handlers.codex_advisor import codex_advisor_handler
    from subroute.plugins.expert_sift import ExpertSiftHook
    from test_codex_proxy_http import _configured_proxy

    expert_base_url = "http://experts:4040"
    monkeypatch.setenv("EXPERTS_BASE_URL", expert_base_url)
    store = MemoryStore()
    accounting_plugin = accounting.UsageAccounting(store)
    root_id = str(uuid.uuid4())
    history = [
        {"role": "user", "content": f"Historical record {index}: " + "detail " * 100}
        if index % 2 == 0 else
        {"role": "assistant", "content": f"Earlier result {index}: " + "summary " * 35}
        for index in range(12)
    ]
    original_messages = deepcopy(history + [{"role": "user", "content": "Keep this latest question."}])
    original_snapshot = deepcopy(original_messages)
    provider_calls = []
    http_requests = []
    hook = ExpertSiftHook()
    hook.model_targets["codex-luna-advisor"] = 512
    hook.max_target = 1_024
    caplog.set_level(logging.INFO, logger="LiteLLM Proxy")

    async def capture_experts_request(self, url, data=None, json=None, **kwargs):
        import json as json_codec

        body = json if json is not None else json_codec.loads(data)
        headers = dict(kwargs.get("headers") or {})
        http_requests.append((str(url), deepcopy(body), headers))
        response = experts_client.post(
            "/v1/messages", json=body, headers=headers,
        )
        request = httpx.Request("POST", str(url))
        return httpx.Response(
            response.status_code, request=request, content=response.content,
            headers=dict(response.headers),
        )

    async def capture_codex_provider(model, messages, *, reasoning_effort=None, max_output_tokens=None):
        provider_calls.append({
            "model": model,
            "messages": deepcopy(messages),
            "reasoning_effort": reasoning_effort,
            "max_output_tokens": max_output_tokens,
        })
        return "fixture expert answer", Usage(prompt_tokens=12, completion_tokens=3, total_tokens=15)

    monkeypatch.setattr(AsyncHTTPHandler, "post", capture_experts_request)
    monkeypatch.setattr(
        "subroute.handlers.codex_advisor.call_codex_streaming_collect",
        capture_codex_provider,
    )
    expert_model = {
        "model_name": "codex-luna-advisor",
        "model_info": {"context_window": 32_000, "max_input_tokens": 32_000},
        "litellm_params": {
            "model": "codex-advisor/gpt-6-luna",
            "api_key": "fixture-key",
            "allowed_openai_params": ["reasoning_effort"],
        },
    }
    with _configured_proxy(
        provider_model_list=[expert_model],
        additional_custom_providers=[{
            "provider": "codex-advisor",
            "custom_handler": codex_advisor_handler,
        }],
        additional_callbacks=[hook],
        enable_pre_call_checks=True,
    ) as (experts_client, *_):
        proxy_server.app.dependency_overrides[proxy_server.user_api_key_auth] = lambda: UserAPIKeyAuth(
            api_key=LITELLM_PROXY_MASTER_KEY_ALIAS,
            user_role=LitellmUserRoles.PROXY_ADMIN,
        )
        gateway_router = litellm.Router(
            model_list=[{
                "model_name": "experts/codex-luna-advisor",
                "litellm_params": {
                    "model": "anthropic/codex-luna-advisor",
                    "api_base": expert_base_url,
                    "api_key": "fixture-only",
                },
            }],
            num_retries=0,
            fallbacks=[],
        )

        async def scenario():
            trace_token = accounting.trace_context.set(
                accounting.Trace(root_id, "staging", "local-master", local_master=True)
            )
            native_token = accounting.native_advisor_context.set(True)
            role_token = accounting.role_context.set(None)
            try:
                deployment = {
                    "model": "anthropic/codex-luna-advisor",
                    "api_base": expert_base_url,
                    "metadata": {"advisor_sub_call": True},
                    "extra_headers": {
                        "x-client-marker": "preserved",
                        "X-Subroute-Request-ID": "forged",
                        "x-subroute-attempt-id": "forged",
                    },
                }
                await accounting_plugin.async_pre_call_deployment_hook(
                    deployment, "aanthropic_messages",
                )
                attempt_id = deployment["metadata"][accounting.TAG]
                result = await gateway_router.aanthropic_messages(
                    model="experts/codex-luna-advisor",
                    system="Retain these system instructions.",
                    messages=original_messages,
                    max_tokens=64,
                    extra_headers=deployment["extra_headers"],
                    metadata=deployment["metadata"],
                )
                return attempt_id, result
            finally:
                accounting.role_context.reset(role_token)
                accounting.native_advisor_context.reset(native_token)
                accounting.trace_context.reset(trace_token)

        attempt_id, result = asyncio.run(scenario())

    assert result["content"][0]["text"] == "fixture expert answer"
    assert len(http_requests) == 1
    request_url, request_body, request_headers = http_requests[0]
    assert request_url == f"{expert_base_url}/v1/messages"
    normalized_headers = {name.lower(): value for name, value in request_headers.items()}
    assert normalized_headers["x-subroute-request-id"] == root_id
    assert normalized_headers["x-subroute-attempt-id"] == attempt_id
    assert normalized_headers["x-client-marker"] == "preserved"
    assert sum(name.lower() == "x-subroute-request-id" for name in request_headers) == 1
    assert sum(name.lower() == "x-subroute-attempt-id" for name in request_headers) == 1
    assert "x-subroute-request-id" not in json.dumps(request_body)
    assert "x-subroute-attempt-id" not in json.dumps(request_body)
    assert len(provider_calls) == 1
    assert provider_calls[0]["max_output_tokens"] == 64
    assert len(provider_calls[0]["messages"]) < len(original_messages) + 1
    assert provider_calls[0]["messages"][0] == {
        "role": "system", "content": "Retain these system instructions.",
    }
    assert provider_calls[0]["messages"][-1]["content"] == "Keep this latest question."
    assert original_messages == original_snapshot
    assert store.rows[attempt_id]["trace"].request_id == root_id
    sift_lines = [
        record.getMessage() for record in caplog.records
        if "sift_request protocol=anthropic_messages" in record.getMessage()
    ]
    assert len(sift_lines) == 1, sift_lines
    assert "compacted=true" in sift_lines[0]
    assert f"request_id={root_id}" in sift_lines[0]
    assert f"advisor_attempt_id={attempt_id}" in sift_lines[0]
    counts = re.search(r"input_tokens=(\d+) forwarded_tokens=(\d+) target=(\d+)", sift_lines[0])
    assert counts is not None, sift_lines[0]
    input_tokens, forwarded_tokens, target_tokens = map(int, counts.groups())
    assert input_tokens > target_tokens >= forwarded_tokens
    assert target_tokens == 512


@pytest.mark.parametrize("stream", [False, True])
def test_failed_expert_dependency_does_not_replay_target_on_fallback(monkeypatch, stream):
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor
    from litellm.router import Router
    from litellm.types.llms.anthropic import ANTHROPIC_ADVISOR_TOOL_TYPE
    from subroute.plugins import advisor_plugin  # noqa: F401  # installs the narrow Router compatibility guard

    assert getattr(Router.async_function_with_fallbacks_common_utils, "_subroute_advisor_fallback_guard", False)

    calls = []

    async def executor(**kwargs):
        calls.append(("target", kwargs["model"]))
        return {
            "id": "msg_fixture", "type": "message", "role": "assistant",
            "model": kwargs["model"],
            "content": [{"type": "tool_use", "id": "advisor-1", "name": "advisor",
                         "input": {"question": "Review."}}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    class ExpertRouter:
        async def aanthropic_messages(self, **kwargs):
            calls.append(("expert", kwargs["model"]))
            raise litellm.ServiceUnavailableError(
                "fixture expert failed", llm_provider="anthropic", model="expert",
            )

    monkeypatch.setattr(litellm, "callbacks", [])
    monkeypatch.setattr(advisor, "_call_messages_handler", executor)
    monkeypatch.setattr(advisor, "_resolve_advisor_router", lambda model: ExpertRouter())
    router = litellm.Router(
        model_list=[
            {"model_name": "target", "litellm_params": {"model": "openai/gpt-4o", "api_key": "fixture"}},
            {"model_name": "fallback", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "fixture"}},
        ],
        num_retries=0,
        default_fallbacks=["fallback"],
        max_fallbacks=1,
    )

    with pytest.raises(litellm.ServiceUnavailableError) as raised:
        asyncio.run(router.aanthropic_messages(
            model="target",
            messages=[{"role": "user", "content": "Review."}],
            tools=[{
                "type": ANTHROPIC_ADVISOR_TOOL_TYPE,
                "name": "advisor",
                "model": "experts/codex-luna-advisor",
                "max_uses": 1,
            }],
            max_tokens=32,
            stream=stream,
        ))

    assert str(raised.value).find("fixture expert failed") >= 0
    assert calls == [("target", "openai/gpt-4o"), ("expert", "experts/codex-luna-advisor")]


def test_unmarked_target_failure_still_uses_configured_fallback(monkeypatch):
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    calls = []

    async def provider(self, url, data=None, json=None, **kwargs):
        import json as json_codec

        body = json if json is not None else json_codec.loads(data)
        calls.append(body["model"])
        request = httpx.Request("POST", str(url))
        if len(calls) == 1:
            response = httpx.Response(503, request=request, json={
                "type": "error", "error": {"type": "overloaded_error", "message": "fixture unavailable"},
            })
            raise httpx.HTTPStatusError(
                "fixture target unavailable", request=request, response=response,
            )
        return httpx.Response(200, request=request, json={
            "id": "msg_fixture", "type": "message", "role": "assistant",
            "model": body["model"], "content": [{"type": "text", "text": "Fallback answer."}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })

    monkeypatch.setattr(litellm, "callbacks", [])
    monkeypatch.setattr(AsyncHTTPHandler, "post", provider)
    router = litellm.Router(
        model_list=[
            {"model_name": "target", "litellm_params": {"model": "anthropic/target", "api_base": "http://fixture.invalid", "api_key": "fixture"}},
            {"model_name": "fallback", "litellm_params": {"model": "anthropic/fallback", "api_base": "http://fixture.invalid", "api_key": "fixture"}},
        ],
        num_retries=0,
        default_fallbacks=["fallback"],
        max_fallbacks=1,
    )

    result = asyncio.run(router.aanthropic_messages(
        model="target", messages=[{"role": "user", "content": "Normal request."}], max_tokens=32,
    ))

    assert result["content"][0]["text"] == "Fallback answer."
    assert calls == ["target", "fallback"]


def test_target_failure_after_successful_advisor_uses_fallback_without_reconsulting(monkeypatch):
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor
    from subroute.plugins.advisor_plugin import ADVISOR_TOOL_TYPE, AdvisorPlugin

    AdvisorPlugin()
    calls = []
    attempts = {}

    async def executor(**kwargs):
        model = kwargs["model"]
        calls.append(("target", model))
        attempts[model] = attempts.get(model, 0) + 1
        if attempts[model] > 1:
            raise litellm.ServiceUnavailableError("final target failed after advice", llm_provider="openai", model=model)
        return {"content": [{"type": "tool_use", "id": "ask", "name": "advisor", "input": {"question": "Review"}}]}

    class Expert:
        async def aanthropic_messages(self, **kwargs):
            calls.append(("expert", kwargs["model"]))
            return {"content": [{"type": "text", "text": "A concise risk."}]}

    fallback_requests = []
    original_anthropic_messages = litellm.anthropic_messages

    async def fallback_provider(**kwargs):
        tools = kwargs.get("tools") or []
        if any(tool.get("type") == ADVISOR_TOOL_TYPE for tool in tools):
            return await original_anthropic_messages(**kwargs)
        fallback_requests.append(kwargs)
        return {
            "id": "msg_fallback", "type": "message", "role": "assistant",
            "model": kwargs["model"], "content": [{"type": "text", "text": "Fallback answer."}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }

    monkeypatch.setattr(litellm, "callbacks", [])
    monkeypatch.setattr(litellm, "anthropic_messages", fallback_provider)
    monkeypatch.setattr(advisor, "_call_messages_handler", executor)
    monkeypatch.setattr(advisor, "_resolve_advisor_router", lambda _: Expert())
    router = litellm.Router(model_list=[
        {"model_name": "target", "litellm_params": {"model": "openai/gpt-4o", "api_key": "fixture"}},
        {"model_name": "fallback", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "fixture"}},
    ], num_retries=0, default_fallbacks=["fallback"], max_fallbacks=1)

    result = asyncio.run(router.aanthropic_messages(
        model="target", messages=[{"role": "user", "content": "Review"}],
        tools=[{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "expert", "max_uses": 1}],
        max_tokens=32, stream=False,
    ))
    assert result["content"][0]["text"] == "Fallback answer."
    assert calls == [("target", "openai/gpt-4o"), ("expert", "expert"), ("target", "openai/gpt-4o")]
    assert calls[1] == ("expert", "expert")
    assert len(fallback_requests) == 1
    assert fallback_requests[0]["model"] == "openai/gpt-4o-mini"
    assert all(tool.get("type") != ADVISOR_TOOL_TYPE for tool in fallback_requests[0].get("tools", []))


def test_advisor_selection_is_isolated_between_overlapping_and_later_requests(monkeypatch):
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor
    from litellm.router_utils.cooldown_handlers import is_advisor_orchestration_failure
    from subroute.plugins.advisor_plugin import ADVISOR_TOOL_TYPE, AdvisorPlugin

    AdvisorPlugin()
    calls = []

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def executor(**kwargs):
            question = kwargs["messages"][0]["content"]
            model = kwargs["model"]
            calls.append((question, model))
            if question == "consult" and len(kwargs["messages"]) == 1:
                return {"content": [{"type": "tool_use", "id": "ask", "name": "advisor", "input": {"question": "Review"}}]}
            if model == "openai/gpt-4o":
                raise litellm.ServiceUnavailableError("target unavailable", llm_provider="openai", model=model)
            return {"content": [{"type": "text", "text": "Fallback answer."}]}

        class Expert:
            async def aanthropic_messages(self, **kwargs):
                calls.append(("consult", "expert"))
                entered.set()
                await release.wait()
                return {"content": [{"type": "text", "text": "One risk."}]}

        monkeypatch.setattr(litellm, "callbacks", [])
        monkeypatch.setattr(advisor, "_call_messages_handler", executor)
        monkeypatch.setattr(advisor, "_resolve_advisor_router", lambda _: Expert())
        router = litellm.Router(model_list=[
            {"model_name": "target", "litellm_params": {"model": "openai/gpt-4o", "api_key": "fixture"}},
            {"model_name": "fallback", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "fixture"}},
        ], num_retries=0, default_fallbacks=["fallback"], max_fallbacks=1, allowed_fails=100)

        async def request(question):
            return await router.aanthropic_messages(
                model="target", messages=[{"role": "user", "content": question}], max_tokens=32,
                tools=[{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "expert", "max_uses": 1}],
            )

        consultation = asyncio.create_task(request("consult"))
        await asyncio.wait_for(entered.wait(), timeout=5)

        async def overlapping():
            try:
                return await request("overlapping")
            finally:
                release.set()

        failed, ordinary = await asyncio.gather(consultation, overlapping(), return_exceptions=True)
        assert isinstance(failed, litellm.ServiceUnavailableError)
        # A final target error still belongs to target deployment health.
        assert not is_advisor_orchestration_failure(failed)
        later = await request("later")
        assert ordinary["content"][0]["text"] == later["content"][0]["text"] == "Fallback answer."

    asyncio.run(scenario())
    assert [model for question, model in calls if question == "consult"] == ["openai/gpt-4o", "expert", "openai/gpt-4o"]
    for question in ("overlapping", "later"):
        assert [model for label, model in calls if label == question] == ["openai/gpt-4o", "openai/gpt-4o-mini"]
