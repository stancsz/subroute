import asyncio

import pytest
from litellm.llms.custom_llm import CustomLLMError

from subroute.handlers import antigravity
from subroute.handlers.antigravity import (
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
        return "provider response", antigravity.Usage(
            prompt_tokens=1, completion_tokens=2, total_tokens=3
        )

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


def test_bridge_receives_cli_display_model(monkeypatch):
    request = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "content": "ok",
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, json):
            request.update(url=url, json=json)
            return Response()

    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://antigravity:4015")
    monkeypatch.setattr(antigravity.httpx, "AsyncClient", lambda **_: Client())

    content, usage = asyncio.run(antigravity.invoke_agy("gemini-3.8-flash", "hello"))
    assert content == "ok"
    assert usage.total_tokens == 3
    assert request["json"] == {
        "model": "Gemini 3.8 Flash (High)",
        "prompt": "hello",
    }
