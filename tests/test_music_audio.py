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


@pytest.mark.parametrize("outcome", ["complete", "missing", "unexpected", "timeout", "disconnect", "refused", "refused_before_reads"])
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
        if outcome not in {"missing", "refused_before_reads"}:
            events.append({"event": "step_update", "step_update": {
                "step_type": "tool", "tool_name": "view_file", "state": "DONE",
                "tool_info": {"parameters": {"AbsolutePath": str(path) if outcome != "unexpected" else "/root/unexpected"}},
            }})
        response_text = " \nThis request was blocked by Gemini's filters. Please rephrase." if outcome.startswith("refused") else "heard"
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
            assert response.status_code == {"complete": 200, "missing": 502, "unexpected": 502, "timeout": 504, "refused": 502, "refused_before_reads": 502}[outcome]
            if outcome.startswith("refused"):
                assert response.json()["code"] == "provider_content_filter"
                assert response.json()["phase"] == ("before_attachment_reads" if outcome == "refused_before_reads" else "after_attachment_reads")
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


def test_mcp_provider_refusal_is_a_tool_error_without_retry(tmp_path, monkeypatch):
    (tmp_path / "sample.wav").write_bytes(decode_audio(sample()))
    client_type = httpx.AsyncClient
    calls = []
    def transport(req):
        calls.append(req)
        return httpx.Response(502, json={"error": {"message":
            "Antigravity bridge returned 502: provider_content_filter phase=after_attachment_reads conversation_id=fixture"}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(transport), **kwargs))
    server = create_server(tmp_path, "http://fixture")
    result = asyncio.run(server.call_tool("analyze_audio", {"asset_path": "sample.wav", "question": "Describe", "focus": ["vocals"]}))
    assert result.isError
    assert result.structuredContent["status"] == "refused"
    assert result.structuredContent["error"]["code"] == "provider_content_filter"
    assert result.structuredContent["error"]["phase"] == "after_attachment_reads"
    assert result.structuredContent["usage"] is None
    assert len(calls) == 1


def test_mp3_metadata_uses_the_exact_attachment_snapshot(tmp_path, monkeypatch):
    # Original 250 ms tone encoded by ffmpeg/libmp3lame at 44.1 kHz mono 96k.
    source = Path(__file__).parent / "fixtures/music-tone.mp3"
    raw = source.read_bytes()
    path = tmp_path / "tone.mp3"
    path.write_bytes(raw)
    attachment, receipt = read_asset(tmp_path, "tone.mp3")
    assert base64.b64decode(attachment["data"]) == raw
    assert receipt["sample_rate"] == 44100 and receipt["channels"] == 1
    assert 0.25 <= receipt["duration_seconds"] < 0.32  # Encoder padding is retained.
    assert receipt["duration_source"] == "mpeg_headers" and receipt["duration_is_estimate"]


def test_id3_prefix_without_audio_rejected_before_provider_work(tmp_path, monkeypatch):
    (tmp_path / "fake.mp3").write_bytes(b"ID3" + b"\x00" * 100)
    def forbidden(**kwargs):
        pytest.fail("unreadable MP3 must not reach the provider")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    result = asyncio.run(create_server(tmp_path, "http://fixture").call_tool("analyze_audio", {
        "asset_path": "fake.mp3", "question": "Listen", "focus": ["clarity"]}))
    assert result.isError and result.structuredContent["error"]["code"] == "invalid_input"


def test_refused_usage_survives_the_public_error_message(tmp_path, monkeypatch):
    (tmp_path / "sample.wav").write_bytes(decode_audio(sample()))
    client_type = httpx.AsyncClient
    def respond(req):
        return httpx.Response(502, json={"error": {"message":
            "provider_content_filter phase=after_attachment_reads request_id=abc conversation_id=def provider_usage=10,2,15No fallback model group found"}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    result = asyncio.run(create_server(tmp_path, "http://fixture").call_tool("analyze_audio", {
        "asset_path": "sample.wav", "question": "Listen", "focus": ["clarity"]}))
    assert result.isError and result.structuredContent["status"] == "refused"
    assert result.structuredContent["usage"] == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 15}


@pytest.mark.parametrize("outcome,code", [
    ("timeout", "gateway_timeout"), ("disconnect", "gateway_unavailable"),
    ("invalid_json", "invalid_response"), ("empty", "invalid_response"),
    ("wrong_model", "invalid_response"), ("refused", "provider_content_filter"),
])
def test_mcp_failure_then_recovery_preserves_question_and_binary(tmp_path, monkeypatch, outcome, code):
    path = tmp_path / "sample.wav"
    path.write_bytes(decode_audio(sample()))
    client_type = httpx.AsyncClient
    calls = []
    question = "secret code: assess the killer trap beat, compressor attack and release; 不要删词。"
    def transport(req):
        payload = json.loads(req.content)
        assert question in payload["messages"][0]["content"][0]["text"]
        assert decode_audio(payload["messages"][0]["content"][1]["input_audio"]) == path.read_bytes()
        calls.append(payload)
        if len(calls) == 1:
            if outcome == "timeout":
                raise httpx.ReadTimeout("fixture", request=req)
            if outcome == "disconnect":
                raise httpx.ConnectError("fixture", request=req)
            if outcome == "invalid_json":
                return httpx.Response(200, text="invalid")
            if outcome == "refused":
                return httpx.Response(502, json={"error": {"message": "provider_content_filter phase=after_attachment_reads request_id=abc conversation_id=def"}})
        return httpx.Response(200, json={"id": "fixture", "model": "other" if len(calls) == 1 and outcome == "wrong_model" else "gemini-subscription",
            "choices": [{"finish_reason": "stop", "message": {"content": "   " if len(calls) == 1 and outcome == "empty" else "heard"}}], "usage": {"total_tokens": 12}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(transport), **kwargs))
    server = create_server(tmp_path, "http://fixture")
    async def run():
        args = {"asset_path": "sample.wav", "question": question, "focus": ["vocals"]}
        first = await server.call_tool("analyze_audio", args)
        assert len(calls) == 1  # No fallback or automatic retry on failure.
        second = await server.call_tool("analyze_audio", args)
        return first, second
    first, second = asyncio.run(run())
    assert first.isError and first.structuredContent["error"]["code"] == code
    assert not second.isError and second.structuredContent["status"] == "complete"
    assert first.structuredContent["inputs"] == second.structuredContent["inputs"]
    assert len(calls) == 2


@pytest.mark.parametrize("question,focus", [(" ", ["vocal"]), ("listen", []), ("x" * 8001, ["vocal"]), ("listen", ["x"] * 17)])
def test_mcp_rejects_unbounded_or_empty_prompt_before_provider_work(tmp_path, monkeypatch, question, focus):
    def forbidden(**kwargs):
        pytest.fail("invalid input must not open a provider client")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    result = asyncio.run(create_server(tmp_path, "http://fixture").call_tool("analyze_audio", {
        "asset_path": "nonexistent.wav", "question": question, "focus": focus}))
    assert result.isError and result.structuredContent["error"]["code"] == "invalid_input"
