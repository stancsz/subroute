import asyncio
from copy import deepcopy

import pytest

from subroute.plugins.advisor_plugin import (
    ADVISOR_TOOL_TYPE,
    AdvisorPlugin,
    expert_route,
)


def run(plugin: AdvisorPlugin, data: dict, call_type: str = "anthropic_messages"):
    return asyncio.run(plugin.async_pre_call_hook({}, None, data, call_type))


def test_messages_get_optional_advisor_tool_without_changing_caller_payload():
    data = {
        "model": "target",
        "messages": [{"role": "user", "content": "Review this decision."}],
        "max_tokens": 4000,
        "tools": [{"name": "caller_tool", "input_schema": {"type": "object"}}],
        "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
    }
    original = deepcopy(data)

    assert run(AdvisorPlugin(), data) is data
    tool, = [item for item in data["tools"] if item.get("type") == ADVISOR_TOOL_TYPE]
    assert tool["name"] == "advisor"
    assert tool["model"] == "experts/codex-luna-advisor"
    assert tool["max_uses"] == 1
    assert data["tools"][0] == original["tools"][0]
    for key in ("model", "messages", "max_tokens", "metadata"):
        assert data[key] == original[key]


@pytest.mark.parametrize("call_type", ["acompletion", "aresponses"])
def test_other_protocols_never_receive_advisor_tool(call_type):
    data = {
        "model": "target",
        "instructions": "Keep this exact.",
        "input": [{"role": "user", "content": "Review."}],
        "messages": [{"role": "user", "content": "Review."}],
        "tools": [{"type": "function", "name": "caller_tool"}],
        "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
    }
    original = deepcopy(data)
    assert run(AdvisorPlugin(), data, call_type) is None
    assert data == original


def test_disabled_or_missing_policy_does_not_add_tool():
    for metadata in ({}, {"gateway_policy": {"advisor_model": None}}):
        data = {"model": "target", "messages": [], "metadata": metadata}
        original = deepcopy(data)
        assert run(AdvisorPlugin(), data) is None
        assert data == original

    unconfigured = {"tools": [{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "attacker"}]}
    with pytest.raises(Exception, match="requires a configured expert"):
        run(AdvisorPlugin(), unconfigured)


def test_untrusted_advisor_tool_is_replaced_with_fixed_route_and_limit():
    data = {
        "messages": [{"role": "user", "content": "Review."}],
        "tools": [{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "attacker", "max_uses": 99}],
        "metadata": {"gateway_policy": {"advisor_model": "gemini-subscription-argon"}},
    }
    run(AdvisorPlugin(), data)
    tool, = data["tools"]
    assert tool["model"] == "experts/gemini-4-argon-advisor"
    assert tool["max_uses"] == 1


def test_unknown_saved_expert_is_rejected():
    data = {"messages": [], "metadata": {"gateway_policy": {"advisor_model": "unlisted"}}}
    with pytest.raises(Exception, match="not available on the experts service"):
        run(AdvisorPlugin(), data)


def test_native_litellm_tool_consults_only_after_model_selects_it(monkeypatch):
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor as litellm_advisor

    target_calls = []

    async def executor(**kwargs):
        target_calls.append(kwargs)
        if len(target_calls) == 1:
            return {"content": [{"type": "tool_use", "id": "advisor-call-1", "name": "advisor",
                                 "input": {"question": "Check my reasoning."}}]}
        return {"content": [{"type": "text", "text": "Final answer."}]}

    expert_calls = []

    class ExpertRouter:
        async def aanthropic_messages(self, **kwargs):
            expert_calls.append(kwargs)
            return {"content": [{"type": "text", "text": "One concise risk."}]}

    monkeypatch.setattr(litellm_advisor, "_call_messages_handler", executor)
    monkeypatch.setattr(litellm_advisor, "_resolve_advisor_router", lambda model: ExpertRouter())
    data = {
        "model": "target",
        "messages": [{"role": "user", "content": "Review."}],
        "tools": [],
        "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
    }
    run(AdvisorPlugin(), data)
    handler = litellm_advisor.AdvisorOrchestrationHandler()
    result = asyncio.run(handler.handle(
        model="target", messages=data["messages"], tools=data["tools"], stream=False,
        max_tokens=200, custom_llm_provider="openai",
    ))

    assert result["content"][0]["text"] == "Final answer."
    assert len(target_calls) == 2
    assert len(expert_calls) == 1
    assert expert_calls[0]["model"] == "experts/codex-luna-advisor"
    assert expert_calls[0]["messages"][-1]["content"] == "Check my reasoning."


def test_native_messages_without_tool_selection_makes_no_expert_call(monkeypatch):
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor as litellm_advisor

    target_calls = []

    async def executor(**kwargs):
        target_calls.append(kwargs)
        return {"content": [{"type": "text", "text": "Ordinary answer."}]}

    class ExpertRouter:
        async def aanthropic_messages(self, **kwargs):
            pytest.fail("An ordinary response must not call the expert provider")

    monkeypatch.setattr(litellm_advisor, "_call_messages_handler", executor)
    monkeypatch.setattr(litellm_advisor, "_resolve_advisor_router", lambda model: ExpertRouter())
    messages = [{"role": "user", "content": "Answer normally."}]
    data = {
        "messages": messages,
        "tools": [],
        "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
    }
    run(AdvisorPlugin(), data)

    result = asyncio.run(litellm_advisor.AdvisorOrchestrationHandler().handle(
        model="target", messages=messages, tools=data["tools"], stream=False,
        max_tokens=200, custom_llm_provider="openai",
    ))

    assert result["content"][0]["text"] == "Ordinary answer."
    assert len(target_calls) == 1
    assert target_calls[0]["messages"] == messages


def test_selected_advisor_failure_propagates_without_retry(monkeypatch):
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor as litellm_advisor

    target_calls = []

    async def executor(**kwargs):
        target_calls.append(kwargs)
        return {"content": [{"type": "tool_use", "id": "call", "name": "advisor",
                             "input": {"question": "Review."}}]}

    class FailedExpertRouter:
        async def aanthropic_messages(self, **kwargs):
            raise RuntimeError("expert unavailable")

    monkeypatch.setattr(litellm_advisor, "_call_messages_handler", executor)
    monkeypatch.setattr(litellm_advisor, "_resolve_advisor_router", lambda model: FailedExpertRouter())
    data = {"messages": [], "tools": [], "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}}}
    run(AdvisorPlugin(), data)
    with pytest.raises(RuntimeError, match="expert unavailable"):
        asyncio.run(litellm_advisor.AdvisorOrchestrationHandler().handle(
            model="target", messages=[], tools=data["tools"], stream=False,
            max_tokens=100, custom_llm_provider="openai",
        ))
    assert len(target_calls) == 1


@pytest.mark.parametrize("advisor,route", [
    ("codex-gpt-6.1-sol-advisor", "experts/codex-gpt-6.1-sol-advisor"),
    ("gemini-subscription", "experts/gemini-3.8-flash-advisor"),
    ("gemini-subscription-argon", "experts/gemini-4-argon-advisor"),
    ("gemini-subscription-3.7-flash", "experts/gemini-3.7-flash-advisor"),
    ("gemini-subscription-3.6-flash", "experts/gemini-3.6-flash-advisor"),
    ("gemini-subscription-pro", "experts/gemini-3.1-pro-advisor"),
])
def test_expert_route_is_fixed_and_allowlisted(advisor, route):
    assert expert_route(advisor) == route
