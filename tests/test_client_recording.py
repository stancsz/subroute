"""The live observer must preserve metadata failures, not just model POSTs."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest
import socket

from test_live_coding_clients import WireRecorder, assert_formatted_answer


def test_wire_recorder_captures_metadata_success_and_failure_without_headers():
    class Metadata(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200 if self.path == "/v1/models" else 404)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[]}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Metadata)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with WireRecorder(f"http://127.0.0.1:{server.server_port}") as recorder:
            for path, status in [("/v1/models", 200), ("/missing", 404)]:
                result = httpx.get(recorder.base_url + path, headers={"Authorization": "Bearer test-secret"})
                assert result.status_code == status
        assert [(r["method"], r["path"], r["status"]) for r in recorder.records] == [
            ("GET", "/v1/models", 200), ("GET", "/missing", 404)]
        assert all(r["body"] == '{"data":[]}' for r in recorder.records)
        assert "test-secret" not in str(recorder.records)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_client_tool_allowlist_rejects_unsafe_setup_before_upstream_dispatch():
    # An unreachable upstream makes any accidental forward fail this check.
    with WireRecorder("http://127.0.0.1:1", allowed_tools={"read_files"}) as recorder:
        response = httpx.post(recorder.base_url + "/v1/chat/completions", json={
            "model": "current", "tools": [{"type": "function", "function": {"name": "run_commands"}}]})
    assert response.status_code == 400
    assert recorder.records[0]["status"] == 400
    assert "read-only allowlist" in response.json()["error"]["message"]


@pytest.mark.parametrize("address", [("1.1.1.1", 443), ("127.0.0.1", 4005)])
def test_unit_network_guard_blocks_cloud_and_running_gateway(address):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(pytest.fail.Exception, match="live network connection"):
            sock.connect(address)


@pytest.mark.parametrize("answer", [
    'FORMAT_SUM: 42\n```python\nx=r"\\n"\n```\x00',
    'FORMAT_SUM: 42\n```python\nx=r"\\n"',
    'FORMAT_SUM: 42\nFORMAT_SUM: 42\n```python\nx=r"\\n"\n```',
    '<think>plan</think>\nFORMAT_SUM: 42\n```python\nx=r"\\n"\n```',
    'START_FOLLOW_CONTRACT\nFORMAT_SUM: 42\n```python\nx=r"\\n"\n```\nEND_FOLLOW_CONTRACT',
])
def test_client_acceptance_rejects_broken_display(answer):
    with pytest.raises(AssertionError):
        assert_formatted_answer(answer)
