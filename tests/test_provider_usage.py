import httpx

from unified_llm_gateway import provider_usage


def test_openrouter_usage_preserves_provider_credit_totals(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(provider_usage.httpx, "get", lambda *args, **kwargs: httpx.Response(200, json={"data": {"total_credits": 20, "total_usage": 3.5}}, request=httpx.Request("GET", "https://example.test")))

    usage = provider_usage._openrouter_usage()

    assert usage == {"state": "ready", "used": 3.5, "limit": 20.0, "remaining": 16.5, "detail": "USD credits"}


def test_minimax_usage_does_not_invent_quota_when_key_is_missing(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert provider_usage._minimax_usage()["state"] == "unavailable"


def test_cached_usage_is_only_refreshed_explicitly(monkeypatch):
    monkeypatch.setattr(provider_usage, "_CACHE", {"sources": {"openrouter": {"state": "ready"}}, "updated_at": "old"})
    monkeypatch.setattr(provider_usage, "_codex_subscription_usage", lambda: {"state": "fresh"})
    monkeypatch.setattr(provider_usage, "_minimax_usage", lambda: {"state": "fresh"})
    monkeypatch.setattr(provider_usage, "_openrouter_usage", lambda: {"state": "fresh"})
    monkeypatch.setattr(provider_usage, "_unavailable", lambda detail: {"state": "unavailable", "detail": detail})

    assert provider_usage.read_provider_usage()["updated_at"] == "old"
    assert provider_usage.read_provider_usage(refresh=True)["sources"]["openrouter"]["state"] == "fresh"
