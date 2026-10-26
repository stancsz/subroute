"""Installed LiteLLM protocols with isolated loopback HTTP provider fixtures."""
import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import litellm
import pytest
from litellm import Router
from subroute import usage_accounting as accounting
from test_usage_accounting import MemoryStore
from litellm.llms.anthropic.pass_through.messages.interceptors.advisor import ANTHROPIC_ADVISOR_TOOL_TYPE


def test_native_advisor_accounting_install_is_idempotent(monkeypatch):
    from litellm.llms.anthropic.pass_through.messages.interceptors.advisor import AdvisorOrchestrationHandler

    async def original(self, *args, **kwargs):
        return "fixture"

    monkeypatch.setattr(AdvisorOrchestrationHandler, "handle", original)
    accounting.install_native_advisor_accounting()
    installed = AdvisorOrchestrationHandler.handle

    accounting.install_native_advisor_accounting()

    assert AdvisorOrchestrationHandler.handle is installed


def test_local_master_call_id_is_the_accounting_request_id_only_when_uuid():
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
    from litellm.proxy.auth.user_api_key_auth import LITELLM_PROXY_MASTER_KEY_ALIAS

    plugin = accounting.UsageAccounting(MemoryStore())
    admin = UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN,
                           api_key=LITELLM_PROXY_MASTER_KEY_ALIAS)
    root_id = str(uuid.uuid4())

    async def scenario():
        await plugin.async_pre_call_hook(admin, None, {
            "proxy_server_request": {"headers": {"x-litellm-call-id": root_id}},
        }, "anthropic_messages")
        assert accounting.trace_context.get().request_id == root_id
        assert accounting.trace_context.get().local_master is True

        await plugin.async_pre_call_hook(UserAPIKeyAuth(api_key="fixture-key"), None, {
            "proxy_server_request": {"headers": {"x-litellm-call-id": root_id}},
        }, "anthropic_messages")
        assert accounting.trace_context.get().request_id != root_id

        await plugin.async_pre_call_hook(admin, None, {
            "proxy_server_request": {"headers": {"x-litellm-call-id": "not-a-uuid"}},
        }, "anthropic_messages")
        assert accounting.trace_context.get().request_id != "not-a-uuid"

    try:
        asyncio.run(scenario())
    finally:
        accounting.trace_context.set(None)


def test_advisor_attempt_headers_are_scoped_and_override_forged_values():
    root_id = str(uuid.uuid4())
    trace = accounting.Trace(root_id, "staging", "local-master", local_master=True)
    store = MemoryStore()
    plugin = accounting.UsageAccounting(store)

    async def scenario():
        trace_token = accounting.trace_context.set(trace)
        advisor_token = accounting.native_advisor_context.set(True)
        try:
            kwargs = {
                "model": "anthropic/codex-luna-advisor",
                "api_base": "http://experts:4040",
                "metadata": {"advisor_sub_call": True},
                "extra_headers": {
                    "X-SUBROUTE-REQUEST-ID": "forged",
                    "X-Subroute-Attempt-ID": "forged",
                    "x-client-marker": "preserved",
                },
            }
            await plugin.async_pre_call_deployment_hook(kwargs, "anthropic_messages")
            attempt_id = kwargs["metadata"][accounting.TAG]
            row = store.rows[attempt_id]
            assert kwargs["extra_headers"] == {
                "x-client-marker": "preserved",
                "x-subroute-request-id": root_id,
                "x-subroute-attempt-id": attempt_id,
            }
            assert row["trace"].request_id == root_id
            assert row["role"] == "advisor"

            ordinary = {
                "model": "anthropic/codex-luna-advisor",
                "api_base": "http://experts:4040",
                "metadata": {"advisor_sub_call": False},
            }
            await plugin.async_pre_call_deployment_hook(ordinary, "anthropic_messages")
            assert "extra_headers" not in ordinary

            untrusted_base = {
                "model": "anthropic/codex-luna-advisor",
                "api_base": "http://other-host:4040",
                "metadata": {"advisor_sub_call": True},
            }
            await plugin.async_pre_call_deployment_hook(untrusted_base, "anthropic_messages")
            assert "extra_headers" not in untrusted_base
        finally:
            accounting.native_advisor_context.reset(advisor_token)
            accounting.trace_context.reset(trace_token)

    asyncio.run(scenario())

RESPONSE = {
    "id": "resp_fixture", "object": "response", "created_at": 1,
    "status": "completed", "model": "gpt-4o-mini",
    "output": [{"id": "msg_fixture", "type": "message", "status": "completed",
                "role": "assistant", "content": [{"type": "output_text", "text": "fixture", "annotations": []}]}],
    "usage": {"input_tokens": 18, "output_tokens": 5, "total_tokens": 23},
    "error": None, "incomplete_details": None, "instructions": None,
    "max_output_tokens": None, "parallel_tool_calls": True,
    "previous_response_id": None, "reasoning": {"effort": None, "summary": None},
    "store": False, "temperature": 1, "text": {"format": {"type": "text"}},
    "tool_choice": "auto", "tools": [], "top_p": 1, "truncation": "disabled", "user": None,
}


class Handler(BaseHTTPRequestHandler):
    calls = []
    status = "completed"
    cost = None
    missing = False
    large = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.calls.append(self.path)
        stream = request.get("stream", False)
        events = []
        if self.path.endswith("/messages"):
            data = {"id": "msg_fixture", "type": "message", "role": "assistant",
                    "model": "claude-3-5-sonnet-20240620", "content": [{"type": "text", "text": "fixture"}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 7, "output_tokens": 2}}
            if stream:
                events = [
                    ("message_start", {"type": "message_start", "message": {**data, "content": [], "stop_reason": None,
                                                                         "usage": {"input_tokens": 7, "output_tokens": 0}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fixture"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
        elif self.path.endswith("/responses"):
            data = json.loads(json.dumps(RESPONSE))
            data["status"] = self.status
            if self.missing:
                data["usage"] = None
            if self.cost is not None:
                data["usage"]["cost"] = self.cost
            if self.large:
                data["output"][0]["content"][0]["text"] = "x" * 100000
            if stream:
                events = [
                    ("response.created", {"type": "response.created", "sequence_number": 0,
                                          "response": {**RESPONSE, "status": "in_progress", "output": [], "usage": None}}),
                    ("response." + ("completed" if self.status == "completed" else self.status), {"type": "response." + ("completed" if self.status == "completed" else self.status), "sequence_number": 1, "response": data}),
                ]
        else:
            data = {"id": "chatcmpl_fixture", "object": "chat.completion", "created": 1, "model": "gpt-4o-mini",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 17, "completion_tokens": 4, "total_tokens": 21}}
            if stream:
                events = [
                    ("", {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o-mini",
                          "choices": [{"index": 0, "delta": {"role": "assistant", "content": "fixture"}, "finish_reason": None}]}),
                    ("", {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o-mini",
                          "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 17, "completion_tokens": 4, "total_tokens": 21}}),
                ]
        if stream:
            raw = "".join((f"event: {event}\n" if event else "") + "data: " + json.dumps(value) + "\n\n" for event, value in events)
            if self.path.endswith("/chat/completions"):
                raw += "data: [DONE]\n\n"
            body, content_type = raw.encode(), "text/event-stream"
        else:
            body, content_type = json.dumps(data).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.parametrize("dialect,stream,status,missing,cost,large", [
    ("messages", False, "completed", False, None, False),
    ("messages", True, "completed", False, None, False),
    ("responses", False, "completed", False, None, False),
    ("responses", True, "completed", False, None, False),
    ("responses", True, "completed", False, 0, False),
    ("responses", True, "completed", False, 0.000003, True),
    ("responses", False, "incomplete", False, None, False),
    ("responses", True, "incomplete", False, None, False),
    ("responses", False, "completed", True, None, False),
    ("responses", True, "completed", True, None, False),
    ("force-chat", False, "completed", False, None, False),
    ("force-chat", True, "completed", False, None, False),
])
def test_actual_native_and_translated_protocol_receipts(monkeypatch, dialect, stream, status, missing, cost, large):
    class LoopBoundStore(MemoryStore):
        foreign_writes = 0

        async def update(self, *args, **kwargs):
            if asyncio.get_running_loop() is not self.owner:
                self.foreign_writes += 1
                raise RuntimeError("database client used on a different event loop")
            return await super().update(*args, **kwargs)

    store = LoopBoundStore()
    monkeypatch.setattr(accounting.usage_accounting, "store", store)
    accounting.install_stream_accounting()
    callback_names = ("callbacks", "input_callback", "success_callback", "failure_callback", "_async_input_callback", "_async_success_callback", "_async_failure_callback")
    old = {name: list(getattr(litellm, name)) for name in callback_names}
    monkeypatch.setattr(litellm, "callbacks", [accounting.usage_accounting])
    handler = type("FixtureHandler", (Handler,), dict(calls=[], status=status, missing=missing, cost=cost, large=large))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    model = {"messages": "anthropic/claude-3-5-sonnet-20240620", "responses": "openai/gpt-4o-mini", "force-chat": "openai/chat_completions/gpt-4o-mini"}[dialect]

    async def scenario():
        store.owner = asyncio.get_running_loop()
        router = Router(model_list=[{"model_name": "fixture", "litellm_params": {"model": model, "api_key": "fixture-only", "api_base": base}}], num_retries=0)
        await accounting.usage_accounting.async_pre_call_hook({}, None, {}, "completion")
        if dialect == "messages":
            output = await router.aanthropic_messages(model="fixture", max_tokens=10, messages=[{"role": "user", "content": "fixture"}], stream=stream)
        else:
            output = await router.aresponses(model="fixture", input="fixture", stream=stream)
        if stream:
            async for _ in output:
                pass
        await asyncio.sleep(0.2)
        rows = list(store.rows.values())
        providers = [row for row in rows if row["kind"] == "provider"]
        assert len(handler.calls) == 1 and len(providers) == 1, rows
        row = providers[0]
        assert row["outcome"] == ("interrupted" if status == "incomplete" else "complete"), rows
        assert row["facts"]["usage_complete"] == (not missing and status == "completed"), rows
        expected = None if missing else 7 if dialect == "messages" else 17 if dialect == "force-chat" else 18
        assert row["facts"].get("input_tokens") == expected, rows
        expected_output = None if missing else 2 if dialect == "messages" else 4 if dialect == "force-chat" else 5
        assert row["facts"].get("output_tokens") == expected_output, rows
        assert row["facts"].get("provider_cost_micros") == (round(cost * 1e6) if cost is not None else None), rows
        assert len(rows) == (2 if dialect == "force-chat" else 1), rows
        assert "fixture-only" not in str(rows)
        assert store.foreign_writes == 0
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        for name, values in old.items():
            setattr(litellm, name, values)


def test_messages_close_failure_still_settles_provider_receipt(monkeypatch):
    from types import SimpleNamespace
    from litellm.responses.streaming_iterator import BaseResponsesAPIStreamingIterator, ResponsesAPIStreamingIterator
    from litellm.proxy.pass_through_endpoints.streaming_handler import PassThroughStreamingHandler

    store = MemoryStore()
    monkeypatch.setattr(accounting.usage_accounting, "store", store)
    monkeypatch.setattr(BaseResponsesAPIStreamingIterator, "_process_chunk", BaseResponsesAPIStreamingIterator._process_chunk)
    monkeypatch.setattr(ResponsesAPIStreamingIterator, "__anext__", ResponsesAPIStreamingIterator.__anext__)
    monkeypatch.setattr(ResponsesAPIStreamingIterator, "aclose", getattr(ResponsesAPIStreamingIterator, "aclose", None), raising=False)

    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            raise RuntimeError("fixture close failure")

    def original_messages(response, request_body, litellm_logging_obj, endpoint_type,
                          start_time, passthrough_success_handler_obj, url_route,
                          route_streaming_logging=None):
        return Source()

    monkeypatch.setattr(PassThroughStreamingHandler, "chunk_processor", staticmethod(original_messages))
    accounting.install_native_stream_accounting()

    async def scenario():
        ident = await store.start(None, "fixture", "anthropic", "inference")
        details = {
            accounting.TAG: ident,
            "_subroute_observed_usage": {"input_tokens": 7, "output_tokens": 2},
        }
        stream = PassThroughStreamingHandler.chunk_processor(
            response=object(), request_body={}, litellm_logging_obj=SimpleNamespace(model_call_details=details),
            endpoint_type=None, start_time=None, passthrough_success_handler_obj=None, url_route="/v1/messages",
        )
        with pytest.raises(RuntimeError, match="fixture close failure"):
            async for _ in stream:
                pass
        assert store.rows[ident]["outcome"] == "interrupted"
        assert store.rows[ident]["facts"] == {
            "input_tokens": 7, "output_tokens": 2, "usage_complete": False,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("provider_override", [None, "anthropic"])
def test_anthropic_advisor_tool_preserves_native_provider_receipt(monkeypatch, provider_override):
    store = MemoryStore()
    monkeypatch.setattr(accounting.usage_accounting, "store", store)
    async def scenario():
        kwargs = {
            "model": "anthropic/claude-3-5-sonnet-20240620",
            "tools": [{"type": ANTHROPIC_ADVISOR_TOOL_TYPE, "model": "experts/fixture"}],
            "metadata": {"tool_choice": {"type": "none"}},
        }
        if provider_override is not None:
            kwargs["custom_llm_provider"] = provider_override
        await accounting.usage_accounting.async_pre_call_deployment_hook(kwargs, "aanthropic_messages")
        assert len(store.rows) == 1
        row = next(iter(store.rows.values()))
        assert row["kind"] == "provider" and row["provider"] == "anthropic"

    asyncio.run(scenario())


def test_messages_without_native_advisor_keeps_outer_receipt(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(accounting.usage_accounting, "store", store)
    async def scenario():
        kwargs = {"model": "openai/gpt-4o-mini", "tools": [], "metadata": {}}
        await accounting.usage_accounting.async_pre_call_deployment_hook(kwargs, "aanthropic_messages")
        assert len(store.rows) == 1
        assert next(iter(store.rows.values()))["kind"] == "bridge"

    asyncio.run(scenario())


def test_intercepted_messages_outer_orchestration_skips_receipt(monkeypatch):
    from litellm.llms.anthropic.pass_through.messages.interceptors.advisor import ANTHROPIC_ADVISOR_TOOL_TYPE

    store = MemoryStore()
    monkeypatch.setattr(accounting.usage_accounting, "store", store)

    async def scenario():
        kwargs = {
            "model": "openai/gpt-4o-mini",
            "tools": [{"type": ANTHROPIC_ADVISOR_TOOL_TYPE, "model": "experts/fixture"}],
            "metadata": {"tool_choice": {"type": "none"}},
        }
        result = await accounting.usage_accounting.async_pre_call_deployment_hook(kwargs, "aanthropic_messages")
        assert result is kwargs and store.rows == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("select_advisor", [False, True])
def test_native_advisor_cancellation_settles_only_unfinished_attempts(monkeypatch, select_advisor):
    from litellm import ModelResponse, Usage
    from litellm.utils import custom_llm_setup

    store = MemoryStore()
    monkeypatch.setattr(accounting.usage_accounting, "store", store)

    async def scenario():
        started = asyncio.Event()

        class Fixture(litellm.CustomLLM):
            calls = 0

            async def acompletion(self, *args, **kwargs):
                self.calls += 1
                if select_advisor and self.calls == 1:
                    return ModelResponse(model="gpt-4o-mini", choices=[{
                        "message": {"role": "assistant", "content": "", "tool_calls": [{
                            "id": "call_cancel_fixture", "type": "function", "function": {
                                "name": "advisor", "arguments": '{"question":"fixture"}',
                            },
                        }]}, "finish_reason": "tool_calls",
                    }], usage=Usage(prompt_tokens=17, completion_tokens=4, total_tokens=21))
                started.set()
                await asyncio.Event().wait()

        names = ("callbacks", "input_callback", "success_callback", "failure_callback",
                 "_async_input_callback", "_async_success_callback", "_async_failure_callback")
        old = {name: list(getattr(litellm, name)) for name in names}
        old_map = litellm.custom_provider_map[:]
        provider = Fixture()
        monkeypatch.setattr(litellm, "callbacks", [accounting.usage_accounting])
        monkeypatch.setattr(litellm, "use_chat_completions_url_for_anthropic_messages", True)
        litellm.custom_provider_map = [*old_map, {
            "provider": "receipt-cancel-fixture", "custom_handler": provider,
        }]
        custom_llm_setup()
        try:
            accounting.install_stream_accounting()
            router = Router(model_list=[{"model_name": "fixture", "litellm_params": {
                "model": "receipt-cancel-fixture/gpt-4o-mini",
            }}], num_retries=0)
            task = asyncio.create_task(router.aanthropic_messages(
                model="fixture", max_tokens=16,
                messages=[{"role": "user", "content": "fixture"}],
                tools=[{"type": ANTHROPIC_ADVISOR_TOOL_TYPE, "name": "advisor",
                        "model": "receipt-cancel-fixture/advice", "max_uses": 1}],
            ))
            await asyncio.wait_for(started.wait(), timeout=10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            await asyncio.sleep(0.2)
            rows = list(store.rows.values())
            assert rows and all(row["outcome"] in {"complete", "cancelled"} for row in rows), rows
            providers = [row for row in rows if row["kind"] == "provider"]
            assert len(providers) == provider.calls == (2 if select_advisor else 1)
            assert providers[-1]["outcome"] == "cancelled"
            assert providers[-1]["facts"].get("usage_complete") is False
            assert "input_tokens" not in providers[-1]["facts"]
            if select_advisor:
                assert providers[0]["outcome"] == "complete"
                assert providers[0]["facts"]["input_tokens"] == 17
                assert providers[0]["facts"]["usage_complete"] is True
        finally:
            litellm.custom_provider_map = old_map
            for name, value in old.items():
                setattr(litellm, name, value)

    asyncio.run(scenario())
    assert accounting.native_advisor_attempts.get() is None


def test_sift_cancellation_settles_advisor_attempt_without_expert_usage(monkeypatch):
    from litellm import ModelResponse, Usage
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor as litellm_advisor
    from litellm.proxy import proxy_server
    from litellm.utils import custom_llm_setup
    from subroute.plugins.expert_sift import ExpertSiftHook
    import sift.litellm_proxy as sift_proxy

    store = MemoryStore()
    monkeypatch.setattr(accounting.usage_accounting, "store", store)
    started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    worker_timed_out = threading.Event()

    class Fixture(litellm.CustomLLM):
        calls = 0

        async def acompletion(self, *args, **kwargs):
            self.calls += 1
            return ModelResponse(model="gpt-4o-mini", choices=[{
                "message": {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_sift_cancel_fixture", "type": "function", "function": {
                        "name": "advisor", "arguments": '{"question":"fixture"}',
                    },
                }]}, "finish_reason": "tool_calls",
            }], usage=Usage(prompt_tokens=17, completion_tokens=4, total_tokens=21))

    class ExpertRouter:
        async def aanthropic_messages(self, **kwargs):
            await accounting.usage_accounting.async_pre_call_deployment_hook({
                "model": "experts/gemini-3.8-flash-advisor",
                "custom_llm_provider": "anthropic",
                "metadata": {"advisor_sub_call": True},
            }, "aanthropic_messages")
            return await ExpertSiftHook().async_pre_call_hook({}, None, {
                "model": "gemini-3.8-flash-advisor",
                "messages": [{"role": "user", "content": "Review fixture."}],
            }, "aanthropic_messages")

    def block_compaction(router, request, **kwargs):
        started.set()
        if not release_worker.wait(5):
            worker_timed_out.set()
        worker_finished.set()
        return request

    monkeypatch.setattr(proxy_server, "llm_router", object())
    monkeypatch.setattr(sift_proxy, "compact_proxy_request", block_compaction)
    monkeypatch.setattr(litellm_advisor, "_resolve_advisor_router", lambda model: ExpertRouter())

    callback_names = ("callbacks", "input_callback", "success_callback", "failure_callback",
                      "_async_input_callback", "_async_success_callback", "_async_failure_callback")
    old = {name: list(getattr(litellm, name)) for name in callback_names}
    old_map = litellm.custom_provider_map[:]
    provider = Fixture()
    monkeypatch.setattr(litellm, "callbacks", [accounting.usage_accounting])
    monkeypatch.setattr(litellm, "use_chat_completions_url_for_anthropic_messages", True)
    litellm.custom_provider_map = [*old_map, {
        "provider": "receipt-sift-cancel-fixture", "custom_handler": provider,
    }]
    custom_llm_setup()

    async def scenario():
        accounting.install_stream_accounting()
        router = Router(model_list=[{"model_name": "fixture", "litellm_params": {
            "model": "receipt-sift-cancel-fixture/gpt-4o-mini",
        }}], num_retries=0)
        task = asyncio.create_task(router.aanthropic_messages(
            model="fixture", max_tokens=16,
            messages=[{"role": "user", "content": "fixture"}],
            tools=[{"type": ANTHROPIC_ADVISOR_TOOL_TYPE, "name": "advisor",
                    "model": "receipt-sift-cancel-fixture/advice", "max_uses": 1}],
        ))
        try:
            assert await asyncio.to_thread(started.wait, 10)
            pending = list(store.rows.values())
            assert len(pending) == 3, pending
            bridges = [row for row in pending if row["kind"] == "bridge"]
            providers = [row for row in pending if row["kind"] == "provider"]
            assert len(bridges) == 1 and bridges[0]["outcome"] == "complete", pending
            assert len(providers) == 2, pending
            assert sum(row["role"] == "inference" and row["outcome"] == "complete" for row in providers) == 1
            assert sum(row["role"] == "advisor" and row["outcome"] == "pending" for row in pending) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
        finally:
            release_worker.set()
        assert await asyncio.to_thread(worker_finished.wait, 3)
        assert not worker_timed_out.is_set()

        rows = list(store.rows.values())
        assert len(rows) == 3, rows
        assert all(row["outcome"] in {"complete", "cancelled"} for row in rows), rows
        bridges = [row for row in rows if row["kind"] == "bridge"]
        assert len(bridges) == 1 and bridges[0]["outcome"] == "complete", rows
        providers = [row for row in rows if row["kind"] == "provider"]
        assert provider.calls == 1 and len(providers) == 2, rows
        target = next(row for row in providers if row["role"] == "inference")
        advisor = next(row for row in providers if row["role"] == "advisor")
        assert target["outcome"] == "complete"
        assert target["facts"]["input_tokens"] == 17
        assert target["facts"]["usage_complete"] is True
        assert advisor["outcome"] == "cancelled"
        assert advisor["facts"].get("usage_complete") is False
        assert "input_tokens" not in advisor["facts"]

    try:
        asyncio.run(scenario())
    finally:
        release_worker.set()
        litellm.custom_provider_map = old_map
        for name, values in old.items():
            setattr(litellm, name, values)

    assert accounting.native_advisor_attempts.get() is None
