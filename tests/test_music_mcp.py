import asyncio
import io
import wave
from types import SimpleNamespace

from mcp.server.mcpserver import MCPServer

from clients import music_mcp
from clients.music_mcp import create_server


def test_music_server_registers_listening_tools_with_mcpserver(tmp_path):
    server = create_server(tmp_path, "http://127.0.0.1:4000")

    assert isinstance(server, MCPServer)
    assert {tool.name for tool in asyncio.run(server.list_tools())} == {"analyze_audio", "compare_audio"}


def test_music_tool_returns_structured_invalid_input_result(tmp_path):
    server = create_server(tmp_path, "http://127.0.0.1:4000")
    result = asyncio.run(server.call_tool("analyze_audio", {
        "asset_path": "missing.wav", "question": "Check clarity", "focus": ["vocal"]
    }))

    assert result.is_error is True
    assert result.structured_content["status"] == "error"
    assert result.structured_content["error"]["code"] == "invalid_input"


def test_music_tool_ignores_ambient_gateway_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_MASTER_KEY", "host-only-secret")
    monkeypatch.setenv("SUBROUTE_API_KEY", "legacy-secret")
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 16)
    (tmp_path / "sample.wav").write_bytes(audio.getvalue())
    observed = {}

    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {
                "id": "response-1",
                "model": "gemini-subscription",
                "choices": [{"message": {"content": "Audio observation."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            }

    class AsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def post(self, url, **kwargs):
            observed["request"] = kwargs
            return Response()

    monkeypatch.setattr(music_mcp, "httpx", SimpleNamespace(AsyncClient=AsyncClient))
    server = create_server(tmp_path, "http://127.0.0.1:4000")
    result = asyncio.run(server.call_tool("analyze_audio", {
        "asset_path": "sample.wav", "question": "Check vocal clarity", "focus": ["vocal"]
    }))

    assert result.is_error is False
    assert result.structured_content["status"] == "complete"
    assert "headers" not in observed["request"]
