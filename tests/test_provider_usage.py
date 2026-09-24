import httpx

from subroute import provider_usage


def test_openrouter_usage_preserves_provider_credit_totals(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(provider_usage.httpx, "get", lambda *args, **kwargs: httpx.Response(200, json={"data": {"total_credits": 20, "total_usage": 3.5}}, request=httpx.Request("GET", "https://example.test")))

    usage = provider_usage._openrouter_usage()

    assert usage == {"state": "ready", "used": 3.5, "limit": 20.0, "remaining": 16.5, "detail": "USD credits"}


def test_minimax_usage_does_not_invent_quota_when_key_is_missing(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert provider_usage._minimax_usage()["state"] == "unavailable"


def test_gemini_subscription_status_comes_from_docker_bridge(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://antigravity:4015")
    monkeypatch.setattr(
        provider_usage.httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={"authenticated": True, "models": ["gemini-a", "gemini-b"]},
            request=httpx.Request("GET", "http://antigravity:4015/v1/status"),
        ),
    )

    assert provider_usage._gemini_subscription_usage() == {
        "state": "connected",
        "detail": "Authenticated Docker bridge; 2 subscription models available",
    }


def test_gemini_subscription_reports_container_sign_in_required(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://antigravity:4015")
    monkeypatch.setattr(
        provider_usage.httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={"authenticated": False, "detail": "Docker bridge ready; Antigravity sign-in required"},
            request=httpx.Request("GET", "http://antigravity:4015/v1/status"),
        ),
    )

    assert provider_usage._gemini_subscription_usage() == {
        "state": "sign_in_required",
        "detail": "Docker bridge ready; Antigravity sign-in required",
    }


def test_cached_usage_is_only_refreshed_explicitly(monkeypatch):
    monkeypatch.setattr(provider_usage, "_CACHE", {"sources": {"openrouter": {"state": "ready"}}, "updated_at": "old"})
    monkeypatch.setattr(provider_usage, "_codex_subscription_usage", lambda: {"state": "fresh"})
    monkeypatch.setattr(provider_usage, "_minimax_usage", lambda: {"state": "fresh"})
    monkeypatch.setattr(provider_usage, "_openrouter_usage", lambda: {"state": "fresh"})
    monkeypatch.setattr(provider_usage, "_gemini_subscription_usage", lambda: {"state": "fresh"})
    monkeypatch.setattr(provider_usage, "_unavailable", lambda detail: {"state": "unavailable", "detail": detail})

    assert provider_usage.read_provider_usage()["updated_at"] == "old"
    assert provider_usage.read_provider_usage(refresh=True)["sources"]["openrouter"]["state"] == "fresh"
