"""Real Router, Chat serializer and ASGI cleanup with loopback provider fixtures."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import litellm
import pytest
from litellm import Router
from litellm.proxy import proxy_server as proxy
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_request_processing import _UpstreamClosingStreamingResponse
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.utils import ProxyLogging
from starlette.applications import Starlette
from starlette.routing import Route

from subroute import usage_accounting as accounting
from test_usage_accounting import MemoryStore


@pytest.mark.parametrize("mode,expected", [("normal", "complete"), ("eof", "interrupted"), ("disconnect", "cancelled")])
def test_actual_chat_serializer_cleanup_preserves_provider_outcome(monkeypatch, mode, expected):
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            type(self).calls += 1
            frames = [
                {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 1,
                 "model": "MiniMax-M3", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "fixture"}, "finish_reason": None}]},
                {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 1,
                 "model": "MiniMax-M3", "choices": [{"index": 0, "delta": {}, "finish_reason": None if mode == "eof" else "stop"}],
                 "usage": {"prompt_tokens": 17, "completion_tokens": 4, "total_tokens": 21}},
            ]
            encoded = [("data: " + json.dumps(frame) + "\n\n").encode() for frame in frames]
            tail = b"data: [DONE]\n\n" if mode != "eof" else b""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(sum(map(len, encoded)) + len(tail)))
            self.end_headers()
            self.wfile.write(encoded[0])
            self.wfile.flush()
            if mode == "disconnect":
                time.sleep(1)
            try:
                self.wfile.write(encoded[1] + tail)
            except (BrokenPipeError, ConnectionResetError):
                pass

    store = MemoryStore()
    monkeypatch.setattr(accounting.usage_accounting, "store", store)
    accounting.install_stream_accounting()
    names = ("callbacks", "input_callback", "success_callback", "failure_callback", "_async_input_callback", "_async_success_callback", "_async_failure_callback")
    old = {name: list(getattr(litellm, name)) for name in names}
    monkeypatch.setattr(litellm, "callbacks", [accounting.usage_accounting])
    monkeypatch.setattr(proxy, "proxy_logging_obj", ProxyLogging(UserApiKeyCache()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        router = Router(model_list=[{"model_name": "fixture", "litellm_params": {
            "model": "minimax/MiniMax-M3", "api_key": "fixture-only", "api_base": f"http://127.0.0.1:{server.server_port}/v1"}}], num_retries=0)
        monkeypatch.setattr(proxy, "llm_router", router)

        async def endpoint(request):
            await accounting.usage_accounting.async_pre_call_hook({}, None, {}, "completion")
            output = await router.acompletion(model="fixture", messages=[{"role": "user", "content": "fixture"}],
                                            stream=True, stream_options={"include_usage": True})
            generator = proxy.select_data_generator(output, UserAPIKeyAuth(), {"model": "fixture"}, request)
            return _UpstreamClosingStreamingResponse(generator, media_type="text/event-stream", upstream_generator=generator)

        app = Starlette(routes=[Route("/v1/chat/completions", endpoint, methods=["POST"])])
        if mode == "disconnect":
            disconnected, sent, read = asyncio.Event(), [], False

            async def receive():
                nonlocal read
                if not read:
                    read = True
                    return {"type": "http.request", "body": b"{}", "more_body": False}
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                sent.append(message["type"])
                if message["type"] == "http.response.body" and message.get("body"):
                    disconnected.set()

            await asyncio.wait_for(app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
                "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions", "query_string": b"", "root_path": "",
                "headers": [(b"content-type", b"application/json")], "server": ("fixture", 80), "client": ("fixture", 1)}, receive, send), 6)
            assert disconnected.is_set() and sent.count("http.response.body") == 1
        else:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
                response = await client.post("/v1/chat/completions", json={"fixture": True})
                assert response.status_code == 200
                if mode == "normal":
                    assert response.text.rstrip().endswith("data: [DONE]")
        # Let real queued framework success callbacks race with close. They
        # must not relabel EOF complete or overwrite provider completion.
        await asyncio.sleep(0.4)
        rows = list(store.rows.values())
        assert Handler.calls == 1 and len(rows) == 1
        row = rows[0]
        assert row["outcome"] == expected, rows
        assert row["facts"]["usage_complete"] is (mode == "normal"), rows
        assert row["facts"].get("input_tokens") == (None if mode == "disconnect" else 17), rows
        assert row["facts"].get("output_tokens") == (None if mode == "disconnect" else 4), rows
        assert "provider_cost_micros" not in row["facts"]
        assert "fixture-only" not in str(rows)

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        for name, value in old.items():
            setattr(litellm, name, value)
