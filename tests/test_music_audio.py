"""Regression boundaries for real audio transport, rather than text-only success."""

import asyncio
import base64
import io
import json
from pathlib import Path
import subprocess
import wave

import httpx
import pytest
from fastapi import HTTPException

from subroute.audio import audio_route, decode_audio
from subroute.handlers import antigravity
from subroute.music_mcp import create_server, read_asset
from subroute.plugins.dynamic_router import DynamicRoutingPlugin
from subroute.plugins.advisor_plugin import AdvisorPlugin
from test_dynamic_routing import make_control_plane
from test_antigravity_bridge import bridge, _start_bridge_server


def sample():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x01\x00\x02\x00" * 4800)
    return {"data": base64.b64encode(stream.getvalue()).decode(), "format": "wav"}


def request(audio=None):
    return {"model": "current", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Listen"}, {"type": "input_audio", "input_audio": audio or sample()},
    ]}]}


@pytest.mark.parametrize("mode", ["force", "alias", "off"])
def test_audio_is_explicit_gemini_exception_and_never_calls_saved_advisor(tmp_path, mode):
    control = make_control_plane(tmp_path)
    control.update("minimax", mode)
    before = control.snapshot()
    data = request()
    result = asyncio.run(DynamicRoutingPlugin(control).async_pre_call_hook({}, None, data, "acompletion"))
    assert result["model"] == "gemini-subscription"
    assert result["messages"] == request()["messages"]
    assert result["metadata"]["routing"]["mode"] == "audio_input"
    assert result["metadata"]["gateway_policy"]["advisor_model"] is None
    assert asyncio.run(AdvisorPlugin().async_pre_call_hook({}, None, result, "acompletion")) is None
    assert control.snapshot() == before


def test_messages_audio_cannot_be_dropped_before_provider_dispatch(tmp_path):
    data = request()
    data["messages"][0]["content"][1] = {"type": "audio", "source": {"type": "base64", "media_type": "audio/wav", "data": sample()["data"]}}
    with pytest.raises(HTTPException) as error:
        asyncio.run(DynamicRoutingPlugin(make_control_plane(tmp_path)).async_pre_call_hook({}, None, data, "anthropic_messages"))
    assert error.value.status_code == 400
    assert "chat/completions" in str(error.value.detail)


@pytest.mark.parametrize("audio", [
    {"data": "bad!", "format": "wav"}, {"data": "YWJj", "format": "wav"},
    {"data": "YWJj", "format": "flac"}, {"data": "YWJj", "format": "mp3"},
    {"data": "YWJj", "format": []},
])
def test_invalid_audio_fails_before_model_work(audio):
    with pytest.raises(ValueError):
        audio_route(request(audio), "acompletion")


def test_truncated_wav_and_too_many_attachments_rejected():
    audio = sample()
    audio["data"] = base64.b64encode(base64.b64decode(audio["data"])[:-10]).decode()
    with pytest.raises(ValueError, match="truncated"):
        decode_audio(audio)
    data = request()
    data["messages"][0]["content"] *= 3
    with pytest.raises(ValueError, match="at most two"):
        audio_route(data, "acompletion")


def test_responses_and_nested_audio_are_rejected_before_conversion():
    with pytest.raises(ValueError, match="Responses"):
        audio_route({"input": [{"type": "input_audio", "input_audio": sample()}]}, "aresponses")
    data = request()
    data["messages"][0]["content"] = [{"type": "tool_result", "content": data["messages"][0]["content"]}]
    with pytest.raises(ValueError, match="nested audio"):
        audio_route(data, "acompletion")


def test_handler_sends_audio_bytes_separately_and_preserves_text_order(monkeypatch):
    async def invoke(model, prompt, **kwargs):
        assert kwargs["attachments"] == [sample()]
        assert prompt.index("Listen") < prompt.index("Audio attachment 1")
        return "heard", antigravity.Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12)
    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    result = asyncio.run(antigravity.antigravity_handler.acompletion(model="gemini-3.8-flash", messages=request()["messages"]))
    assert result.choices[0].message.content == "heard"
    assert result.usage.total_tokens == 12


@pytest.mark.parametrize("outcome", ["complete", "missing", "unexpected", "timeout", "disconnect", "refused"])
def test_bridge_reads_exact_bytes_and_cleans_temporary_audio_on_every_outcome(monkeypatch, tmp_path, outcome):
    original_directory = bridge.tempfile.TemporaryDirectory
    monkeypatch.setattr(bridge.tempfile, "TemporaryDirectory", lambda **kwargs: original_directory(**{**kwargs, "dir": str(tmp_path)}))
    workspaces = []
    server, thread, url = _start_bridge_server(monkeypatch, lambda *args, **kwargs: [])

    def run(command, prompt, **kwargs):
        cwd = Path(kwargs["cwd"])
        workspaces.append(cwd)
        path = cwd / "attachment-1.wav"
        assert path.read_bytes() == decode_audio(sample())
        assert "--dangerously-skip-permissions" not in command
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 0.1)
        if outcome == "disconnect":
            raise bridge.ClientDisconnected()
        events = []
        if outcome != "missing":
            events.append({"event": "step_update", "step_update": {
                "step_type": "tool", "tool_name": "view_file", "state": "DONE",
                "tool_info": {"parameters": {"AbsolutePath": str(path) if outcome != "unexpected" else "/root/unexpected"}},
            }})
        response_text = "This request was blocked by Gemini's filters. Please rephrase." if outcome == "refused" else "heard"
        events.append({"event": "result", "result": {"status": "SUCCESS", "response": response_text, "usage": {
            "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
        }}})
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")

    monkeypatch.setattr(bridge, "_run_agy", run)
    try:
        try:
            response = httpx.post(url + "/v1/completions", json={"model": "fixture", "prompt": "listen", "attachments": [sample()]})
        except httpx.RemoteProtocolError:
            assert outcome == "disconnect"
        else:
            assert response.status_code == {"complete": 200, "missing": 502, "unexpected": 502, "timeout": 504, "refused": 502}[outcome]
        assert workspaces and all(not cwd.exists() for cwd in workspaces)
        assert bridge._CLI_SLOTS.acquire(timeout=0.1)
        bridge._CLI_SLOTS.release()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mcp_root_escape_and_symlink_escape_are_rejected(tmp_path):
    root = tmp_path / "audio"
    root.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(decode_audio(sample()))
    with pytest.raises(ValueError, match="configured root"):
        read_asset(root, "../outside.wav")
    try:
        (root / "link.wav").symlink_to(outside)
    except OSError:
        return  # Windows may lack symlink privilege; Linux run covers it.
    with pytest.raises(ValueError, match="configured root"):
        read_asset(root, "link.wav")


def test_mcp_tool_sends_actual_binary_to_gateway_and_checks_terminal_result(tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    path.write_bytes(decode_audio(sample()))
    client_type = httpx.AsyncClient
    calls = []
    def transport(req):
        payload = json.loads(req.content)
        assert payload["model"] == "gemini-subscription"
        audio = payload["messages"][0]["content"][1]["input_audio"]
        assert decode_audio(audio) == path.read_bytes()
        calls.append(payload)
        return httpx.Response(200, json={"id": "fixture", "model": "gemini-subscription", "choices": [{"finish_reason": "stop", "message": {"content": "heard"}}], "usage": {"total_tokens": 12}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(transport), **kwargs))
    server = create_server(tmp_path, "http://fixture")
    async def run():
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == {"analyze_audio", "compare_audio"}
        return await server.call_tool("analyze_audio", {"asset_path": "sample.wav", "question": "Describe", "focus": ["vocals"]})
    result = asyncio.run(run())
    assert len(calls) == 1
    assert "heard" in str(result)
