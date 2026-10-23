from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import subroute.plugins.dynamic_router as dynamic_routing
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.proxy_server import app

@pytest.fixture
def client():
    async def authenticated_key():
        return SimpleNamespace(team_id=None, team_metadata=None)

    app.dependency_overrides[user_api_key_auth] = authenticated_key
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(user_api_key_auth, None)


def test_codex_search_returns_codex_result_shape(client, monkeypatch):
    calls = []
    from litellm.proxy import proxy_server

    monkeypatch.setattr(
        proxy_server,
        "llm_router",
        SimpleNamespace(
            search_tools=[
                {
                    "search_tool_name": "subroute-searxng",
                    "litellm_params": {
                        "search_provider": "searxng",
                        "api_base": "https://search.example.test",
                    },
                }
            ]
        ),
    )

    class Result:
        def model_dump(self, exclude_none=True):
            return {
                "title": "Subroute search",
                "url": "https://docs.example.test/search",
                "snippet": "Configured SearXNG result",
            }

    async def fake_asearch(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(results=[Result()])

    import litellm

    monkeypatch.setattr(litellm, "asearch", fake_asearch)
    response = client.post(
        "/v1/alpha/search",
        json={
            "id": "search-1",
            "model": "gpt-5-codex",
            "input": "search the web",
            "commands": {"search_query": [{"q": "Subroute search"}]},
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "output": "1. Subroute search\nhttps://docs.example.test/search\nConfigured SearXNG result",
        "results": [
            {
                "title": "Subroute search",
                "url": "https://docs.example.test/search",
                "snippet": "Configured SearXNG result",
            }
        ],
    }
    assert calls == [
        {
            "query": "Subroute search",
            "search_provider": "searxng",
            "api_base": "https://search.example.test",
        }
    ]


@pytest.mark.parametrize(
    ("response_length", "expected_max_results"),
    [("short", 5), ("medium", 10), ("long", 20)],
)
def test_codex_search_response_length_bounds_litellm_results(
    client, monkeypatch, response_length, expected_max_results
):
    calls = []
    from litellm.proxy import proxy_server

    monkeypatch.setattr(
        proxy_server,
        "llm_router",
        SimpleNamespace(
            search_tools=[
                {
                    "search_tool_name": "subroute-searxng",
                    "litellm_params": {"search_provider": "searxng"},
                }
            ]
        ),
    )

    class Result:
        def model_dump(self, exclude_none=True):
            return {
                "title": "Bounded result",
                "url": "https://example.test/bounded",
                "snippet": "A usable result for the response-length check.",
            }

    async def fake_asearch(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(results=[Result()])

    import litellm

    monkeypatch.setattr(litellm, "asearch", fake_asearch)
    response = client.post(
        "/v1/alpha/search",
        json={
            "id": "search-length",
            "model": "gpt-5-codex",
            "commands": {
                "search_query": [{"q": "bounded search"}],
                "response_length": response_length,
            },
        },
    )

    assert response.status_code == 200
    assert calls == [
        {
            "query": "bounded search",
            "search_provider": "searxng",
            "max_results": expected_max_results,
        }
    ]


def test_codex_search_response_length_caps_results_returned_by_provider(client, monkeypatch):
    from litellm.proxy import proxy_server

    monkeypatch.setattr(
        proxy_server,
        "llm_router",
        SimpleNamespace(
            search_tools=[
                {
                    "search_tool_name": "subroute-searxng",
                    "litellm_params": {"search_provider": "searxng"},
                }
            ]
        ),
    )

    class Result:
        def __init__(self, index):
            self.index = index

        def model_dump(self, exclude_none=True):
            return {
                "title": f"Result {self.index}",
                "url": f"https://example.test/{self.index}",
                "snippet": f"Snippet {self.index}",
            }

    async def fake_asearch(**kwargs):
        return SimpleNamespace(results=[Result(index) for index in range(1, 9)])

    import litellm

    monkeypatch.setattr(litellm, "asearch", fake_asearch)
    response = client.post(
        "/v1/alpha/search",
        json={
            "id": "search-length-capped",
            "model": "gpt-5-codex",
            "commands": {
                "search_query": [{"q": "bounded search"}],
                "response_length": "short",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 5
    assert body["output"].count("Snippet ") == 5
    assert [item["title"] for item in body["results"]] == [
        f"Result {index}" for index in range(1, 6)
    ]


def test_codex_search_returns_error_when_no_usable_results(client, monkeypatch):
    from litellm.proxy import proxy_server

    monkeypatch.setattr(
        proxy_server,
        "llm_router",
        SimpleNamespace(
            search_tools=[
                {
                    "search_tool_name": "subroute-searxng",
                    "litellm_params": {"search_provider": "searxng"},
                }
            ]
        ),
    )

    async def empty_asearch(**_kwargs):
        return SimpleNamespace(results=[], unresponsive_engines=["private provider detail"])

    import litellm

    monkeypatch.setattr(litellm, "asearch", empty_asearch)
    response = client.post(
        "/v1/alpha/search",
        json={
            "id": "search-empty",
            "model": "gpt-5-codex",
            "commands": {"search_query": [{"q": "known hit query"}]},
        },
    )

    assert response.status_code == 502
    assert response.json()["detail"]["error"]["code"] == "empty_results"
    assert "no usable results" in response.text
    assert "private provider detail" not in response.text


def test_codex_search_rejects_malformed_request(client):
    response = client.post(
        "/v1/alpha/search",
        json={"id": "missing-model", "commands": {"search_query": [{"q": "test"}]}},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("commands", "expected_code"),
    [
        ({"search_query": [{"q": "   "}]}, "invalid_request"),
        ({"image_query": [{"q": "cats"}]}, "unsupported_operation"),
        ({"open": [{"ref_id": "https://example.test"}]}, "unsupported_operation"),
    ],
)
def test_codex_search_rejects_invalid_or_unsupported_commands(client, commands, expected_code):
    response = client.post(
        "/v1/alpha/search",
        json={"id": "search-2", "model": "gpt-5-codex", "commands": commands},
    )

    assert response.status_code == (422 if expected_code == "invalid_request" else 400)
    if expected_code != "invalid_request":
        assert response.json()["detail"]["error"]["code"] == expected_code


def test_codex_search_maps_litellm_failure_without_leaking_provider_error(client, monkeypatch):
    async def failing_asearch(**kwargs):
        raise RuntimeError("private provider detail")

    import litellm

    monkeypatch.setattr(litellm, "asearch", failing_asearch)
    response = client.post(
        "/v1/alpha/search",
        json={
            "id": "search-3",
            "model": "gpt-5-codex",
            "commands": {"search_query": [{"q": "failure test"}]},
        },
    )

    assert response.status_code == 502
    assert response.json()["detail"]["error"]["code"] == "search_failed"
    assert "private provider detail" not in response.text
