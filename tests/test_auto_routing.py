"""Exercise LiteLLM's actual fallback traversal without contacting providers."""

import asyncio
from pathlib import Path

import litellm
import pytest
import yaml

from subroute.plugins.dynamic_router import (
    DynamicRoutingPlugin,
    RoutingControlPlane,
    _fallback_eligible,
)


CONFIG = Path(__file__).parents[1] / "config" / "litellm.yaml"


@pytest.mark.parametrize("mode", ["alias", "force", "off"])
def test_auto_preserves_client_effort_and_bypasses_saved_target(tmp_path, mode):
    control = RoutingControlPlane(CONFIG, tmp_path / "policy.json")
    control.update("codex-luna", mode, reasoning_effort="high")
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "auto", "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "low"}
    result = asyncio.run(plugin.async_pre_call_hook({}, None, data, "acompletion"))
    assert result["model"] == "auto"
    assert result["reasoning_effort"] == "low"
    assert result["metadata"]["gateway_reasoning_effort"] is None


def test_generic_fallback_is_limited_to_text_chat_semantics():
    assert _fallback_eligible(
        {"model": "minimax", "messages": [{"role": "user", "content": "hello"}]},
        "acompletion",
    )
    assert not _fallback_eligible(
        {"model": "minimax", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe"}, {"type": "image_url", "image_url": "data"},
        ]}]},
        "acompletion",
    )
    assert not _fallback_eligible({"model": "minimax-tts", "input": "hello"}, "aspeech")
    assert not _fallback_eligible({"model": "minimax", "messages": []}, "aembeddings")


@pytest.mark.parametrize("primary,chain", [
    ("minimax", ["minimax", "openrouter", "mimo-v2.6-pro", "codex-luna"]),
    ("auto", ["auto", "mimo-v2.6-pro", "codex-luna"]),
    ("openrouter", ["openrouter", "mimo-v2.6-pro", "codex-luna"]),
    ("mimo-v2.6-pro", ["mimo-v2.6-pro", "openrouter", "codex-luna"]),
    ("mimo-v2.6-flash", ["mimo-v2.6-flash", "openrouter", "codex-luna"]),
    ("codex-luna", ["codex-luna", "openrouter", "mimo-v2.6-pro"]),
])
@pytest.mark.parametrize("failure_kind", ["unavailable", "bad_request", "context_window", "content_policy",
                                        "rate_limit", "timeout", "internal_server", "connection"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_litellm_catchall_fallback_order_and_exhaustion(monkeypatch, primary, chain, failure_kind, exhausted):
    settings = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    groups = [
        "minimax", "auto", "codex-subscription", "openrouter", "mimo-v2.6-pro",
        "mimo-v2.6-flash", "codex-luna",
    ]
    calls = []
    failure_count = len(chain) if exhausted else 1

    async def completion(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) > failure_count:
            return litellm.ModelResponse(model=kwargs["model"], choices=[{
                "index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop",
            }])
        if failure_kind == "unavailable":
            error = litellm.ServiceUnavailableError(
                message="controlled provider failure", llm_provider="openai", model=kwargs["model"],
            )
        elif failure_kind == "bad_request":
            error = litellm.BadRequestError(
                message="controlled provider rejection", model=kwargs["model"],
                llm_provider="openai",
            )
        elif failure_kind == "context_window":
            error = litellm.ContextWindowExceededError(
                message="controlled context failure", model=kwargs["model"], llm_provider="openai",
            )
        elif failure_kind == "rate_limit":
            error = litellm.RateLimitError(
                message="controlled rate limit", model=kwargs["model"], llm_provider="openai",
            )
        elif failure_kind == "timeout":
            error = litellm.Timeout(
                message="controlled timeout", model=kwargs["model"], llm_provider="openai",
            )
        elif failure_kind == "internal_server":
            error = litellm.InternalServerError(
                message="controlled server failure", model=kwargs["model"], llm_provider="openai",
            )
        elif failure_kind == "connection":
            # LiteLLM 1.101.0 requires llm_provider and model on
            # APIConnectionError, the canonical provider transport error for
            # a failed upstream connection. Building it without those two
            # arguments raises TypeError inside the mocked provider call, and
            # the router does not treat TypeError as a provider failure, so
            # the fallback chain never starts.
            error = litellm.APIConnectionError(
                message="controlled connection failure",
                llm_provider="openai",
                model=kwargs["model"],
            )
        else:
            error = litellm.ContentPolicyViolationError(
                message="controlled provider refusal", model=kwargs["model"], llm_provider="openai",
            )
        raise error

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(litellm, "callbacks", [])

    async def check():
        router = litellm.Router(model_list=[{
            "model_name": group,
            "litellm_params": {"model": f"openai/{group}", "api_key": "fixture-only"},
        } for group in groups], **settings["router_settings"])
        if exhausted:
            with pytest.raises((litellm.ServiceUnavailableError, litellm.BadRequestError,
                                litellm.ContextWindowExceededError, litellm.ContentPolicyViolationError,
                                litellm.RateLimitError, litellm.Timeout, litellm.InternalServerError,
                                litellm.APIConnectionError)):
                await router.acompletion(model=primary, messages=[{"role": "user", "content": "hello"}])
        else:
            response = await router.acompletion(
                model=primary, messages=[{"role": "user", "content": "hello"}],
            )
            assert response.choices[0].message.content == "OK"

    asyncio.run(check())
    expected_chain = chain if exhausted else chain[:2]
    assert calls == [f"openai/{group}" for group in expected_chain]


def test_catchall_fallback_keeps_original_model_name(monkeypatch, tmp_path):
    settings = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    calls = []

    async def fail(*args, **kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "openai/codex-subscription":
            raise litellm.ServiceUnavailableError(
                message="controlled failure", llm_provider="openai", model=kwargs["model"],
            )
        return litellm.ModelResponse(model=kwargs["model"], choices=[{
            "index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop",
        }])

    monkeypatch.setattr(litellm, "acompletion", fail)
    monkeypatch.setattr(litellm, "callbacks", [])

    async def check():
        groups = ["codex-subscription", "openrouter", "mimo-v2.6-pro", "codex-luna"]
        router = litellm.Router(model_list=[{
            "model_name": group,
            "litellm_params": {"model": f"openai/{group}", "api_key": "fixture-only"},
        } for group in groups], **settings["router_settings"])
        response = await router.acompletion(
            model="codex-subscription", messages=[{"role": "user", "content": "hello"}],
        )
        assert response.model == "openai/openrouter"
        plugin = DynamicRoutingPlugin(RoutingControlPlane(CONFIG, tmp_path / "policy.json"))
        response = await plugin.async_post_call_success_hook(
            {"model": "codex-subscription", "metadata": {
                "routing": {"requested_model": "default", "resolved_model": "codex-subscription"},
            }},
            None,
            response,
        )
        assert response.model == "default"

    asyncio.run(check())
    assert calls == ["openai/codex-subscription", "openai/openrouter"]


def test_streaming_fallback_keeps_original_model_name(tmp_path):
    plugin = DynamicRoutingPlugin(RoutingControlPlane(CONFIG, tmp_path / "policy.json"))
    request_data = {
        "model": "codex-subscription",
        "metadata": {"routing": {"requested_model": "default"}},
    }

    async def source():
        yield {"object": "chat.completion.chunk", "model": "openrouter", "choices": []}

    async def check():
        response = plugin.async_post_call_streaming_iterator_hook(None, source(), request_data)
        chunks = [chunk async for chunk in response]
        assert chunks[0]["model"] == "default"

    asyncio.run(check())
