import asyncio
import base64

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ImageContent

from subroute.image_mcp import create_server, decode_image

PNG = b"\x89PNG\r\n\x1a\nfixture"


def test_decode_image_requires_valid_supported_image():
    data, media_type = decode_image({"b64_json": base64.b64encode(PNG).decode()})
    assert (data, media_type) == (PNG, "image/png")
    with pytest.raises(ValueError, match="unsupported image format"):
        decode_image({"b64_json": base64.b64encode(b"not an image").decode()})


def test_mcp_image_tool_returns_inline_image_content(monkeypatch):
    from subroute import image_mcp

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 190, "follow_redirects": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers, json):
            assert url == "http://127.0.0.1:4000/v1/images/generations"
            assert json == {"model": "codex-luna", "prompt": "anime portrait", "n": 1,
                            "response_format": "b64_json", "size": "auto"}
            return httpx.Response(200, request=httpx.Request("POST", url),
                                  json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    monkeypatch.setattr(image_mcp.httpx, "AsyncClient", Client)
    server = create_server()
    tool = server._tool_manager.get_tool("generate_image")
    result = asyncio.run(tool.run({"prompt": " anime portrait "}, convert_result=True))
    assert result == [ImageContent(type="image", data=base64.b64encode(PNG).decode(), mimeType="image/png")]


def test_mcp_image_tool_reports_upstream_failure_without_retry(monkeypatch):
    from subroute import image_mcp

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return httpx.Response(502, request=httpx.Request("POST", "http://localhost"))

    monkeypatch.setattr(image_mcp.httpx, "AsyncClient", Client)
    tool = create_server()._tool_manager.get_tool("generate_image")
    with pytest.raises(ToolError, match="HTTP 502"):
        asyncio.run(tool.run({"prompt": "anime portrait"}, convert_result=True))


def test_mcp_image_tool_rejects_empty_and_overlong_prompt_without_dispatch():
    tool = create_server()._tool_manager.get_tool("generate_image")
    for prompt in ("  ", "x" * 8001):
        with pytest.raises(ToolError, match="1 to 8000"):
            asyncio.run(tool.run({"prompt": prompt}, convert_result=True))
