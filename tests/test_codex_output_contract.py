"""Caller output ceilings must survive the real LiteLLM HTTP conversion."""

import json
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import litellm
import pytest

from test_codex_proxy_http import _configured_proxy


@contextmanager
def responses_server():
    requests = []
    message = {"id": "msg_caps", "type": "message", "status": "completed",
               "role": "assistant", "content": [{"type": "output_text", "text": "CAP_OK", "annotations": []}]}
    completed = {"id": "resp_caps", "object": "response", "created_at": 1,
                 "model": "gpt-6-luna", "status": "completed", "output": [message],
                 "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}}
    events = [
        {"type": "response.created", "response": {**completed, "status": "in_progress", "output": [], "usage": None}},
        {"type": "response.output_text.delta", "item_id": "msg_caps", "output_index": 0,
         "content_index": 0, "delta": "CAP_OK"},
        {"type": "response.completed", "response": completed},
    ]
    wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("protocol,limits,cap", [
    ("chat", {"max_tokens": 33}, 33),
    ("chat", {"max_completion_tokens": 16}, 16),
    ("chat", {"max_tokens": 48, "max_completion_tokens": 32}, 32),
    ("messages", {"max_tokens": 41}, 41),
    ("responses", {"max_output_tokens": 37}, 37),
])
@pytest.mark.parametrize("stream", [False, True])
def test_output_limit_reaches_native_responses_wire(protocol, limits, cap, stream):
    original_completion = litellm.acompletion
    payload = {"model": "codex-luna", "stream": stream}
    if protocol == "responses":
        path = "/v1/responses"
        payload.update(input="Reply briefly", store=False)
    else:
        path = "/v1/messages" if protocol == "messages" else "/v1/chat/completions"
        payload.update(messages=[{"role": "user", "content": "Reply briefly"}])
    payload.update(limits)
    unchanged = deepcopy(payload)
    with responses_server() as (base, requests):
        with _configured_proxy(api_base=base) as (client, *_):
            # The shared proxy helper stubs acompletion. Restore native dispatch
            # here so the assertion covers the serialized provider HTTP body.
            with patch.object(litellm, "acompletion", original_completion):
                response = client.post(path, json=payload, headers={"anthropic-version": "2023-06-01"})
    assert response.status_code == 200, response.text
    assert "CAP_OK" in response.text
    if stream:
        terminal = {"chat": "[DONE]", "messages": "message_stop", "responses": "response.completed"}
        assert terminal[protocol] in response.text
    else:
        body = response.json()
        if protocol == "chat":
            assert body["choices"][0]["finish_reason"] == "stop"
        elif protocol == "messages":
            assert body["stop_reason"] == "end_turn"
            assert body["usage"] == {"input_tokens": 5, "output_tokens": 3}
        else:
            assert body["status"] == "completed"
    assert len(requests) == 1
    request_path, body = requests[0]
    assert request_path == "/v1/responses"
    assert body["max_output_tokens"] == cap
    assert "max_tokens" not in body and "max_completion_tokens" not in body
    assert body["store"] is False
    assert payload == unchanged


@pytest.mark.parametrize("protocol", ["chat", "messages", "responses"])
def test_unrepresentable_output_limit_rejects_before_provider(protocol):
    original_completion = litellm.acompletion
    payload = {"model": "codex-luna"}
    if protocol == "responses":
        path = "/v1/responses"
        payload.update(input="Reply briefly", max_output_tokens=8)
    else:
        path = "/v1/messages" if protocol == "messages" else "/v1/chat/completions"
        payload.update(messages=[{"role": "user", "content": "Reply briefly"}], max_tokens=8)
    with responses_server() as (base, requests):
        with _configured_proxy(api_base=base) as (client, *_):
            with patch.object(litellm, "acompletion", original_completion):
                response = client.post(path, json=payload)
    assert response.status_code == 400, response.text
    assert requests == []
