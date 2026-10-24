import asyncio

from mcp.server.mcpserver import MCPServer

from subroute.music_mcp import create_server


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
