import asyncio

import httpx
import litellm
import pytest


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


def test_anthropic_expert_transport_builds_single_v1_path(monkeypatch):
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    requests = []

    async def capture(self, url, data=None, json=None, **kwargs):
        import json as json_codec

        body = json if json is not None else json_codec.loads(data)
        requests.append((str(url), body))
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
                "api_base": "http://fixture.invalid",
                "api_key": "fixture-only",
            },
        }],
        num_retries=0,
        fallbacks=[],
    )

    asyncio.run(router.aanthropic_messages(
        model="experts/codex-luna-advisor",
        messages=[{"role": "user", "content": "Review."}],
        max_tokens=64,
        metadata={"gateway_policy": {
            "advisor_model": "codex-luna-advisor", "reasoning_effort": "high",
        }},
    ))

    assert len(requests) == 1
    url, body = requests[0]
    assert url == "http://fixture.invalid/v1/messages"
    # LiteLLM's native Advisor tool loop forwards only its own sub-call metadata.
    # Expert defaults therefore come from the 4040 deployment config.
    assert body["metadata"] == {}


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


@pytest.mark.parametrize("stream", [False, True])
def test_target_failure_after_successful_advisor_does_not_replay_consultation(monkeypatch, stream):
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

    monkeypatch.setattr(litellm, "callbacks", [])
    monkeypatch.setattr(advisor, "_call_messages_handler", executor)
    monkeypatch.setattr(advisor, "_resolve_advisor_router", lambda _: Expert())
    router = litellm.Router(model_list=[
        {"model_name": "target", "litellm_params": {"model": "openai/gpt-4o", "api_key": "fixture"}},
        {"model_name": "fallback", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "fixture"}},
    ], num_retries=0, default_fallbacks=["fallback"], max_fallbacks=1)

    with pytest.raises(litellm.ServiceUnavailableError, match="final target failed after advice"):
        asyncio.run(router.aanthropic_messages(
            model="target", messages=[{"role": "user", "content": "Review"}],
            tools=[{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "expert", "max_uses": 1}],
            max_tokens=32, stream=stream,
        ))
    assert calls == [("target", "openai/gpt-4o"), ("expert", "expert"), ("target", "openai/gpt-4o")]


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
