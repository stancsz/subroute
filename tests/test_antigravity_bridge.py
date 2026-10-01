import importlib.util
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from http.server import ThreadingHTTPServer

from subroute.handlers.antigravity import BRIDGE_MAX_REQUEST_BYTES


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "sidecars" / "antigravity" / "bridge.py"
SPEC = importlib.util.spec_from_file_location("antigravity_bridge", BRIDGE_PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def test_response_text_requires_provider_content():
    payload = '\n'.join([
        '{"event":"step_update","step_update":{"text_delta":"partial"}}',
        '{"event":"result","result":{"status":"SUCCESS","response":"complete"}}',
    ])

    assert bridge._response_text(payload) == "complete"


@pytest.mark.parametrize("content", ["", " \n\t"])
def test_whitespace_is_not_a_completed_provider_answer(content):
    payload = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": content}})
    with pytest.raises(RuntimeError, match="no response text"):
        bridge._response_text(payload)


def test_quoting_a_filter_message_in_an_answer_is_not_a_refusal():
    content = "The CLI diagnostic is: This request was blocked by Gemini's filters."
    payload = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": content}})
    assert bridge._response_text(payload) == content


def test_audio_refusal_keeps_exact_completed_attachment_count():
    paths = ["/tmp/a.wav", "/tmp/b.wav"]
    stdout = "\n".join([
        json.dumps({"event": "step_update", "step_update": {
            "step_type": "tool", "tool_name": "view_file", "state": "DONE",
            "tool_info": {"parameters": {"AbsolutePath": paths[0]}},
        }}),
        json.dumps({"event": "result", "result": {
            "status": "SUCCESS", "response": "This request was blocked by Gemini's filters.",
            "conversation_id": "fixture",
        }}),
    ])
    with pytest.raises(bridge.ProviderRefused) as caught:
        bridge._audio_reads(stdout, paths)
    assert caught.value.completed_reads == 1
    assert caught.value.phase == "after_attachment_reads"


@pytest.mark.parametrize("options", [{}, {"advisor": True}, {"json_schema": {"type": "object"}}])
def test_captured_provider_refusal_is_502_for_every_target_mode(monkeypatch, capsys, options):
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    stdout = json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "conversation_id": "refusal-fixture", "usage": usage,
        "response": " \nThis request was blocked by Gemini's filters. Please rephrase.",
    }})
    server, thread, url = _start_bridge_server(monkeypatch, lambda *args, **kwargs: [])
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([], 0, stdout, "")
    monkeypatch.setattr(bridge, "_run_agy", run)
    try:
        response = httpx.post(url + "/v1/completions", json={"model": "fixture", "prompt": "hello", **options})
        assert response.status_code == 502
        error = response.json()
        assert error["code"] == "provider_content_filter"
        assert error["phase"] == "generation"
        assert error["conversation_id"] == "refusal-fixture"
        assert error["provider_usage"] == usage
        assert len(calls) == 1
        assert "completion succeeded" not in capsys.readouterr().err
        assert bridge._CLI_SLOTS.acquire(timeout=0.1)
        bridge._CLI_SLOTS.release()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_response_text_rejects_failed_agent_turn_with_reason():
    payload = '{"event":"result","result":{"status":"ERROR","response":"","error":"permission check failed"}}'

    with pytest.raises(RuntimeError, match="status ERROR: permission check failed"):
        bridge._response_text(payload)


def test_response_text_rejects_missing_terminal_event_even_with_partial_text():
    payload = '{"event":"step_update","step_update":{"text_delta":"partial"}}'

    with pytest.raises(RuntimeError, match="no terminal result event"):
        bridge._response_text(payload)


@pytest.mark.parametrize("raw_usage", [None, {}, {"input_tokens": True}])
def test_invalid_provider_usage_is_502_and_never_logged_as_success(monkeypatch, capsys, raw_usage):
    stdout = json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "response": "answer", "usage": raw_usage,
    }})
    server, thread, base_url = _start_bridge_server(monkeypatch, lambda *args, **kwargs: [])
    monkeypatch.setattr(bridge, "_run_agy", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout, ""))
    try:
        response = httpx.post(f"{base_url}/v1/completions", json={"model": "fixture", "prompt": "hello"})
        assert response.status_code == 502
        assert "completion succeeded" not in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_duplicate_terminal_results_cannot_mix_text_and_usage():
    result = {"event": "result", "result": {"status": "SUCCESS", "response": "first"}}
    payload = json.dumps(result) + "\n" + json.dumps(result)
    with pytest.raises(RuntimeError, match="multiple terminal"):
        bridge._response_text(payload)


def test_plain_target_uses_target_agent_and_advisor_uses_advisor_agent():
    target = bridge._agy_command("Gemini Test")
    advisor = bridge._agy_command("Gemini Test", advisor=True)
    assert target[target.index("--agent") + 1] == "subroute-target"
    assert advisor[advisor.index("--agent") + 1] == "subroute-advisor"


def test_agy_command_selects_sandboxed_target_agent_and_uses_stdin():
    command = bridge._agy_command("Gemini 3.8 Flash (Medium)")

    assert command[command.index("--agent") + 1] == "subroute-target"
    assert "--sandbox" in command
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert "--print" not in command
    assert bridge._prompt_input("give advice") == (
        '{"event": "user", "message": {"content": "give advice"}}\n'
    )


def test_agy_command_selects_target_agent_for_schema_output():
    command = bridge._agy_command("Gemini 3.8 Flash (Low)", json_schema_path="/tmp/schema.json")

    assert command[command.index("--agent") + 1] == "subroute-target"
    assert command[command.index("--json-schema") + 1] == "/tmp/schema.json"
    assert "--sandbox" in command


def test_prompt_is_sent_on_stdin_instead_of_command_arguments():
    prompt = "large-context " * 20_000
    command = bridge._agy_command("Gemini 3.8 Flash (Low)")

    assert len(bridge._prompt_input(prompt)) > 128_000
    assert all(prompt not in argument for argument in command)


def test_gateway_preflight_limit_matches_sidecar_ingress_limit():
    assert BRIDGE_MAX_REQUEST_BYTES == bridge.MAX_REQUEST_BYTES


def test_provider_usage_requires_terminal_provider_counts():
    payload = '{"event":"result","result":{"status":"SUCCESS","usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15}}}'

    assert bridge._provider_usage(payload) == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
    }


def test_runtime_status_distinguishes_sign_in_from_bridge_health(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_run_agy",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="Fetching available models...",
            stderr="Error: Please sign in to view available models.",
        ),
    )

    assert bridge._runtime_status() == {
        "status": "ready",
        "authenticated": False,
        "models": [],
        "detail": "Docker bridge ready; Antigravity sign-in required",
    }


def test_runtime_status_reports_authenticated_model_inventory(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_run_agy",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="gemini-a\tGemini A\ngemini-b\tGemini B\n",
            stderr="",
        ),
    )

    assert bridge._runtime_status() == {
        "status": "ready",
        "authenticated": True,
        "models": ["gemini-a", "gemini-b"],
        "detail": "Authenticated; 2 Gemini subscription models available",
    }


def test_runtime_status_bounds_inventory_timeout(monkeypatch):
    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="agy models", timeout=15)

    monkeypatch.setattr(bridge, "_run_agy", time_out)

    assert bridge._runtime_status()["detail"] == "Model inventory timed out"


def test_runtime_status_skips_inventory_when_cli_capacity_is_full(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(bridge, "_CLI_SLOTS", slots)
    monkeypatch.setattr(bridge, "CLI_ADMISSION_WAIT_SECONDS", 0)
    monkeypatch.setattr(
        bridge, "_run_agy",
        lambda *args, **kwargs: pytest.fail("inventory must not exceed CLI capacity"),
    )

    try:
        assert bridge._runtime_status() == {
            "status": "ready", "authenticated": None, "models": [],
            "detail": "Subscription CLI at capacity; model inventory unavailable",
        }
    finally:
        slots.release()


def _start_bridge_server(monkeypatch, command_factory):
    monkeypatch.setattr(bridge, "_CLI_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(bridge, "CLI_ADMISSION_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(bridge, "_agy_command", command_factory)
    server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def _success_cli_command(marker_path: Path, delay: float) -> list[str]:
    script = (
        "import json,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text('started'); "
        "sys.stdin.read(); time.sleep(float(sys.argv[2])); "
        "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'ok',"
        "'usage':{'input_tokens':2,'output_tokens':1,'total_tokens':3}}}))"
    )
    return [sys.executable, "-c", script, str(marker_path), str(delay)]


def test_bridge_bounds_cli_concurrency_rejects_overload_and_recovers(monkeypatch, tmp_path):
    marker = tmp_path / "cli-started"
    server, thread, base_url = _start_bridge_server(
        monkeypatch, lambda model, json_schema_path=None: _success_cli_command(marker, 0.5)
    )
    payload = {"model": "Gemini Test", "prompt": "hello"}
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(httpx.post, f"{base_url}/v1/completions", json=payload, timeout=5)
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), "first request never entered the CLI slot"

            overloaded = httpx.post(f"{base_url}/v1/completions", json=payload, timeout=5)
            assert overloaded.status_code == 429
            assert "CLI capacity" in overloaded.json()["detail"]

            completed = first.result(timeout=5)
            assert completed.status_code == 200
            assert completed.json() == {
                "content": "ok",
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            }

        recovered = httpx.post(f"{base_url}/v1/completions", json=payload, timeout=5)
        assert recovered.status_code == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bridge_client_disconnect_kills_cli_and_releases_capacity(monkeypatch, tmp_path):
    marker = tmp_path / "cli-pid"
    script = (
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid())); "
        "sys.stdin.read(); time.sleep(30)"
    )
    captured_processes = []
    original_popen = bridge.subprocess.Popen

    def record_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        captured_processes.append(process)
        return process

    monkeypatch.setattr(bridge.subprocess, "Popen", record_process)
    monkeypatch.setattr(
        bridge, "_agy_command", lambda model, json_schema_path=None: [sys.executable, "-c", script, str(marker)]
    )
    server, thread, base_url = _start_bridge_server(
        monkeypatch, lambda model, json_schema_path=None: [sys.executable, "-c", script, str(marker)]
    )
    payload = json.dumps({"model": "Gemini Test", "prompt": "hello"}).encode()
    request = (
        f"POST /v1/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + payload
    try:
        client = socket.create_connection(("127.0.0.1", server.server_port), timeout=3)
        client.sendall(request)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists() and captured_processes, "request never started the CLI"
        client.close()

        deadline = time.monotonic() + 5
        while captured_processes[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert captured_processes[0].poll() is not None, "CLI survived client disconnect"

        monkeypatch.setattr(
            bridge, "_agy_command", lambda model, json_schema_path=None: _success_cli_command(marker, 0)
        )
        recovered = httpx.post(f"{base_url}/v1/completions", json={"model": "Gemini Test", "prompt": "again"}, timeout=5)
        assert recovered.status_code == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_start_failure_returns_bounded_error_and_releases_capacity(monkeypatch, tmp_path):
    missing = tmp_path / "missing-agy-executable"
    marker = tmp_path / "cli-started"
    server, thread, base_url = _start_bridge_server(
        monkeypatch, lambda model, json_schema_path=None: [str(missing)]
    )
    payload = {"model": "Gemini Test", "prompt": "hello"}
    try:
        failed = httpx.post(f"{base_url}/v1/completions", json=payload, timeout=5)
        assert failed.status_code == 502
        assert failed.json() == {"detail": "Unable to start Antigravity CLI"}

        monkeypatch.setattr(
            bridge, "_agy_command",
            lambda model, json_schema_path=None: _success_cli_command(marker, 0),
        )
        recovered = httpx.post(f"{base_url}/v1/completions", json=payload, timeout=5)
        assert recovered.status_code == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bridge_failure_echoes_and_logs_valid_request_id(monkeypatch, caplog):
    request_id = "a" * 32
    command = [
        sys.executable,
        "-c",
        "import json,sys; sys.stdin.read(); print(json.dumps({'event':'result','result':{"
        "'status':'ERROR','response':'','error':'upstream internal error'}}))",
    ]
    server, thread, base_url = _start_bridge_server(
        monkeypatch, lambda model, json_schema_path=None: command
    )
    try:
        with caplog.at_level(logging.WARNING, logger=bridge.__name__):
            response = httpx.post(
                f"{base_url}/v1/completions",
                json={"model": "Gemini Test", "prompt": "hello"},
                headers={"X-Subroute-Request-ID": request_id},
                timeout=5,
            )

        assert response.status_code == 502
        assert response.headers["X-Subroute-Request-ID"] == request_id
        assert f"request_id={request_id}" in caplog.text
        assert "upstream internal error" in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cli_timeout_kills_and_reaps_process(monkeypatch):
    captured = []
    original_popen = bridge.subprocess.Popen

    def record_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        captured.append(process)
        return process

    monkeypatch.setattr(bridge.subprocess, "Popen", record_process)
    command = [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"]

    with pytest.raises(subprocess.TimeoutExpired):
        bridge._run_agy(command, "prompt", timeout=0.2)

    assert len(captured) == 1
    assert captured[0].poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="Compose process-group cleanup requires POSIX")
def test_timeout_reaps_descendants_even_after_cli_parent_exits():
    command = [sys.executable, "-c", (
        "import subprocess,sys; sys.stdin.read(); "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])"
    )]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        bridge._run_agy(command, "prompt", timeout=0.3)
    assert time.monotonic() - started < 2, "orphan inherited pipes outlived the CLI deadline"
