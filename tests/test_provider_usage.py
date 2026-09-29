import asyncio

import pytest

from subroute import provider_usage


def test_openrouter_usage_preserves_provider_credit_totals(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async def get_json(*args, **kwargs):
        return {"data": {"total_credits": 20, "total_usage": 3.5}}

    monkeypatch.setattr(provider_usage, "_get_json", get_json)

    usage = asyncio.run(provider_usage._openrouter_usage())

    assert usage == {"state": "ready", "used": 3.5, "limit": 20.0, "remaining": 16.5, "detail": "USD credits"}


def test_minimax_usage_does_not_invent_quota_when_key_is_missing(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert asyncio.run(provider_usage._minimax_usage())["state"] == "unavailable"


def test_gemini_subscription_status_comes_from_docker_bridge(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://antigravity:4015")
    async def get_json(*args, **kwargs):
        return {"authenticated": True, "models": ["gemini-a", "gemini-b"]}

    monkeypatch.setattr(provider_usage, "_get_json", get_json)

    assert asyncio.run(provider_usage._gemini_subscription_usage()) == {
        "state": "connected",
        "detail": "Authenticated Docker bridge; 2 subscription models available",
    }


def test_gemini_subscription_reports_container_sign_in_required(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://antigravity:4015")
    async def get_json(*args, **kwargs):
        return {"authenticated": False, "detail": "Docker bridge ready; Antigravity sign-in required"}

    monkeypatch.setattr(provider_usage, "_get_json", get_json)

    assert asyncio.run(provider_usage._gemini_subscription_usage()) == {
        "state": "sign_in_required",
        "detail": "Docker bridge ready; Antigravity sign-in required",
    }


@pytest.mark.parametrize("detail", ["Model inventory timed out", "Subscription CLI at capacity"])
def test_inventory_failure_does_not_claim_credentials_need_sign_in(monkeypatch, detail):
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://antigravity:4015")

    async def get_json(*args, **kwargs):
        return {"authenticated": None, "detail": detail}

    monkeypatch.setattr(provider_usage, "_get_json", get_json)
    assert asyncio.run(provider_usage._gemini_subscription_usage()) == {
        "state": "unavailable", "detail": detail,
    }


def test_cached_usage_is_only_refreshed_explicitly(monkeypatch):
    monkeypatch.setattr(provider_usage, "_CACHE", {"sources": {"openrouter": {"state": "ready"}}, "updated_at": "old"})
    async def fresh():
        return {"state": "fresh"}

    monkeypatch.setattr(provider_usage, "_codex_subscription_usage", fresh)
    monkeypatch.setattr(provider_usage, "_minimax_usage", fresh)
    monkeypatch.setattr(provider_usage, "_openrouter_usage", fresh)
    monkeypatch.setattr(provider_usage, "_gemini_subscription_usage", fresh)
    monkeypatch.setattr(provider_usage, "_unavailable", lambda detail: {"state": "unavailable", "detail": detail})

    async def check():
        assert (await provider_usage.read_provider_usage())["updated_at"] == "old"
        assert (await provider_usage.read_provider_usage(refresh=True))["sources"]["openrouter"]["state"] == "fresh"

    asyncio.run(check())


def test_refresh_reads_sources_concurrently_and_keeps_source_failures_independent(monkeypatch):
    monkeypatch.setattr(provider_usage, "_CACHE", None)
    active = 0
    peak = 0

    async def slow():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"state": "connected"}

    async def broken():
        raise ValueError("malformed provider response")

    monkeypatch.setattr(provider_usage, "_codex_subscription_usage", slow)
    monkeypatch.setattr(provider_usage, "_minimax_usage", broken)
    monkeypatch.setattr(provider_usage, "_openrouter_usage", slow)
    monkeypatch.setattr(provider_usage, "_gemini_subscription_usage", slow)

    snapshot = asyncio.run(provider_usage.read_provider_usage(refresh=True))

    assert peak == 3
    assert snapshot["sources"]["openai-subscription"] == {"state": "connected"}
    assert snapshot["sources"]["openrouter"] == {"state": "connected"}
    assert snapshot["sources"]["gemini-subscription"] == {"state": "connected"}
    assert snapshot["sources"]["minimax"] == {
        "state": "unavailable",
        "detail": "minimax usage read failed: ValueError",
    }


def test_refresh_lock_coalesces_simultaneous_provider_work(monkeypatch):
    monkeypatch.setattr(provider_usage, "_CACHE", None)
    calls = 0

    async def slow():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return {"state": "connected"}

    monkeypatch.setattr(provider_usage, "_codex_subscription_usage", slow)
    monkeypatch.setattr(provider_usage, "_minimax_usage", slow)
    monkeypatch.setattr(provider_usage, "_openrouter_usage", slow)
    monkeypatch.setattr(provider_usage, "_gemini_subscription_usage", slow)

    async def check():
        return await asyncio.gather(
            provider_usage.read_provider_usage(refresh=True),
            provider_usage.read_provider_usage(refresh=True),
            provider_usage.read_provider_usage(refresh=True),
        )

    snapshots = asyncio.run(check())

    assert calls == 4
    assert snapshots[0] is snapshots[1] is snapshots[2]


def test_cancelling_a_provider_read_closes_its_http_connection():
    async def check():
        request_started = asyncio.Event()
        peer_closed = asyncio.Event()

        async def receive(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                request_started.set()
                await reader.read()
            finally:
                peer_closed.set()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(receive, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            request = asyncio.create_task(
                provider_usage._get_json(f"http://127.0.0.1:{port}/slow", timeout=10)
            )
            await asyncio.wait_for(request_started.wait(), timeout=1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(peer_closed.wait(), timeout=1)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(check())
