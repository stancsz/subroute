import asyncio

import pytest
from litellm.types.utils import ModelResponse
from pydantic import ValidationError

from unified_llm_gateway import (
    CompletionRequest,
    LiteLLMAdapter,
    MiniMaxAdapter,
    complete,
    desktop_adapter,
    freetoken_adapter,
    gemini_subscription_adapter,
    openai_subscription_adapter,
)


class FakeClient:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or ModelResponse(
            model="test-model",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        )
        self.error = error

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


def request(model="test-model"):
    return CompletionRequest(
        model=model,
        messages=[{"role": "user", "content": "hello"}],
        temperature=0,
        max_tokens=8,
    )


def test_request_validates_only_the_owned_contract():
    with pytest.raises(ValidationError):
        CompletionRequest(model="", messages=[])
    with pytest.raises(ValidationError):
        CompletionRequest(model="x", messages=[{"role": "user"}], stream=True)


@pytest.mark.parametrize(
    "factory,expected_name,expected_base",
    [
        (freetoken_adapter, "freetoken", "http://freetoken.test/v1"),
        (desktop_adapter, "desktop", "http://desktop.test/v1"),
    ],
)
def test_openai_compatible_adapters_map_to_litellm_standard(
    factory, expected_name, expected_base
):
    client = FakeClient()
    adapter = factory(api_base=expected_base, client=client)

    result = asyncio.run(complete(request(), adapter))

    assert adapter.name == expected_name
    assert isinstance(result, ModelResponse)
    assert client.calls == [
        {
            "model": "openai/test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.0,
            "max_tokens": 8,
            "api_base": expected_base,
            "api_key": "local",
            **(
                {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
                if expected_name == "freetoken"
                else {}
            ),
        }
    ]


def test_gemini_subscription_maps_to_antigravity_and_model_response():
    calls = []

    async def runner(command, prompt):
        calls.append((command, prompt))
        return '\n'.join([
            '{"event":"step_update","step_update":{"text_delta":"partial"}}',
            '{"event":"result","result":{"response":"gemini ok"}}',
        ])

    adapter = gemini_subscription_adapter(agy_path="agy-test", runner=runner)
    result = asyncio.run(complete(request("gemini-3.8-flash"), adapter))

    assert isinstance(result, ModelResponse)
    assert result.model == "gemini-3.8-flash"
    assert result.choices[0].message.content == "gemini ok"
    assert calls == [
        (
            [
                "agy-test",
                "--model",
                "Gemini 3.8 Flash (High)",
                "--output-format",
                "stream-json",
                "--input-format",
                "text",
            ],
            "[User]:\nhello",
        )
    ]


def test_gemini_subscription_fails_closed_for_unknown_model():
    adapter = gemini_subscription_adapter(agy_path="agy-test")
    with pytest.raises(ValueError, match="Unsupported Antigravity model"):
        asyncio.run(complete(request("not-gemini"), adapter))


def test_openai_subscription_uses_litellm_responses_bridge():
    client = FakeClient()
    adapter = openai_subscription_adapter(
        access_token="token",
        account_id="account",
        api_base="https://subscription.test/codex",
        client=client,
    )

    result = asyncio.run(complete(request("gpt-test"), adapter))

    assert isinstance(result, ModelResponse)
    call = client.calls[0]
    assert call["model"] == "openai/responses/gpt-test"
    assert call["api_base"] == "https://subscription.test/codex"
    assert call["extra_headers"]["ChatGPT-Account-ID"] == "account"
    assert "max_tokens" not in call
    assert call["store"] is False
    assert call["stream"] is True


def test_provider_error_is_not_swallowed():
    error = RuntimeError("provider unavailable")
    adapter = LiteLLMAdapter("test", "http://test/v1", client=FakeClient(error=error))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(complete(request(), adapter))


def test_minimax_uses_one_litellm_deployment_per_unique_key():
    client = FakeClient()
    router_calls = []

    def router_factory(**kwargs):
        router_calls.append(kwargs)
        return client

    adapter = MiniMaxAdapter(["key-a", "key-b", "key-a"], router_factory)
    result = asyncio.run(complete(request("MiniMax-M3"), adapter))

    assert isinstance(result, ModelResponse)
    assert router_calls == [
        {
            "model_list": [
                {
                    "model_name": "MiniMax-M3",
                    "litellm_params": {
                        "model": "minimax/MiniMax-M3",
                        "api_key": "key-a",
                    },
                },
                {
                    "model_name": "MiniMax-M3",
                    "litellm_params": {
                        "model": "minimax/MiniMax-M3",
                        "api_key": "key-b",
                    },
                },
            ],
            "routing_strategy": "simple-shuffle",
            "num_retries": 0,
            "fallbacks": [],
        }
    ]
    assert client.calls[0]["model"] == "MiniMax-M3"
