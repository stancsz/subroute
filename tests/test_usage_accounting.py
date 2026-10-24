import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from litellm import Usage

from subroute import usage_accounting as accounting


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.mirrors = []
        self.fail_start = False
        self.fail_update = False

    async def start(self, trace, model, provider, role, kind="provider"):
        if self.fail_start:
            raise HTTPException(503, "Usage storage unavailable")
        ident = str(uuid.uuid4())
        self.rows[ident] = dict(trace=trace, model=model, provider=provider,
                               role=role, kind=kind, outcome="pending", facts={})
        return ident

    async def update(self, ident, facts, outcome="pending"):
        if self.fail_update:
            return False
        row = self.rows[ident]
        if row["outcome"] in {"pending", "interrupted"}:
            if row["outcome"] == "pending":
                row["outcome"] = outcome
            row["facts"].update(facts)
        return True

    async def pending(self, environment):
        return []

    async def checkpoint(self, ident, facts):
        return await self.update(ident, facts)

    async def mirror_advisor_log(self, ident=None):
        self.mirrors.append(ident)
        return True

    async def ack(self, environment, rows):
        self.acked = (environment, rows)


@pytest.fixture
def store(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(accounting.usage_accounting, "store", store)
    token = accounting.trace_context.set(None)
    yield store
    accounting.trace_context.reset(token)


def signed(secret, **overrides):
    obj = dict(request_id=str(uuid.uuid4()), parent_attempt_id=str(uuid.uuid4()),
               key_id="operator-key-1", customer_id="customer-1", environment="staging",
               api_path="/v1/chat/completions", role="advisor", iat=int(time.time()), exp=int(time.time())+60)
    obj.update(overrides)
    encoded = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return encoded + "." + hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()


def test_signed_attribution_rejects_forgery_expiry_wrong_path_and_environment():
    secret = "receipt-secret-" * 4
    grant = signed(secret)
    trace = accounting.verify_grant(grant, secret, "/v1/chat/completions")
    assert trace.customer_id == "customer-1" and trace.environment == "staging"
    for bad, path in ((grant, "/v1/messages"), (grant, "/key/generate"),
                      (signed(secret, exp=int(time.time())-1), "/v1/chat/completions"),
                      (signed(secret, environment="other"), "/v1/chat/completions"),
                      (grant[:-1] + ("0" if grant[-1] != "0" else "1"), "/v1/chat/completions")):
        with pytest.raises(HTTPException) as err:
            accounting.verify_grant(bad, secret, path)
        assert err.value.status_code == 401
    trace = accounting.verify_grant(signed(secret, customer_id="客户 one"), secret, "/v1/chat/completions")
    assert trace.customer_id == "客户 one"
    for payload in ([], None, "text", 7):
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        invalid = encoded + "." + hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        with pytest.raises(HTTPException) as err:
            accounting.verify_grant(invalid, secret, "/v1/chat/completions")
        assert err.value.status_code == 401


def test_protocol_translation_classification_uses_framework_dispatch_rules():
    assert accounting.deployment_kind({"model": "openai/chat_completions/gpt-4o-mini"}, "aresponses") == "bridge"
    assert accounting.deployment_kind({"model": "openai/gpt-4o-mini", "use_chat_completions_api": True}, "aresponses") == "bridge"
    assert accounting.deployment_kind({"model": "openai/gpt-4o-mini"}, "aresponses") == "provider"
    assert accounting.deployment_kind({"model": "anthropic/claude-sonnet-4-5"}, "aanthropic_messages") == "provider"
    assert accounting.deployment_kind({"model": "openai/gpt-4o-mini"}, "aanthropic_messages") == "bridge"
    assert accounting.deployment_kind({"model": "openai/gpt-4o-mini", "_skip_responses_api_bridge": True}, "acompletion") == "provider"


def test_litellm_internal_search_skips_model_usage_receipt_but_inference_is_accounted(store):
    from litellm.types.utils import CallTypes

    async def scenario():
        plugin = accounting.UsageAccounting(store)
        for call_type in (CallTypes.search, CallTypes.asearch):
            search_kwargs = {"query": "current weather", "litellm_metadata": {"marker": "preserved"}}
            result = await plugin.async_pre_call_deployment_hook(search_kwargs, call_type)
            assert result is search_kwargs
            assert result == {"query": "current weather", "litellm_metadata": {"marker": "preserved"}}
            assert accounting.attempt_id(result) is None
            await plugin.async_post_call_success_deployment_hook(result, {"results": []}, call_type)
            await plugin.async_post_call_failure_deployment_hook(result, RuntimeError("search failure"), call_type)
            assert store.rows == {}

        kwargs = {"model": "openai/fixture", "litellm_metadata": {"marker": "preserved"}}
        await plugin.async_pre_call_deployment_hook(kwargs, "acompletion")
        ident = accounting.attempt_id(kwargs)
        assert ident and store.rows[ident]["outcome"] == "pending"
        await plugin.async_post_call_success_deployment_hook(
            kwargs,
            {"usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}},
            "acompletion",
        )
        assert store.rows[ident]["outcome"] == "complete"
        assert store.rows[ident]["facts"]["input_tokens"] == 7

    asyncio.run(scenario())


def test_caller_metadata_cannot_attribute_a_local_request(store):
    async def scenario():
        plugin = accounting.UsageAccounting(store)
        accounting.install_stream_accounting()
        data = {"metadata": {accounting.TAG: str(uuid.uuid4()), "customer_id": "forged"},
                "proxy_server_request": {"url": "http://localhost/v1/chat/completions",
                                         "headers": {"x-litellm-end-user-id": "forged"}}}
        await plugin.async_pre_call_hook({"api_key": "fake-auth-object"}, None, data, "completion")
        kwargs = await plugin.async_pre_call_deployment_hook({"model": "openai/fixture", "metadata": data["metadata"]}, "acompletion")
        ident = accounting.attempt_id(kwargs)
        assert store.rows[ident]["trace"].customer_id is None
        assert store.rows[ident]["trace"].key_id == "local-unattributed"
        assert ident != data["metadata"].get(accounting.TAG)
    asyncio.run(scenario())


def test_direct_local_inference_aliases_are_canonical_and_management_is_not_a_receipt_route(store):
    async def scenario():
        for path in ("/chat/completions", "/responses", "/messages", "/audio/speech", "/v1/embeddings"):
            data = {"proxy_server_request": {"url": "http://fixture" + path, "headers": {}}}
            await accounting.usage_accounting.async_pre_call_hook({}, None, data, "completion")
            assert accounting.trace_context.get().api_path == (path if path.startswith("/v1/") else "/v1" + path)
        assert accounting.local_api_path("/key/generate") == "/local"
    asyncio.run(scenario())


def test_missing_cost_tokens_and_litellm_estimates_are_not_zero():
    assert accounting.usage_facts({"usage": None, "response_cost": 99}) == {}
    assert accounting.usage_facts({"usage": {"prompt_tokens": True, "completion_tokens": -1}}) == {}
    assert accounting.usage_facts({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}) == {"input_tokens": 0, "output_tokens": 0}


def test_outage_blocks_direct_spending_and_checkpoint_retry(store, monkeypatch):
    async def scenario():
        accounting.trace_context.set(accounting.Trace(str(uuid.uuid4()), "staging"))
        store.fail_start = True
        with pytest.raises(HTTPException):
            async with accounting.direct_attempt("fixture", "fixture", "advisor"):
                pytest.fail("Provider dispatch after pending receipt failure")
        store.fail_start = False
        ident = await store.start(accounting.trace_context.get(), "fixture", "fixture", "advisor")
        attempt = accounting.DirectAttempt(ident, store)
        clock = [20.0]
        monkeypatch.setattr(accounting.time, "monotonic", lambda: clock[0])
        store.fail_update = True
        await attempt.observe({"usage": {"input_tokens": 4}})
        assert not attempt.saved
        store.fail_update = False
        clock[0] += 15
        await attempt.observe({"usage": {"input_tokens": 4}})
        assert store.rows[ident]["facts"] == {"input_tokens": 4}
    asyncio.run(scenario())


def test_receipt_endpoints_separate_auth_bounded_ack_and_environment(store, monkeypatch):
    secret = "receipt-service-" * 4
    monkeypatch.setenv("SUBROUTE_ACCOUNTING_SECRET", secret)
    app = FastAPI()
    accounting.install_receipt_routes(app)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            path = "/internal/accounting/receipts?environment=staging"
            assert (await client.get(path, headers={"authorization": "Bearer " + secret})).status_code == 401
            headers = {"x-subroute-accounting-token": secret}
            assert (await client.get(path, headers=headers)).json() == {"receipts": []}
            assert (await client.get(path.replace("staging", "other"), headers=headers)).status_code == 400
            ack = [{"attempt_id": str(uuid.uuid4()), "revision": 2}]
            response = await client.post("/internal/accounting/ack?environment=staging", headers=headers, json={"receipts": ack})
            assert response.status_code == 200 and store.acked == ("staging", ack)
            assert (await client.post("/internal/accounting/ack?environment=staging", headers=headers,
                                     json={"receipts": [{"attempt_id": "bad", "revision": 1}]})).status_code == 400
            assert (await client.post("/internal/accounting/ack?environment=staging", headers=headers,
                                     content=b"x" * 17000)).status_code == 413
            for invalid in ([], None, "text", 7, {"receipts": [None]}):
                assert (await client.post("/internal/accounting/ack?environment=staging", headers=headers,
                                         content=json.dumps(invalid))).status_code == 400
    asyncio.run(scenario())


@pytest.mark.parametrize("provider_usage", [None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                           {"prompt_tokens": 18, "completion_tokens": 5, "total_tokens": 23}])
def test_actual_litellm_deployment_and_stream_callbacks(store, provider_usage):
    """Run the installed framework, not manual callback invocations."""
    import litellm
    from litellm import Router
    from litellm.types.utils import ModelResponse, ModelResponseStream, GenericStreamingChunk

    class FixtureLLM(litellm.CustomLLM):
        async def acompletion(self, *args, **kwargs):
            return ModelResponse(model="fixture-model", choices=[{"message": {"role": "assistant", "content": "fixture response"}}],
                                 usage=Usage(prompt_tokens=17, completion_tokens=4, total_tokens=21))

        async def astreaming(self, *args, **kwargs):
            yield GenericStreamingChunk(text="fixture response", is_finished=False, finish_reason=None, usage=None, index=0)
            yield GenericStreamingChunk(text="", is_finished=True, finish_reason="stop",
                                        usage=provider_usage, index=0)

    async def scenario():
        plugin = accounting.usage_accounting
        accounting.install_stream_accounting()
        old_callbacks = litellm.callbacks[:]
        callback_names = ("input_callback", "success_callback", "failure_callback", "_async_input_callback", "_async_success_callback", "_async_failure_callback")
        old_lists = {name: list(getattr(litellm, name)) for name in callback_names}
        old_map = litellm.custom_provider_map[:]
        litellm.callbacks = [plugin]
        litellm.custom_provider_map = [*old_map, {"provider": "receipt-fixture", "custom_handler": FixtureLLM()}]
        from litellm.utils import custom_llm_setup
        custom_llm_setup()
        try:
            router = Router(model_list=[{"model_name": "fixture", "litellm_params": {"model": "receipt-fixture/fixture-model"}}],
                            num_retries=0)
            await plugin.async_pre_call_hook({}, None, {}, "completion")
            await router.acompletion(model="fixture", messages=[{"role": "user", "content": "private prompt"}])
            response = await router.acompletion(model="fixture", messages=[{"role": "user", "content": "private prompt"}], stream=True)
            async for _chunk in response:
                pass
            await asyncio.sleep(0.2)
            rows = list(store.rows.values())
            assert len(rows) == 2
            assert all(row["outcome"] == "complete" for row in rows), rows
            assert [row["facts"].get("input_tokens") for row in rows] == [17, provider_usage["prompt_tokens"] if provider_usage else None]
            assert rows[1]["facts"]["usage_complete"] == (provider_usage is not None)
        finally:
            litellm.callbacks = old_callbacks
            for name, values in old_lists.items():
                setattr(litellm, name, values)
            litellm.custom_provider_map = old_map
    asyncio.run(scenario())


def test_actual_framework_advisor_calls_keep_parent_identity_and_restore_executor_role(store, monkeypatch):
    import litellm
    from litellm import Router, ModelResponse
    from litellm.utils import custom_llm_setup

    class Fixture(litellm.CustomLLM):
        calls = 0

        async def acompletion(self, *args, **kwargs):
            self.calls += 1
            message, finish = {"role": "assistant", "content": "fixture"}, "stop"
            if self.calls == 1:
                message = {"role": "assistant", "content": "", "tool_calls": [{"id": "call_fixture", "type": "function",
                    "function": {"name": "advisor", "arguments": '{"question":"fixture"}'}}]}
                finish = "tool_calls"
            return ModelResponse(model="gpt-4o-mini", choices=[{"message": message, "finish_reason": finish}],
                                 usage=Usage(prompt_tokens=17, completion_tokens=4, total_tokens=21))

    async def scenario():
        provider = Fixture()
        names = ("callbacks", "input_callback", "success_callback", "failure_callback", "_async_input_callback", "_async_success_callback", "_async_failure_callback")
        old = {name: list(getattr(litellm, name)) for name in names}
        old_map = litellm.custom_provider_map[:]
        monkeypatch.setattr(litellm, "callbacks", [accounting.usage_accounting])
        monkeypatch.setattr(litellm, "use_chat_completions_url_for_anthropic_messages", True)
        litellm.custom_provider_map = [*old_map, {"provider": "receipt-advisor-fixture", "custom_handler": provider}]
        custom_llm_setup()
        trace = accounting.Trace(str(uuid.uuid4()), "staging", "key-1", "customer-1", str(uuid.uuid4()), "/v1/messages")
        token = accounting.trace_context.set(trace)
        try:
            accounting.install_stream_accounting()
            router = Router(model_list=[{"model_name": "fixture", "litellm_params": {"model": "receipt-advisor-fixture/gpt-4o-mini"}}], num_retries=0)
            await router.aanthropic_messages(model="fixture", max_tokens=10, messages=[{"role": "user", "content": "fixture"}],
                tools=[{"type": "advisor_20260301", "name": "advisor", "model": "receipt-advisor-fixture/advice", "max_uses": 1}])
            await asyncio.sleep(0.2)
            rows = [row for row in store.rows.values() if row["kind"] == "provider"]
            assert provider.calls == 3 and len(rows) == 3
            assert [row["role"] for row in rows] == ["inference", "advisor", "inference"]
            assert all(row["trace"] == trace and row["outcome"] == "complete" for row in rows)
            assert accounting.native_advisor_context.get() is False and accounting.role_context.get() is None
        finally:
            accounting.trace_context.reset(token)
            litellm.custom_provider_map = old_map
            for name, values in old.items():
                setattr(litellm, name, values)
    asyncio.run(scenario())
