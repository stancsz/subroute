import asyncio

import pytest
from litellm.llms.custom_llm import CustomLLMError

from unified_llm_gateway.handlers import antigravity
from unified_llm_gateway.handlers.antigravity import (
    antigravity_handler,
    prompt_from_messages,
)


def test_antigravity_prompt_is_transport_only_text():
    assert prompt_from_messages([{"role": "user", "content": "hello"}]) == (
        "[User]:\nhello"
    )


def test_antigravity_rejects_multimodal_content_until_certified():
    with pytest.raises(ValueError, match="text messages only"):
        prompt_from_messages(
            [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        )


def test_antigravity_custom_provider_returns_litellm_model_response(monkeypatch):
    async def fake_invoke(model: str, prompt: str) -> str:
        assert model == "gemini-3.8-flash"
        assert prompt == "[User]:\nhello"
        return "provider response"

    monkeypatch.setattr(antigravity, "invoke_agy", fake_invoke)
    response = asyncio.run(
        antigravity_handler.acompletion(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "hello"}],
            optional_params={},
        )
    )

    assert response.choices[0].message.content == "provider response"
    assert response.model == "gemini-3.8-flash"


def test_antigravity_custom_provider_rejects_tools(monkeypatch):
    with pytest.raises(CustomLLMError, match="tool calls are not yet certified"):
        asyncio.run(
            antigravity_handler.acompletion(
                model="gemini-3.8-flash",
                messages=[{"role": "user", "content": "hello"}],
                optional_params={"tools": [{"type": "function"}]},
            )
        )
