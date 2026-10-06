import asyncio

import pytest
from fastapi import HTTPException
from litellm.llms.anthropic.experimental_pass_through.messages.interceptors.advisor import (
    AdvisorOrchestrationHandler,
)

from subroute.plugins.advisor_plugin import (
    ADVISOR_TOOL_TYPE,
    AdvisorPlugin,
)


@pytest.fixture(autouse=True)
def advisor_router(monkeypatch):
    """Supply the live router services required by preconsult and advisor subcalls."""
    from litellm.proxy import proxy_server
    from subroute.plugins import advisor_plugin

    async def unexpected_provider(*args, **kwargs):
        pytest.fail("Unit tests must mock every advisor transport, including fallback providers")

    monkeypatch.setattr(advisor_plugin, "invoke_agy", unexpected_provider)
    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", unexpected_provider)

    class Router:
        def get_model_info(self, model):
            return {"advisor_max_input_tokens": 32000} if model.endswith("-advisor") else {}

        def get_configured_token_limits(self, model):
            return (32000, None) if model.endswith("-advisor") else (256000, None)

        def _count_pre_call_check_tokens(self, *, messages, input, request_kwargs):
            return 1

    router = Router()
    monkeypatch.setattr(proxy_server, "llm_router", router)
    return router


def run(plugin: AdvisorPlugin, data: dict, call_type: str = "anthropic_messages"):
    return asyncio.run(plugin.async_pre_call_hook({}, None, data, call_type))


def advisor_plugin_error(kind: str) -> Exception:
    from subroute.handlers.antigravity import (
        AntigravityBridgeError,
        AntigravityRequestTooLargeError,
    )
    from subroute.handlers.codex_advisor import CodexAdvisorError

    if kind == "runtime":
        return RuntimeError("provider returned no terminal result")
    if kind == "timeout":
        return TimeoutError("provider request timed out")
    if kind == "value":
        return ValueError("provider response omitted usage")
    if kind == "too_large":
        return AntigravityRequestTooLargeError("bridge request exceeds size limit")
    if kind == "bridge_429":
        return AntigravityBridgeError(429, "bridge returned 429")
    if kind == "codex_401":
        return CodexAdvisorError(502, "Codex subscription API error 401")
    if kind == "codex_429":
        return CodexAdvisorError(429, "Codex subscription API error 429")
    if kind == "codex_timeout":
        return TimeoutError("Codex subscription request timed out")
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "error_kind,status_code,error_message",
    [
        ("runtime", 502, "provider returned no terminal result"),
        ("timeout", 504, "provider request timed out"),
        ("value", 400, "provider response omitted usage"),
        ("too_large", 413, "bridge request exceeds size limit"),
        ("bridge_429", 429, "bridge returned 429"),
        ("codex_401", 502, "Codex subscription API error 401"),
        ("codex_429", 429, "Codex subscription API error 429"),
        ("codex_timeout", 504, "Codex subscription request timed out"),
    ],
)
def test_selected_preconsult_failure_fails_closed_with_correlated_reason(
    monkeypatch, caplog, error_kind, status_code, error_message
):
    from subroute.plugins import advisor_plugin

    messages = [{"role": "user", "content": "review this design"}]
    data = {
        "model": "codex-luna",
        "messages": messages.copy(),
        "litellm_call_id": "gateway-request-test-123",
        "metadata": {"gateway_policy": {
            "active_model": "codex-luna",
            "mode": "force",
            "policy_version": 41,
            "advisor_model": "gemini-subscription",
            "advisor_reasoning_effort": "low",
        }},
    }

    attempts = []

    async def fail_gemini(*args, **kwargs):
        attempts.append("gemini")
        raise advisor_plugin_error(error_kind)

    async def fail_codex(*args, **kwargs):
        attempts.append("codex")
        raise advisor_plugin_error(error_kind)

    monkeypatch.setattr(advisor_plugin, "invoke_agy", fail_gemini)
    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", fail_codex)

    with pytest.raises(HTTPException) as caught:
        run(AdvisorPlugin(), data)

    assert caught.value.status_code == status_code
    assert attempts == ["gemini", "codex"]
    assert caught.value.detail["error"]["type"] == "advisor_consultation_failed"
    assert caught.value.detail["error"]["message"] == (
        "Selected advisor failed; base request was not sent"
    )
    assert caught.value.detail["error"]["request_id"] == "gateway-request-test-123"

    receipt = data["metadata"]["gateway_advisor"]
    assert receipt["status"] == "failed"
    assert receipt["model"] == "gemini-subscription"
    assert len(receipt["consultation_id"]) == 32
    assert receipt["reason"] == error_message
    assert receipt["fallback_attempt"] == {"failed_model": "gemini-subscription", "reason": error_message}
    assert receipt["gateway_request_id"] == "gateway-request-test-123"
    assert data["messages"] == messages
    assert f"consultation_id={receipt['consultation_id']}" in caplog.text
    assert "gateway_request_id=gateway-request-test-123" in caplog.text
    assert "model=gemini-subscription" in caplog.text
    assert "base_model=codex-luna" in caplog.text
    assert f"error={error_message}" in caplog.text


def test_advisor_failure_reason_redacts_credentials_and_is_bounded():
    from subroute.plugins.advisor_plugin import _safe_error_reason

    reason = _safe_error_reason(RuntimeError(
        'request failed\nAuthorization: Bearer abc.secret/token== '
        'api_key="sk-proj-abcdefghijklmnopqrstuvwxyz123456" '
        'access_token=refreshsecret password=hunter2 '
        'Google key AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 '
        + "x" * 400
    ))

    assert len(reason) == 300
    assert "request failed" in reason
    assert "[REDACTED]" in reason
    assert "abc.secret/token" not in reason
    assert "sk-proj-" not in reason
    assert "refreshsecret" not in reason
    assert "hunter2" not in reason
    assert "AIza" not in reason
    assert "\n" not in reason


@pytest.mark.parametrize("error_kind", ["runtime", "timeout", "value", "too_large", "bridge_429", "codex_401", "codex_429", "codex_timeout"])
def test_preconsult_first_failure_uses_one_fallback_and_its_actual_usage(monkeypatch, error_kind):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    attempts = []
    async def gemini(*args, **kwargs):
        attempts.append("gemini")
        raise advisor_plugin_error(error_kind)
    async def codex(*args, **kwargs):
        attempts.append("codex")
        return "Fallback guidance", Usage(prompt_tokens=31, completion_tokens=7, total_tokens=38)
    monkeypatch.setattr(advisor_plugin, "invoke_agy", gemini)
    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", codex)
    messages = [{"role": "user", "content": "Review this design"}]
    data = {"model": "codex-luna", "messages": messages.copy(), "litellm_call_id": "fallback-test",
        "metadata": {"gateway_policy": {"advisor_model": "gemini-subscription", "advisor_reasoning_effort": "low"}}}
    assert run(AdvisorPlugin(), data) is data
    assert attempts == ["gemini", "codex"]
    assert data["messages"][:-1] == messages
    assert data["messages"][-1] == {"role": "developer", "content": "Independent advisor guidance:\nFallback guidance"}
    receipt = data["metadata"]["gateway_advisor"]
    assert receipt["status"] == "advice_injected" and receipt["resolved_model"] == "codex-gpt-6.1-sol-advisor"
    assert receipt["usage_source"] == "provider" and receipt["input_tokens"] == 31 and receipt["output_tokens"] == 7
    assert receipt["fallback_attempt"] == {"failed_model": "gemini-subscription", "reason": str(advisor_plugin_error(error_kind))}


def test_selected_advisor_enables_plain_model_on_messages_route():
    plugin = AdvisorPlugin(
        max_uses=2,
    )
    data = {
        "model": "openrouter",
        "messages": [{"role": "user", "content": "review this design"}],
        "tools": [{"name": "existing", "input_schema": {"type": "object"}}],
    }

    data["metadata"] = {"gateway_policy": {"advisor_model": "gemini-subscription"}}
    result = run(plugin, data)

    assert result is data
    assert data["model"] == "openrouter"
    assert data["tools"][0]["name"] == "existing"
    assert data["tools"][1] == {
        "type": ADVISOR_TOOL_TYPE,
        "name": "advisor",
        "model": "gemini-3.8-flash-advisor",
        "max_uses": 2,
        "caching": {"type": "ephemeral", "ttl": "5m"},
    }
    assert AdvisorOrchestrationHandler().can_handle(
        data["tools"], custom_llm_provider="openrouter"
    )


def test_does_not_duplicate_existing_advisor_tool():
    plugin = AdvisorPlugin()
    existing = {"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "other"}
    data = {"model": "openrouter", "tools": [existing]}

    data["metadata"] = {"gateway_policy": {"advisor_model": "gemini-subscription"}}
    run(plugin, data)

    assert data["tools"] == [existing]


def test_policy_selected_non_subscription_target_uses_native_advisor():
    plugin = AdvisorPlugin()
    data = {
        "model": "minimax",
        "messages": [{"role": "user", "content": "review this design"}],
        "metadata": {"gateway_policy": {
            "active_model": "minimax", "mode": "force", "policy_version": 38,
            "advisor_model": "codex-sol-advisor",
        }},
    }

    run(plugin, data)

    assert data["tools"] == [{
        "type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "codex-sol-advisor",
        "max_uses": 3, "caching": {"type": "ephemeral", "ttl": "5m"},
    }]


@pytest.mark.parametrize("effort", [None, "high"])
@pytest.mark.parametrize("target", ["codex-luna", "gemini-subscription-pro"])
@pytest.mark.parametrize("metadata_key", ["metadata", "litellm_metadata"])
def test_codex_subscription_pair_collects_sol_and_injects_advice(monkeypatch, effort, target, metadata_key):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def fake_collect(model, messages, **kwargs):
        assert model == "gpt-6-sol"
        assert kwargs.get("reasoning_effort") == effort
        assert messages == [{"role": "user", "content": "review this design"}]
        return "Check the error path.", Usage(
            prompt_tokens=11, completion_tokens=7, total_tokens=18
        )

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", fake_collect)
    plugin = AdvisorPlugin()
    data = {
        "model": target,
        "messages": [{"role": "user", "content": "review this design"}],
        metadata_key: {"gateway_policy": {
            "active_model": target, "mode": "force", "policy_version": 38,
            "advisor_model": "codex-sol-advisor",
            "advisor_reasoning_effort": effort,
        }},
    }

    run(plugin, data)

    assert "tools" not in data
    assert data["messages"][-1] == {
        "role": "developer",
        "content": "Independent advisor guidance:\nCheck the error path.",
    }
    receipt = data[metadata_key]["gateway_advisor"]
    assert receipt["status"] == "advice_injected"
    assert receipt["model"] == "codex-sol-advisor"
    assert receipt["usage_source"] == "provider"
    assert receipt["input_tokens"] == 11
    assert receipt["output_tokens"] == 7
    assert receipt["reasoning_effort"] == effort


@pytest.mark.parametrize("target", ["codex-subscription", "codex-gpt-6.1-sol", "codex-luna", "gemini-subscription"])
@pytest.mark.parametrize("effort", [None, "high"])
def test_codex_luna_advisor_preconsults_and_injects_advice(monkeypatch, target, effort):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def fake_collect(model, messages, **kwargs):
        assert model == "gpt-6-luna"
        assert kwargs.get("reasoning_effort") == effort
        assert messages == [{"role": "user", "content": "review this design"}]
        return "Check the fallback boundary.", Usage(
            prompt_tokens=13, completion_tokens=8, total_tokens=21
        )

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", fake_collect)
    data = {
        "model": target,
        "messages": [{"role": "user", "content": "review this design"}],
        "metadata": {"gateway_policy": {
            "active_model": target,
            "mode": "force",
            "policy_version": 9,
            "advisor_model": "codex-luna-advisor",
            "advisor_reasoning_effort": effort,
        }},
    }

    run(AdvisorPlugin(), data)

    assert data["messages"][-1] == {
        "role": "developer",
        "content": "Independent advisor guidance:\nCheck the fallback boundary.",
    }
    receipt = data["metadata"]["gateway_advisor"]
    assert receipt["status"] == "advice_injected"
    assert receipt["model"] == "codex-luna-advisor"
    assert receipt["input_tokens"] == 13
    assert receipt["output_tokens"] == 8
    assert receipt["reasoning_effort"] == effort


@pytest.mark.parametrize("alias,expected,effort", [
    ("gemini-subscription", "gemini-3.8-flash", None),
    ("gemini-subscription-pro", "gemini-3.1-pro-low", "low"),
    ("gemini-subscription-3.7-flash", "gemini-3.7-flash-medium", "medium"),
])
@pytest.mark.parametrize("target", ["codex-luna", "minimax"])
def test_preconsult_target_collects_antigravity_advice(monkeypatch, alias, expected, effort, target):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def fake_invoke(model, prompt, *, advisor=False):
        assert advisor is True
        assert model == expected
        assert prompt == "[User]:\nreview this design"
        return "Check the failure handling.", Usage(
            prompt_tokens=9, completion_tokens=6, total_tokens=15
        )

    monkeypatch.setattr(advisor_plugin, "invoke_agy", fake_invoke)
    plugin = AdvisorPlugin()
    data = {
        "model": target,
        "messages": [{"role": "user", "content": "review this design"}],
        "metadata": {"gateway_policy": {
            "active_model": target, "mode": "force", "policy_version": 38,
            "advisor_model": alias,
            "advisor_reasoning_effort": effort,
        }},
    }

    run(plugin, data)

    assert data["messages"][-1]["content"] == (
        "Independent advisor guidance:\nCheck the failure handling."
    )
    receipt = data["metadata"]["gateway_advisor"]
    assert receipt["status"] == "advice_injected"
    assert receipt["model"] == alias
    assert receipt["usage_source"] == "provider"


def test_minimax_gemini_pair_injects_advice_before_main_call(monkeypatch):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def fake_invoke(model, prompt, *, advisor=False):
        assert advisor is True
        assert model == "gemini-3.8-flash-medium"
        assert prompt == "[User]:\nreview this design"
        return "Use isolated fixtures and deterministic state.", Usage(
            prompt_tokens=9, completion_tokens=7, total_tokens=16
        )

    monkeypatch.setattr(advisor_plugin, "invoke_agy", fake_invoke)
    data = {
        "model": "minimax",
        "messages": [{"role": "user", "content": "review this design"}],
        "metadata": {"gateway_policy": {
            "active_model": "minimax", "mode": "force", "policy_version": 40,
            "advisor_model": "gemini-subscription",
            "advisor_reasoning_effort": "medium",
        }},
    }

    run(AdvisorPlugin(), data)

    assert data["messages"][-1] == {
        "role": "developer",
        "content": "Independent advisor guidance:\nUse isolated fixtures and deterministic state.",
    }
    assert data["metadata"]["gateway_advisor"]["status"] == "advice_injected"
    assert data["metadata"]["gateway_advisor"]["usage_source"] == "provider"


def test_minimax_gemini_pair_skips_structured_content_without_changing_request(monkeypatch):
    from copy import deepcopy
    from subroute.plugins import advisor_plugin

    async def unexpected(*args, **kwargs):
        pytest.fail("Text-only Antigravity must not receive structured content")

    monkeypatch.setattr(advisor_plugin, "invoke_agy", unexpected)
    messages = [{"role": "user", "content": [{
        "type": "image", "source": {"type": "base64", "data": "opaque"}
    }]}]
    data = {
        "model": "minimax",
        "messages": messages,
        "metadata": {"gateway_policy": {
            "advisor_model": "gemini-subscription",
            "advisor_reasoning_effort": "medium",
        }},
    }
    original_messages = deepcopy(messages)

    run(AdvisorPlugin(), data)

    assert data["messages"] == original_messages
    assert data["metadata"]["gateway_advisor"] == {
        "status": "skipped",
        "reason": "unsupported_content",
        "model": "gemini-subscription",
    }


@pytest.mark.parametrize("advisor_model", ["codex-gpt-6.1-sol-advisor", "codex-luna-advisor"])
def test_codex_advisor_is_injected_for_supported_tool_history(advisor_model):
    plugin = AdvisorPlugin()
    existing_tools = [{"name": "read_file", "input_schema": {"type": "object"}}]
    data = {
        "model": "minimax",
        "messages": [
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "toolu_1", "content": "contents"
            }]},
        ],
        "tools": existing_tools.copy(),
    }

    data["metadata"] = {"gateway_policy": {"advisor_model": advisor_model}}
    run(plugin, data)

    assert data["tools"][0] == existing_tools[0]
    assert data["tools"][1]["type"] == ADVISOR_TOOL_TYPE
    assert data["tools"][1]["model"] == advisor_model
    assert "gateway_advisor" not in data.get("metadata", {})


@pytest.mark.parametrize("advisor_model", ["gemini-subscription", "codex-gpt-6.1-sol-advisor"])
def test_unsupported_tool_history_skips_only_advisor(advisor_model):
    plugin = AdvisorPlugin()
    original_tool = {"name": "read_file", "input_schema": {"type": "object"}}
    data = {
        "model": "minimax",
        "messages": [{"role": "tool", "tool_call_id": "unmatched", "content": "contents"}],
        "tools": [original_tool.copy()],
    }

    data["metadata"] = {"gateway_policy": {"advisor_model": advisor_model}}
    run(plugin, data)

    assert data["messages"][0]["content"] == "contents"
    assert data["tools"] == [original_tool]
    assert data["metadata"]["gateway_advisor"] == {
        "status": "skipped",
        "reason": "unsupported_tool_history",
        "model": advisor_model,
    }


@pytest.mark.parametrize(
    "model,call_type",
    [
        ("minimax", "anthropic_messages"),
        ("prefix-minimax-suffix", "anthropic_messages"),
        ("minimax", "acompletion"),
        ("minimax", "aresponses"),
    ],
)
def test_non_target_requests_are_unchanged(model: str, call_type: str):
    plugin = AdvisorPlugin()
    data = {"model": model, "messages": []}

    assert run(plugin, data, call_type) is None
    assert "tools" not in data


@pytest.mark.parametrize("max_uses", [0, 6])
def test_rejects_max_uses_outside_litellm_hard_limit(max_uses: int):
    with pytest.raises(ValueError, match="between 1.*5"):
        AdvisorPlugin(max_uses=max_uses)


@pytest.mark.parametrize("advisor_model", [None, ""])
@pytest.mark.parametrize("target", ["minimax", "openai", "openrouter", "codex-luna", "gemini-subscription-pro"])
def test_empty_advisor_never_injects_tools_or_calls_a_provider(monkeypatch, advisor_model, target):
    from copy import deepcopy
    from subroute.plugins import advisor_plugin

    async def unexpected(*args, **kwargs):
        pytest.fail("Disabled advisor must not call a provider")

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", unexpected)
    monkeypatch.setattr(advisor_plugin, "invoke_agy", unexpected)
    data = {"model": target, "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"gateway_policy": {"advisor_model": advisor_model}}}
    original = deepcopy(data)
    assert run(AdvisorPlugin(), data) is None
    assert data == original


@pytest.mark.parametrize("mode", ["alias", "force", "off"])
@pytest.mark.parametrize("target", ["openai", "openrouter", "minimax"])
def test_advisor_selection_is_independent_of_target_alias_and_routing_mode(mode, target):
    data = {"model": target, "messages": [], "metadata": {"gateway_policy": {
        "active_model": "different-target", "mode": mode, "advisor_model": "codex-sol-advisor",
    }}}
    run(AdvisorPlugin(), data)
    assert data["tools"][0]["model"] == "codex-sol-advisor"


def test_advisor_subcall_never_consults_another_advisor():
    data = {"model": "gemini-subscription", "metadata": {
        "advisor_sub_call": True, "gateway_policy": {"advisor_model": "codex-sol-advisor"},
    }}
    assert run(AdvisorPlugin(), data) is None
    assert "tools" not in data


def test_advisor_compaction_falls_back_to_luna_when_minimax_summary_is_over_target(monkeypatch):
    from litellm.types.utils import Usage
    from litellm.proxy import proxy_server

    calls = []

    async def count_context(_router, _data, messages):
        if not any(
            message.get("role") == "assistant"
            and "Relevant prior context" in str(message.get("content", ""))
            for message in messages
        ):
            return 40000
        return 26000 if "minimax summary" in str(messages) else 10000

    async def minimax(_router, messages):
        calls.append(("minimax", messages))
        return "minimax summary", Usage(prompt_tokens=40000, completion_tokens=7000, total_tokens=47000)

    async def luna(messages):
        calls.append(("luna", messages))
        return "concise Luna summary", Usage(prompt_tokens=40000, completion_tokens=900, total_tokens=40900)

    monkeypatch.setattr(AdvisorPlugin, "_count_context_tokens", staticmethod(count_context))
    monkeypatch.setattr(AdvisorPlugin, "_minimax_compaction", staticmethod(minimax))
    monkeypatch.setattr(AdvisorPlugin, "_luna_compaction", staticmethod(luna))
    messages = [
        {"role": "user", "content": "earlier goal"},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "current question"},
    ]

    compacted, receipt = asyncio.run(AdvisorPlugin._prepare_advisor_messages(
        proxy_server.llm_router, "codex-sol-advisor", {"model": "codex-sol-advisor"}, messages,
    ))

    assert [call[0] for call in calls] == ["minimax", "luna"]
    assert receipt["provider"] == "codex-luna-advisor"
    assert [attempt["status"] for attempt in receipt["attempts"]] == ["over_budget", "succeeded"]
    assert receipt["attempts"][0]["usage_source"] == "provider"
    assert receipt["attempts"][1]["output_tokens"] == 900
    assert compacted[-1] is messages[-1]
    assert "concise Luna summary" in compacted[-2]["content"]


def test_advisor_compaction_does_not_call_providers_when_context_is_under_limit(monkeypatch):
    from litellm.proxy import proxy_server

    async def count_context(_router, _data, _messages):
        return 32000

    async def unexpected(*_args, **_kwargs):
        pytest.fail("Compaction providers must not run under the Advisor limit")

    monkeypatch.setattr(AdvisorPlugin, "_count_context_tokens", staticmethod(count_context))
    monkeypatch.setattr(AdvisorPlugin, "_minimax_compaction", staticmethod(unexpected))
    monkeypatch.setattr(AdvisorPlugin, "_luna_compaction", staticmethod(unexpected))
    messages = [{"role": "user", "content": "current question"}]

    compacted, receipt = asyncio.run(AdvisorPlugin._prepare_advisor_messages(
        proxy_server.llm_router, "codex-sol-advisor", {"model": "codex-sol-advisor"}, messages,
    ))

    assert compacted is messages
    assert receipt is None


def test_luna_compaction_uses_supported_timeout_and_reasoning_only(monkeypatch):
    from subroute.plugins import advisor_plugin

    calls = []

    async def capture(model, messages, **kwargs):
        calls.append((model, messages, kwargs))
        return "briefing", None

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", capture)
    result = asyncio.run(AdvisorPlugin._luna_compaction([
        {"role": "user", "content": "current question"},
    ]))

    assert result[0] == "briefing"
    assert calls[0][0] == "gpt-6-luna"
    assert calls[0][2] == {"timeout": 60.0, "reasoning_effort": "low"}
