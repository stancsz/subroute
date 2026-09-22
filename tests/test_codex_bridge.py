"""Exercise LiteLLM's real Chat-to-Responses dispatch without provider spending."""

import asyncio

import litellm
import pytest


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6-luna", "gpt-6-astra", "gpt-5.6-terra"])
def test_standard_bridge_preserves_nonstreaming_advisor_contract(monkeypatch, model):
    captured = []

    async def subscription_endpoint(**request):
        captured.append(request)
        # The existing Codex subscription adapter uses streaming transport even
        # when its caller needs a completed, non-streaming advisor response.
        raise ValueError("fixture endpoint requires stream=true")

    monkeypatch.setattr(litellm, "aresponses", subscription_endpoint)
    with pytest.raises(Exception, match="fixture endpoint requires stream=true"):
        asyncio.run(litellm.acompletion(
            model=f"openai/responses/{model}",
            messages=[{"role": "user", "content": "review"}],
            api_key="fixture", api_base="https://fixture.invalid/v1",
            stream=False, num_retries=0,
        ))
    assert len(captured) == 1
    assert captured[0]["model"] == f"openai/{model}"
    assert captured[0].get("stream") is not True
