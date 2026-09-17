"""Exercise LiteLLM's real Chat-to-Responses dispatch without provider spending."""

import asyncio

import litellm
import pytest


def test_standard_bridge_preserves_nonstreaming_advisor_contract(monkeypatch):
    captured = []

    async def subscription_endpoint(**request):
        captured.append(request)
        # The existing Codex subscription adapter uses streaming transport even
        # when its caller needs a completed, non-streaming advisor response.
        raise ValueError("fixture endpoint requires stream=true")

    monkeypatch.setattr(litellm, "aresponses", subscription_endpoint)
    with pytest.raises(Exception, match="fixture endpoint requires stream=true"):
        asyncio.run(litellm.acompletion(
            model="openai/responses/gpt-5.6-terra",
            messages=[{"role": "user", "content": "review"}],
            api_key="fixture", api_base="https://fixture.invalid/v1",
            stream=False, num_retries=0,
        ))
    assert len(captured) == 1
    assert captured[0].get("stream") is not True
