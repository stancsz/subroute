import asyncio

import pytest
from litellm.llms.anthropic.experimental_pass_through.messages.interceptors.advisor import (
    AdvisorOrchestrationHandler,
)

from subroute.plugins.advisor_plugin import (
    ADVISOR_TOOL_TYPE,
    AdvisorPlugin,
)


def run(plugin: AdvisorPlugin, data: dict, call_type: str = "anthropic_messages"):
    return asyncio.run(plugin.async_pre_call_hook({}, None, data, call_type))


def test_selected_advisor_enables_plain_model_on_messages_route():
    plugin = AdvisorPlugin(
        max_uses=2,
    )
    data = {
        "model": "minimax",
        "messages": [{"role": "user", "content": "review this design"}],
        "tools": [{"name": "existing", "input_schema": {"type": "object"}}],
    }

    data["metadata"] = {"gateway_policy": {"advisor_model": "gemini-subscription"}}
    result = run(plugin, data)

    assert result is data
    assert data["model"] == "minimax"
    assert data["tools"][0]["name"] == "existing"
    assert data["tools"][1] == {
        "type": ADVISOR_TOOL_TYPE,
        "name": "advisor",
        "model": "gemini-subscription",
        "max_uses": 2,
        "caching": {"type": "ephemeral", "ttl": "5m"},
    }
    assert AdvisorOrchestrationHandler().can_handle(
        data["tools"], custom_llm_provider="minimax"
    )


def test_does_not_duplicate_existing_advisor_tool():
    plugin = AdvisorPlugin()
    existing = {"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "other"}
    data = {"model": "minimax", "tools": [existing]}

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


@pytest.mark.parametrize("alias,expected,effort", [
    ("gemini-subscription", "gemini-3.8-flash", None),
    ("gemini-subscription-pro", "gemini-3.1-pro-low", "low"),
    ("gemini-subscription-3.7-flash", "gemini-3.7-flash-medium", "medium"),
])
def test_codex_subscription_pair_collects_antigravity_advice(monkeypatch, alias, expected, effort):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin

    async def fake_invoke(model, prompt):
        assert model == expected
        assert prompt == "[User]:\nreview this design"
        return "Check the failure handling.", Usage(
            prompt_tokens=9, completion_tokens=6, total_tokens=15
        )

    monkeypatch.setattr(advisor_plugin, "invoke_agy", fake_invoke)
    plugin = AdvisorPlugin()
    data = {
        "model": "codex-luna",
        "messages": [{"role": "user", "content": "review this design"}],
        "metadata": {"gateway_policy": {
            "active_model": "codex-luna", "mode": "force", "policy_version": 38,
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


def test_codex_advisor_is_injected_for_supported_tool_history():
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

    data["metadata"] = {"gateway_policy": {"advisor_model": "codex-terra-advisor"}}
    run(plugin, data)

    assert data["tools"][0] == existing_tools[0]
    assert data["tools"][1]["type"] == ADVISOR_TOOL_TYPE
    assert "gateway_advisor" not in data.get("metadata", {})


@pytest.mark.parametrize("advisor_model", ["gemini-subscription", "codex-terra-advisor"])
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
