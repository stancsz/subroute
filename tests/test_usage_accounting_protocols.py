"""Installed LiteLLM protocols with isolated loopback HTTP provider fixtures."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import litellm
import pytest
from litellm import Router
from subroute import usage_accounting as accounting
from test_usage_accounting import MemoryStore

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
