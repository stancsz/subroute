import asyncio

import pytest
from fastapi import HTTPException

from subroute.plugins.advisor_plugin import (
    ADVISOR_BRIEF_INSTRUCTIONS,
    AdvisorContextCompactionError,
    AdvisorPlugin,
)


@pytest.fixture(autouse=True)
def advisor_router(monkeypatch):
    """Supply the live router services required by preconsult and advisor subcalls."""
    from litellm.proxy import proxy_server
    from subroute.plugins import advisor_plugin

    async def unexpected_provider(*args, **kwargs):
        pytest.fail("Unit tests must mock every advisor transport, including fallback providers")

    from subroute.advisor_ranker import AdvisorRequestRanker
    from subroute.advisor_frequency import AdvisorFrequencyGate

    async def score(self, prompt):
        return .8

    monkeypatch.setattr(AdvisorRequestRanker, "rank", score)
    monkeypatch.setattr(AdvisorFrequencyGate, "choose", lambda self, *args: {
        "consult": True, "score": .8, "target_rate": .5,
        "selected_count": 1, "eligible_count": 1,
    })
    monkeypatch.setattr(advisor_plugin, "invoke_agy", unexpected_provider)
    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", unexpected_provider)

    class Router:
        supplemental_calls = []

        def get_model_info(self, model):
            if model == "claude-opus-5-5-low-advisor":
                return {"advisor_max_input_tokens": 32000, "auto_compact_token_limit": 25600}
            if model == "gemini-subscription-argon":
                return {"advisor_max_input_tokens": 32000, "auto_compact_token_limit": 25600}
            return {"advisor_max_input_tokens": 32000, "auto_compact_token_limit": 25600} if model.endswith("-advisor") else {}

        def get_configured_token_limits(self, model):
            return (32000, None) if model.endswith("-advisor") else (256000, None)

        def _count_pre_call_check_tokens(self, *, messages, input, request_kwargs):
            if request_kwargs.get("model", "").endswith("-advisor") or request_kwargs.get("model") == "gemini-subscription-argon":
                def count_content(content):
                    if isinstance(content, str):
                        return len(content)
                    if isinstance(content, list):
                        return sum(count_content(part) for part in content)
                    if isinstance(content, dict):
                        if content.get("type") == "text" and isinstance(content.get("text"), str):
                            return len(content["text"])
                        if content.get("type") in {"image_url", "input_image"}:
                            return 768
                        return sum(count_content(value) for value in content.values())
                    return 0

                return sum(count_content(message.get("content")) for message in messages)
            return 1

        async def acompletion(self, **kwargs):
            from types import SimpleNamespace
            from litellm.types.utils import Usage
            self.supplemental_calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="Check independently."))],
                usage=Usage(prompt_tokens=17, completion_tokens=5, total_tokens=22),
            )

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
    assert data["messages"][:-2] == messages
    assert data["messages"][-2] == {"role": "developer", "content": "Independent advisor guidance:\nFallback guidance"}
    assert data["messages"][-1]["content"] == "Supplemental independent review (Claude Opus 5.5 Low):\nCheck independently."
    receipt = data["metadata"]["gateway_advisor"]
    assert receipt["status"] == "advice_injected" and receipt["resolved_model"] == "codex-gpt-6.1-sol-advisor"
    assert receipt["usage_source"] == "provider" and receipt["input_tokens"] == 31 and receipt["output_tokens"] == 7
    assert receipt["fallback_attempt"] == {"failed_model": "gemini-subscription", "reason": str(advisor_plugin_error(error_kind))}


def test_ranked_preconsults_responses_and_preserves_instructions(monkeypatch):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def fake_collect(model, messages, **kwargs):
        assert model == "gpt-6-luna"
        assert messages == [
            {"role": "developer", "content": ADVISOR_BRIEF_INSTRUCTIONS},
            {"role": "developer", "content": "Keep answers concise."},
            {"role": "user", "content": "x" * 1200},
        ]
        return "Check the failure path.", Usage(prompt_tokens=10, completion_tokens=4, total_tokens=14)

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", fake_collect)
    data = {
        "model": "openrouter",
        "instructions": "Keep answers concise.",
        "input": "x" * 1200,
        "metadata": {"gateway_policy": {
            "advisor_model": "codex-luna-advisor", "advisor_trigger": "adaptive",
            "advisor_sensitivity": "balanced", "advisor_classifier": "ranked",
        }},
    }

    run(AdvisorPlugin(), data, "aresponses")

    assert data["instructions"] == (
        "Keep answers concise.\n\nIndependent advisor guidance:\nCheck the failure path."
        "\n\nSupplemental independent review (Claude Opus 5.5 Low):\nCheck independently."
    )
    assert data["input"] == "x" * 1200
    assert "tools" not in data
    assert data["metadata"]["gateway_advisor"]["status"] == "advice_injected"
    assert data["metadata"]["gateway_advisor"]["call_type"] == "aresponses"


@pytest.mark.parametrize("call_type,data", [
    ("acompletion", {"messages": [{"role": "user", "content": "simple"}]}),
    ("aresponses", {"input": "simple"}),
])
def test_ranked_skips_unselected_chat_and_responses(call_type, data, monkeypatch):
    from copy import deepcopy
    from subroute.plugins import advisor_plugin

    async def unexpected(*args, **kwargs):
        pytest.fail("Below-threshold Chat and Responses requests must not preconsult")

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", unexpected)
    monkeypatch.setattr(advisor_plugin, "invoke_agy", unexpected)
    monkeypatch.setattr(advisor_plugin.AdvisorFrequencyGate, "choose", lambda self, *args: {
        "consult": False, "score": .2, "target_rate": .7,
        "selected_count": 0, "eligible_count": 1,
    })
    original = deepcopy(data)
    data.update({"model": "openrouter", "metadata": {"gateway_policy": {
        "advisor_model": "codex-luna-advisor", "advisor_trigger": "adaptive",
        "advisor_sensitivity": "frequent", "advisor_classifier": "ranked",
    }}})

    run(AdvisorPlugin(), data, call_type)

    assert {key: value for key, value in data.items() if key != "metadata"} == {
        **original, "model": "openrouter",
    }
    assert data["metadata"]["gateway_advisor"]["status"] == "skipped"
    assert data["metadata"]["gateway_advisor"]["reason"] == "not_selected_by_ranking"
    assert data["metadata"]["gateway_advisor"]["call_type"] == call_type


def test_responses_previous_response_id_skips_without_partial_context(monkeypatch):
    from subroute.plugins import advisor_plugin

    async def unexpected(*args, **kwargs):
        pytest.fail("Unresolvable Responses state must not trigger an advisor call")

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", unexpected)
    monkeypatch.setattr(advisor_plugin, "invoke_agy", unexpected)
    data = {
        "model": "openrouter", "previous_response_id": "resp_prior", "input": "Review this",
        "metadata": {"gateway_policy": {
            "advisor_model": "codex-luna-advisor", "advisor_trigger": "adaptive",
        }},
    }

    run(AdvisorPlugin(), data, "aresponses")

    assert data["previous_response_id"] == "resp_prior"
    assert data["input"] == "Review this"
    assert data["metadata"]["gateway_advisor"]["status"] == "skipped"
    assert data["metadata"]["gateway_advisor"]["reason"] == "unsupported_request_context"


def test_gateway_trigger_consults_chat_and_skips_unsupported_gemini_content(monkeypatch):
    from subroute.plugins import advisor_plugin
    from litellm.types.utils import Usage

    async def fake_invoke(model, prompt, *, advisor=False):
        assert model == "gemini-3.8-flash"
        assert advisor is True
        return "Check the edge case.", Usage(prompt_tokens=6, completion_tokens=4, total_tokens=10)

    monkeypatch.setattr(advisor_plugin, "invoke_agy", fake_invoke)
    data = {"model": "openrouter", "messages": [{"role": "user", "content": "x" * 1200}],
            "metadata": {"gateway_policy": {"advisor_model": "gemini-subscription", "advisor_trigger": "adaptive"}}}
    run(AdvisorPlugin(), data, "acompletion")
    assert data["messages"][-2]["content"] == "Independent advisor guidance:\nCheck the edge case."
    assert data["messages"][-1]["content"] == "Supplemental independent review (Claude Opus 5.5 Low):\nCheck independently."
    assert data["metadata"]["gateway_advisor"]["call_type"] == "acompletion"
    assert "tools" not in data

    data["messages"] = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    run(AdvisorPlugin(), data)
    assert data["metadata"]["gateway_advisor"]["reason"] == "unsupported_content"
    assert "tools" not in data


@pytest.mark.parametrize("effort", [None, "high"])
@pytest.mark.parametrize("target", ["codex-luna", "gemini-subscription-pro"])
@pytest.mark.parametrize("metadata_key", ["metadata", "litellm_metadata"])
def test_codex_subscription_pair_collects_sol_and_injects_advice(monkeypatch, effort, target, metadata_key):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def fake_collect(model, messages, **kwargs):
        assert model == "gpt-6-sol"
        assert kwargs.get("reasoning_effort") == effort
        assert messages == [
            {"role": "developer", "content": ADVISOR_BRIEF_INSTRUCTIONS},
            {"role": "user", "content": "review this design"},
        ]
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
    assert data["messages"][-2] == {
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
    supplemental = data[metadata_key]["gateway_advisor_supplemental"]
    assert supplemental["status"] == "succeeded"
    assert supplemental["provider"] == "antigravity"
    assert supplemental["model"] == "claude-opus-5-5-low"
    assert supplemental["usage_source"] == "provider"
    assert supplemental["input_tokens"] == 17
    assert supplemental["consultation_id"] == receipt["consultation_id"]
    assert supplemental["supplemental_call_id"]


@pytest.mark.parametrize("target", ["codex-subscription", "codex-gpt-6.1-sol", "codex-luna", "gemini-subscription"])
@pytest.mark.parametrize("effort", [None, "high"])
def test_codex_luna_advisor_preconsults_and_injects_advice(monkeypatch, target, effort):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def fake_collect(model, messages, **kwargs):
        assert model == "gpt-6-luna"
        assert kwargs.get("reasoning_effort") == effort
        assert messages == [
            {"role": "developer", "content": ADVISOR_BRIEF_INSTRUCTIONS},
            {"role": "user", "content": "review this design"},
        ]
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

    assert data["messages"][-2] == {
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
        assert prompt == f"[Developer]:\n{ADVISOR_BRIEF_INSTRUCTIONS}\n\n[User]:\nreview this design"
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

    assert data["messages"][-2]["content"] == "Independent advisor guidance:\nCheck the failure handling."
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
        assert prompt == f"[Developer]:\n{ADVISOR_BRIEF_INSTRUCTIONS}\n\n[User]:\nreview this design"
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

    assert data["messages"][-2] == {
        "role": "developer",
        "content": "Independent advisor guidance:\nUse isolated fixtures and deterministic state.",
    }
    assert data["metadata"]["gateway_advisor"]["status"] == "advice_injected"
    assert data["metadata"]["gateway_advisor"]["usage_source"] == "provider"


def test_preconsult_brief_preserves_target_conversation(monkeypatch):
    from copy import deepcopy
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    messages = [
        {"role": "system", "content": "Keep this constraint."},
        {"role": "user", "content": "Earlier requirement."},
        {"role": "assistant", "content": "Earlier decision."},
        {"role": "user", "content": "Review this change."},
    ]
    original = deepcopy(messages)

    async def collect(model, advisor_messages, **kwargs):
        assert model == "gpt-6-luna"
        assert advisor_messages[0] == {"role": "developer", "content": ADVISOR_BRIEF_INSTRUCTIONS}
        assert advisor_messages[1:] == original
        return "Check the boundary.", Usage(prompt_tokens=12, completion_tokens=5, total_tokens=17)

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", collect)
    data = {"model": "minimax", "messages": messages,
            "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor", "advisor_trigger": "adaptive"}}}

    run(AdvisorPlugin(), data)

    assert messages == original
    assert data["messages"][:-2] == original
    assert data["messages"][-2]["content"] == "Independent advisor guidance:\nCheck the boundary."
    assert data["messages"][-1]["content"] == "Supplemental independent review (Claude Opus 5.5 Low):\nCheck independently."


def test_supplemental_failure_blocks_base_dispatch_and_is_not_retried(monkeypatch, advisor_router):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def primary(*args, **kwargs):
        return "Primary guidance", Usage(prompt_tokens=12, completion_tokens=4, total_tokens=16)

    async def fail_supplemental(**kwargs):
        advisor_router.supplemental_calls.append(kwargs)
        raise RuntimeError("supplemental unavailable")

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", primary)
    monkeypatch.setattr(advisor_router, "acompletion", fail_supplemental)
    data = {
        "model": "openrouter",
        "messages": [{"role": "user", "content": "review"}],
        "litellm_call_id": "supplemental-fail-test",
        "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
    }

    with pytest.raises(HTTPException) as caught:
        run(AdvisorPlugin(), data)

    assert caught.value.status_code == 502
    assert caught.value.detail["error"]["type"] == "supplemental_advisor_failed"
    assert len(advisor_router.supplemental_calls) == 1
    assert advisor_router.supplemental_calls[0]["num_retries"] == 0
    assert data["metadata"]["gateway_advisor"]["status"] == "advice_ready"
    assert data["metadata"]["gateway_advisor_supplemental"]["status"] == "failed"
    assert len(data["messages"]) == 1


def test_supplemental_compaction_bounds_exact_dispatched_messages_and_receipt(monkeypatch, advisor_router):
    from copy import deepcopy
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    messages = [
        {"role": "system", "content": "Retain this system rule."},
        {"role": "user", "content": "Earlier context. " + "x" * 18000},
        {"role": "assistant", "content": "Earlier answer. " + "y" * 18000},
        {"role": "user", "content": "Latest question: keep this exact turn."},
    ]
    original = deepcopy(messages)
    dispatched = []

    async def primary(*args, **kwargs):
        return "Primary review", Usage(prompt_tokens=9, completion_tokens=3, total_tokens=12)

    async def compact(router, compact_messages):
        assert compact_messages[-1] == original[-1]
        return "Brief prior context.", Usage(prompt_tokens=18010, completion_tokens=4, total_tokens=18014)

    async def completion(**kwargs):
        dispatched.append(deepcopy(kwargs["messages"]))
        from types import SimpleNamespace
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="Supplemental review"))],
            usage=Usage(prompt_tokens=123, completion_tokens=7, total_tokens=130),
        )

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", primary)
    monkeypatch.setattr(AdvisorPlugin, "_minimax_compaction", staticmethod(compact))
    monkeypatch.setattr(advisor_router, "acompletion", completion)
    data = {"model": "openrouter", "messages": messages, "metadata": {
        "gateway_policy": {"advisor_model": "codex-luna-advisor"},
    }}

    run(AdvisorPlugin(), data)

    assert len(dispatched) == 1
    assert dispatched[0][-1] == original[-1]
    assert [message["role"] for message in dispatched[0]] == ["developer", "system", "assistant", "user"]
    estimate = sum(len(message["content"]) for message in dispatched[0] if isinstance(message.get("content"), str))
    assert estimate <= 32_000
    assert estimate < 36_000  # the un-compacted supplemental brief exceeds its configured limit
    receipt = data["metadata"]["gateway_advisor_supplemental"]
    assert receipt["estimated_input_tokens"] == estimate
    assert receipt["original_estimated_input_tokens"] > 32_000
    assert receipt["input_tokens"] == 123
    assert receipt["usage_source"] == "provider"


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
        "call_type": "anthropic_messages",
    }


@pytest.mark.parametrize("advisor_model", ["codex-gpt-6.1-sol-advisor", "codex-luna-advisor"])
def test_codex_advisor_is_injected_for_supported_tool_history(advisor_model, monkeypatch):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def collect(*args, **kwargs):
        return "Check tool result.", Usage(prompt_tokens=9, completion_tokens=3, total_tokens=12)

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", collect)
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
    assert data["tools"] == existing_tools
    assert data["metadata"]["gateway_advisor"]["status"] == "advice_injected"
    assert data["metadata"]["gateway_advisor"]["model"] == advisor_model


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
        "call_type": "anthropic_messages",
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
def test_advisor_selection_is_independent_of_target_alias_and_routing_mode(mode, target, monkeypatch):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def collect(*args, **kwargs):
        return "Check routing.", Usage(prompt_tokens=9, completion_tokens=3, total_tokens=12)

    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", collect)
    data = {"model": target, "messages": [], "metadata": {"gateway_policy": {
        "active_model": "different-target", "mode": mode, "advisor_model": "codex-sol-advisor",
    }}}
    run(AdvisorPlugin(), data)
    assert "tools" not in data
    assert data["metadata"]["gateway_advisor"]["model"] == "codex-sol-advisor"


def test_advisor_compaction_falls_back_to_mimo_when_minimax_summary_is_over_target(monkeypatch):
    from litellm.types.utils import Usage
    from litellm.proxy import proxy_server

    calls = []

    async def count_context(_router, _data, messages):
        if "minimax summary" in str(messages):
            return 26000
        if "concise MiMo summary" in str(messages):
            return 10000
        if "SIFT COMPACTED" in str(messages):
            return 15000
        if not any(
            message.get("role") == "assistant"
            and "Relevant prior context" in str(message.get("content", ""))
            for message in messages
        ):
            return 40000
        return 40000

    async def minimax(_router, messages):
        calls.append(("minimax", messages))
        return "minimax summary", Usage(prompt_tokens=40000, completion_tokens=7000, total_tokens=47000)

    async def mimo(_router, messages, *, multimodal=False):
        assert multimodal is False
        calls.append(("mimo", messages))
        return "concise MiMo summary", Usage(prompt_tokens=40000, completion_tokens=900, total_tokens=40900)

    monkeypatch.setattr(AdvisorPlugin, "_count_context_tokens", staticmethod(count_context))
    async def sift_noop(_router, _data, messages, _target):
        return [*messages[:-2], {"role": "assistant", "content": "SIFT COMPACTED"}, *messages[-1:]], {
            "compacted": True, "input_tokens": 40000, "forwarded_tokens": 15000,
        }

    monkeypatch.setattr(AdvisorPlugin, "_sift_compaction", staticmethod(sift_noop))
    monkeypatch.setattr(AdvisorPlugin, "_minimax_compaction", staticmethod(minimax))
    monkeypatch.setattr(AdvisorPlugin, "_mimo_compaction", staticmethod(mimo))
    messages = [
        {"role": "user", "content": "earlier goal"},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "current question"},
    ]

    compacted, receipt = asyncio.run(AdvisorPlugin._prepare_advisor_messages(
        proxy_server.llm_router, "codex-sol-advisor", {"model": "codex-sol-advisor"}, messages,
    ))

    assert [call[0] for call in calls] == ["minimax", "mimo"]
    assert receipt["provider"] == "mimo-v2.6-flash"
    assert [attempt["status"] for attempt in receipt["attempts"]] == ["compacted", "over_budget", "succeeded"]
    assert receipt["attempts"][1]["usage_source"] == "provider"
    assert receipt["attempts"][2]["output_tokens"] == 900
    assert compacted[-1] is messages[-1]
    assert "concise MiMo summary" in compacted[-2]["content"]


def test_advisor_compaction_uses_sift_before_cheap_model_and_keeps_current_turn(monkeypatch, advisor_router):
    from copy import deepcopy
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    model = "claude-opus-5-5-low-advisor"
    messages = [
        {"role": "system", "content": "Keep the original instruction."},
        {"role": "user", "content": "Earlier failure details. " + "alpha failure evidence. " * 700},
        {"role": "assistant", "content": "Earlier analysis. " + "Preserve this relevant decision. " * 700},
        {"role": "user", "content": "Latest question: retain this exact turn."},
    ]
    original = deepcopy(messages)
    minimax_inputs = []

    async def compact(router, compact_messages):
        minimax_inputs.append(deepcopy(compact_messages))
        return "Brief factual context.", Usage(prompt_tokens=25000, completion_tokens=8, total_tokens=25008)

    monkeypatch.setattr(AdvisorPlugin, "_minimax_compaction", staticmethod(compact))
    data = {"model": model, "messages": messages}
    compacted, receipt = asyncio.run(AdvisorPlugin._prepare_advisor_messages(
        advisor_router, model, data, messages,
    ))

    assert len(minimax_inputs) == 1
    assert minimax_inputs[0] != original
    assert len(str(minimax_inputs[0])) < len(str(original))
    assert receipt["provider"] == "minimax"
    assert receipt["assisted_by"] == ["sift"]
    assert receipt["attempts"][0]["model"] == "sift"
    assert receipt["attempts"][0]["status"] == "compacted"
    assert compacted[-1] == original[-1]
    assert compacted[0] == original[0]


@pytest.mark.parametrize("advisor_model", ["codex-sol-advisor", "gemini-subscription-argon"])
def test_oversized_sol_and_argon_paths_sift_before_summary_and_opus(monkeypatch, advisor_router, advisor_model):
    from copy import deepcopy
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    events = []
    original_sift = AdvisorPlugin._sift_compaction
    original_completion = advisor_router.acompletion
    messages = [
        {"role": "system", "content": "Keep this instruction."},
        {"role": "user", "content": "Earlier failure. " + "failure evidence and context. " * 700},
        {"role": "assistant", "content": "Prior analysis. " + "Retain this relevant decision. " * 700},
        {"role": "user", "content": "Latest question stays exact."},
    ]
    original = deepcopy(messages)

    async def sift(router, data, source, target):
        result = await original_sift(router, data, source, target)
        events.append("sift")
        return result

    async def minimax(_router, source):
        events.append("minimax")
        return "Compact prior facts.", Usage(prompt_tokens=6000, completion_tokens=6, total_tokens=6006)

    async def sol(*_args, **_kwargs):
        events.append("sol")
        return "Sol review.", Usage(prompt_tokens=20, completion_tokens=6, total_tokens=26)

    async def argon(model, _prompt, *, advisor=False):
        assert advisor is True
        assert model.startswith("gemini-4-argon-")
        events.append("argon")
        return "Argon review.", Usage(prompt_tokens=21, completion_tokens=7, total_tokens=28)

    async def opus(**kwargs):
        events.append("opus")
        return await original_completion(**kwargs)

    monkeypatch.setattr(AdvisorPlugin, "_sift_compaction", staticmethod(sift))
    monkeypatch.setattr(AdvisorPlugin, "_minimax_compaction", staticmethod(minimax))
    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", sol)
    monkeypatch.setattr(advisor_plugin, "invoke_agy", argon)
    monkeypatch.setattr(advisor_router, "acompletion", opus)
    data = {"model": "openrouter", "messages": messages, "metadata": {
        "gateway_policy": {
            "advisor_model": advisor_model,
            "advisor_reasoning_effort": "medium",
        },
    }}

    run(AdvisorPlugin(), data)

    primary = "sol" if advisor_model == "codex-sol-advisor" else "argon"
    assert events == ["sift", "minimax", primary, "sift", "minimax", "opus"]
    assert data["metadata"]["gateway_advisor_compaction"]["provider"] == "minimax"
    assert data["metadata"]["gateway_advisor_compaction"]["assisted_by"] == ["sift"]
    supplemental = data["metadata"]["gateway_advisor_supplemental"]
    assert supplemental["compaction"]["assisted_by"] == ["sift"]
    assert supplemental["estimated_input_tokens"] <= 32_000
    assert supplemental["compaction"]["attempts"][0]["model"] == "sift"
    assert messages == original


def test_oversized_image_context_runs_sift_then_mimo_without_dropping_images(monkeypatch, advisor_router):
    from copy import deepcopy
    from litellm.types.utils import Usage

    model = "claude-opus-5-5-low-advisor"
    image = {"type": "image_url", "image_url": {"url": "https://example.test/reference.png", "detail": "high"}}
    latest_image = {"type": "image_url", "image_url": {"url": "https://example.test/latest.png", "detail": "high"}}
    messages = [
        {"role": "system", "content": "Keep this instruction."},
        {"role": "user", "content": [
            {"type": "text", "text": "Earlier visual evidence. " + "Screenshot context remains relevant. " * 600},
            image,
        ]},
        {"role": "assistant", "content": "Prior analysis. " + "Keep the established decision. " * 600},
        {"role": "user", "content": [{"type": "text", "text": "Latest image question remains exact."}, latest_image]},
    ]
    original = deepcopy(messages)
    calls = []

    async def mimo(_router, source, *, multimodal=False):
        assert multimodal is True
        calls.append(deepcopy(source))
        assert any(
            part == image
            for message in source
            for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        )
        return "Brief image context.", Usage(prompt_tokens=20000, completion_tokens=8, total_tokens=20008)

    monkeypatch.setattr(AdvisorPlugin, "_mimo_compaction", staticmethod(mimo))
    data = {"model": model, "messages": messages}
    compacted, receipt = asyncio.run(AdvisorPlugin._prepare_advisor_messages(
        advisor_router, model, data, messages,
    ))

    assert receipt["provider"] == "mimo-v2.5-pro"
    assert receipt["attempts"][0]["model"] == "sift"
    assert receipt["attempts"][0]["status"] == "compacted"
    assert len(calls) == 1
    assert len(str(calls[0])) < len(str(original))
    assert compacted[-1] == original[-1]
    assert sum(
        part == latest_image
        for message in calls[0]
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
    ) == 1
    assert sum(
        part == latest_image
        for message in compacted
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
    ) == 1
    assert any(
        part == image
        for message in compacted
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
    )
    assert messages == original


@pytest.mark.parametrize("sift_behavior", ["raises", "noop", "over_budget"])
def test_advisor_compaction_fails_closed_without_compacted_sift_output(monkeypatch, advisor_router, sift_behavior):
    from litellm.types.utils import Usage

    calls = []

    async def count_context(_router, _data, messages):
        if messages and messages[-1].get("content") == "current question":
            return 40000
        return 40000

    async def sift(_router, _data, messages, target):
        if sift_behavior == "raises":
            raise RuntimeError("sift unavailable")
        audit = {
            "compacted": sift_behavior != "noop",
            "input_tokens": 40000,
            "forwarded_tokens": target + 1 if sift_behavior == "over_budget" else 40000,
        }
        return messages, audit

    async def unexpected(*_args, **_kwargs):
        calls.append("summarizer")
        return "unexpected summarizer output", Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    monkeypatch.setattr(AdvisorPlugin, "_count_context_tokens", staticmethod(count_context))
    monkeypatch.setattr(AdvisorPlugin, "_sift_compaction", staticmethod(sift))
    monkeypatch.setattr(AdvisorPlugin, "_minimax_compaction", staticmethod(unexpected))
    monkeypatch.setattr(AdvisorPlugin, "_mimo_compaction", staticmethod(unexpected))
    messages = [
        {"role": "user", "content": "older context"},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "current question"},
    ]

    with pytest.raises(AdvisorContextCompactionError):
        asyncio.run(AdvisorPlugin._prepare_advisor_messages(
            advisor_router, "codex-sol-advisor", {"model": "codex-sol-advisor"}, messages,
        ))
    assert calls == []


def test_advisor_compaction_does_not_call_providers_when_context_is_under_limit(monkeypatch):
    from litellm.proxy import proxy_server

    async def count_context(_router, _data, _messages):
        return 16384

    async def unexpected(*_args, **_kwargs):
        pytest.fail("Compaction providers must not run under the Advisor limit")

    monkeypatch.setattr(AdvisorPlugin, "_count_context_tokens", staticmethod(count_context))
    monkeypatch.setattr(AdvisorPlugin, "_minimax_compaction", staticmethod(unexpected))
    monkeypatch.setattr(AdvisorPlugin, "_mimo_compaction", staticmethod(unexpected))
    messages = [{"role": "user", "content": "current question"}]

    compacted, receipt = asyncio.run(AdvisorPlugin._prepare_advisor_messages(
        proxy_server.llm_router, "codex-sol-advisor", {"model": "codex-sol-advisor"}, messages,
    ))

    assert compacted is messages
    assert receipt is None


def test_mimo_compaction_is_bounded_and_disables_retry_and_fallback(monkeypatch, advisor_router):
    from types import SimpleNamespace
    from litellm.types.utils import Usage

    calls = []

    async def capture(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="briefing"))],
            usage=Usage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
        )

    monkeypatch.setattr(advisor_router, "acompletion", capture)
    result = asyncio.run(AdvisorPlugin._mimo_compaction(
        advisor_router, [{"role": "user", "content": "compact this"}],
    ))

    assert result[0] == "briefing"
    assert calls[0]["model"] == "mimo-v2.6-flash"
    assert calls[0]["timeout"] == 30.0
    assert calls[0]["num_retries"] == 0
    assert calls[0]["disable_fallbacks"] is True
    assert calls[0]["max_tokens"] == 8192
