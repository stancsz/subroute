import asyncio

import pytest
from litellm.llms.anthropic.experimental_pass_through.messages.interceptors.advisor import (
    AdvisorOrchestrationHandler,
)

from unified_llm_gateway.plugins.advisor_plugin import (
    ADVISOR_TOOL_TYPE,
    AdvisorPlugin,
)


def run(plugin: AdvisorPlugin, data: dict, call_type: str = "anthropic_messages"):
    return asyncio.run(plugin.async_pre_call_hook({}, None, data, call_type))


def test_injects_advisor_for_exact_guided_alias_on_messages_route():
    plugin = AdvisorPlugin(
        advisor_model="gemini-subscription",
        target_model_aliases=frozenset({"minimax-guided"}),
        max_uses=2,
    )
    data = {
        "model": "minimax-guided",
        "messages": [{"role": "user", "content": "review this design"}],
        "tools": [{"name": "existing", "input_schema": {"type": "object"}}],
    }

    result = run(plugin, data)

    assert result is data
    assert data["model"] == "minimax-guided"
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
    plugin = AdvisorPlugin(target_model_aliases=frozenset({"minimax-guided"}))
    existing = {"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "other"}
    data = {"model": "minimax-guided", "tools": [existing]}

    run(plugin, data)

    assert data["tools"] == [existing]


@pytest.mark.parametrize(
    "model,call_type",
    [
        ("minimax", "anthropic_messages"),
        ("prefix-minimax-guided-suffix", "anthropic_messages"),
        ("minimax-guided", "acompletion"),
        ("minimax-guided", "aresponses"),
    ],
)
def test_non_target_requests_are_unchanged(model: str, call_type: str):
    plugin = AdvisorPlugin(target_model_aliases=frozenset({"minimax-guided"}))
    data = {"model": model, "messages": []}

    assert run(plugin, data, call_type) is None
    assert "tools" not in data


@pytest.mark.parametrize("max_uses", [0, 6])
def test_rejects_max_uses_outside_litellm_hard_limit(max_uses: int):
    with pytest.raises(ValueError, match="between 1.*5"):
        AdvisorPlugin(max_uses=max_uses)
