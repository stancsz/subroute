import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from subroute import search_mcp


ROOT = Path(__file__).resolve().parents[1]


def test_gateway_url_requires_loopback_and_canonicalizes_ipv6():
    assert search_mcp.validate_gateway_url("http://localhost:4000/") == "http://localhost:4000"
    assert search_mcp.validate_gateway_url("http://[::1]:4000") == "http://[::1]:4000"
    for url in (
        "https://127.0.0.1:4000",
        "http://user:secret@127.0.0.1:4000",
        "http://gateway.example:4000",
        "http://[::1%25eth0]:4000",
    ):
        with pytest.raises(ValueError):
            search_mcp.validate_gateway_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/", "http://[::1]/", "http://localhost/", "http://user:pass@example.com/", "https://example.com:8443/"])
def test_fetch_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        search_mcp.validate_public_url(url)


def test_fetch_rejects_private_dns_results(monkeypatch):
    monkeypatch.setattr(search_mcp.socket, "getaddrinfo", lambda *_args, **_kwargs: [(None, None, None, None, ("192.168.1.10", 0))])
    with pytest.raises(ValueError, match="non-public"):
        asyncio.run(search_mcp._validate_resolved_public_host("https://public.example/"))


def test_fetch_extracts_html_and_marks_page_as_untrusted(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def aiter_bytes(self): yield b"<h1>Useful docs</h1><script>ignore me</script><p>Page text</p>"

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10, "follow_redirects": False, "trust_env": False}
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        def stream(self, *_args, **_kwargs): return Response()

    async def public_host(_url): return None
    monkeypatch.setattr(search_mcp, "_validate_resolved_public_host", public_host)
    monkeypatch.setattr(search_mcp.httpx, "AsyncClient", Client)
    result = asyncio.run(search_mcp.fetch_public_page("https://example.com/docs"))
    assert result.is_error is False
    assert result.structured_content["text"] == "Useful docs\nPage text"
    assert "ignore me" not in result.content[0].text
    assert "untrusted" in result.structured_content["notice"]


def test_search_tool_supports_explicit_single_url_fetch_without_search(monkeypatch):
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return search_mcp._error("test_complete", "fetch path reached")

    monkeypatch.setattr(search_mcp, "fetch_public_page", fetch)
    result = asyncio.run(search_mcp.create_server().call_tool(
        "web_search", {"fetch_url": "https://docs.python.org/3/library/asyncio-task.html"}
    ))
    assert fetched == ["https://docs.python.org/3/library/asyncio-task.html"]
    assert result.structured_content["error"]["code"] == "test_complete"


def test_search_tool_rejects_ambiguous_and_missing_operations(monkeypatch):
    async def unexpected(*_args, **_kwargs):
        pytest.fail("ambiguous or missing operation must not dispatch")

    monkeypatch.setattr(search_mcp, "fetch_public_page", unexpected)
    server = search_mcp.create_server()
    ambiguous = asyncio.run(server.call_tool("web_search", {"queries": ["docs"], "fetch_url": "https://example.com/"}))
    missing = asyncio.run(server.call_tool("web_search", {}))
    assert ambiguous.structured_content["error"]["code"] == "ambiguous_operation"
    assert missing.structured_content["error"]["code"] == "missing_operation"


@pytest.mark.parametrize(("status", "content_type", "chunks", "error"), [
    (302, "text/html", [], "redirect_blocked"),
    (200, "image/png", [], "unsupported_content_type"),
    (200, "text/plain", [b"x" * (search_mcp.MAX_FETCH_BYTES + 1)], "page_too_large"),
])
def test_fetch_rejects_redirects_non_text_and_oversized_pages(monkeypatch, status, content_type, chunks, error):
    class Response:
        status_code = status
        headers = {"content-type": content_type}
        encoding = "utf-8"
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def aiter_bytes(self):
            for chunk in chunks: yield chunk

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        def stream(self, *_args, **_kwargs): return Response()

    async def public_host(_url): return None
    monkeypatch.setattr(search_mcp, "_validate_resolved_public_host", public_host)
    monkeypatch.setattr(search_mcp.httpx, "AsyncClient", Client)
    result = asyncio.run(search_mcp.fetch_public_page("https://example.com/"))
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == error


def test_search_uses_loopback_without_environment_proxy_or_redirects(monkeypatch):
    observed = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "results": [
                    {"title": "Result", "url": "https://example.test/page", "snippet": "Evidence"}
                ]
            }

    class Client:
        def __init__(self, **kwargs):
            observed["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            observed["url"] = url
            observed["request"] = kwargs
            return Response()

    monkeypatch.setattr(search_mcp.httpx, "AsyncClient", Client)
    result = asyncio.run(
        search_mcp.search_gateway(["current docs"], "short", "http://127.0.0.1:4000", "local-secret")
    )

    assert result.is_error is False
    assert observed["url"] == "http://127.0.0.1:4000/v1/alpha/search"
    assert observed["client_options"] == {
        "timeout": 12,
        "follow_redirects": False,
        "trust_env": False,
    }
    assert observed["request"]["headers"]["Authorization"] == "Bearer local-secret"
    assert observed["request"]["json"]["commands"]["search_query"] == [{"q": "current docs"}]
    assert result.structured_content["results"][0]["url"] == "https://example.test/page"


def test_invalid_gateway_url_is_rejected_before_sending_credentials(monkeypatch):
    called = False

    class Client:
        def __init__(self, **_kwargs):
            nonlocal called
            called = True

    monkeypatch.setattr(search_mcp.httpx, "AsyncClient", Client)
    result = asyncio.run(
        search_mcp.search_gateway(["query"], "medium", "http://attacker.example", "secret")
    )

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "invalid_gateway_url"
    assert called is False
    assert "secret" not in result.content[0].text


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [(401, "gateway_auth"), (403, "gateway_auth"), (502, "search_failed")],
)
def test_gateway_errors_are_redacted(monkeypatch, status_code, error_code):
    class Response:
        def __init__(self):
            self.status_code = status_code

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(search_mcp.httpx, "AsyncClient", Client)
    result = asyncio.run(
        search_mcp.search_gateway(["query"], "short", "http://127.0.0.1:4000", "secret")
    )

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == error_code
    assert result.structured_content["error"]["http_status"] == status_code
    assert "secret" not in result.content[0].text


def test_malformed_gateway_results_return_a_typed_error(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"results": {"unexpected": "object"}}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(search_mcp.httpx, "AsyncClient", Client)
    result = asyncio.run(
        search_mcp.search_gateway(["query"], "short", "http://127.0.0.1:4000", None)
    )

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "invalid_gateway_response"


def test_empty_gateway_results_are_reported_as_search_error(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"results": []}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(search_mcp.httpx, "AsyncClient", Client)
    result = asyncio.run(
        search_mcp.search_gateway(["query"], "short", "http://127.0.0.1:4000", None)
    )

    assert result.is_error is True
    assert result.structured_content == {
        "status": "error",
        "error": {
            "code": "empty_results",
            "message": "The search provider returned no usable results. Do not treat this as verified information.",
        },
    }
    assert "Do not treat this as verified information" in result.content[0].text


def test_rendered_results_obey_output_limit(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "results": [
                    {"title": f"title-{index}", "url": f"https://example.test/{index}", "snippet": "x" * 80}
                    for index in range(5)
                ]
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(search_mcp.httpx, "AsyncClient", Client)
    monkeypatch.setattr(search_mcp, "MAX_OUTPUT_CHARS", 200)
    result = asyncio.run(
        search_mcp.search_gateway(["query"], "long", "http://127.0.0.1:4000", None)
    )

    assert result.is_error is False
    assert result.structured_content["truncated"] is True
    assert len(result.content[0].text) <= 200


def test_project_configs_are_additive_and_project_scoped():
    claude = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    codex = tomllib.loads((ROOT / ".codex" / "config.toml").read_text(encoding="utf-8"))

    assert claude["mcpServers"]["subroute-search"]["command"] == "uv"
    assert "${GATEWAY_MASTER_KEY}" == claude["mcpServers"]["subroute-search"]["env"]["GATEWAY_MASTER_KEY"]
    # Codex's protected tool allowlist exposes only this combined interface.
    assert codex["mcp_servers"]["subroute-search"]["enabled_tools"] == ["web_search"]
    assert codex["mcp_servers"]["subroute-search"]["env_vars"] == [
        "GATEWAY_MASTER_KEY",
        "SUBROUTE_GATEWAY_URL",
    ]
    assert codex["mcp_servers"]["subroute-search"]["tools"]["web_search"]["approval_mode"] == "auto"
