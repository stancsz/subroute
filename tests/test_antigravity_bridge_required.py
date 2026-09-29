import asyncio

import pytest

from subroute.handlers.antigravity import invoke_agy


@pytest.mark.parametrize("structured", [False, True])
def test_subscription_requires_compose_bridge_without_local_cli_fallback(
    monkeypatch, structured
):
    monkeypatch.delenv("ANTIGRAVITY_BRIDGE_URL", raising=False)
    # AGY_PATH used to enable a second, less constrained CLI transport. It is
    # deliberately ignored now; Compose owns the supported provider transport.
    monkeypatch.setenv("AGY_PATH", "fixture-agy")
    spawned = False

    async def unexpected_spawn(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("local CLI fallback must not start")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_spawn)
    options = {"json_schema": {"type": "object"}} if structured else {}

    with pytest.raises(RuntimeError, match="requires the authenticated Compose CLI bridge"):
        asyncio.run(invoke_agy("gemini-3.8-flash", "hello", **options))

    assert not spawned
