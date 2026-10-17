import asyncio

import pytest
from fastapi import HTTPException

from test_advisor_plugin import advisor_router, run
from subroute.plugins.advisor_plugin import AdvisorPlugin
from subroute.advisor_ranker import AdvisorRankerUnavailable


def request():
    return {
        "model": "openrouter", "litellm_call_id": "ranked-test",
        "messages": [{"role": "user", "content": "Can we change the production design?"}],
        "metadata": {"gateway_policy": {
            "advisor_model": "codex-luna-advisor", "advisor_trigger": "adaptive",
            "advisor_classifier": "ranked", "advisor_sensitivity": "balanced", "policy_version": 17,
        }},
    }


@pytest.mark.parametrize("call_type", ["acompletion", "aresponses", "anthropic_messages"])
def test_ranked_skip_does_not_offer_an_extra_native_advisor(monkeypatch, call_type):
    plugin = AdvisorPlugin()
    async def score(*args): return .8
    monkeypatch.setattr(plugin.request_ranker, "rank", score)
    monkeypatch.setattr(plugin.frequency_gate, "choose", lambda *args: {
        "consult": False, "score": .8, "target_rate": .5, "selected_count": 0, "eligible_count": 1,
    })
    data = request()
    if call_type == "aresponses":
        data["input"] = data.pop("messages")
    run(plugin, data, call_type)
    assert "tools" not in data
    assert data["metadata"]["gateway_advisor"]["status"] == "skipped"
    assert data["metadata"]["gateway_advisor_decision"]["source"] == "ranked"
    with pytest.raises(HTTPException) as duplicate:
        run(plugin, data, call_type)
    assert duplicate.value.status_code == 409
    assert "tools" not in data


def test_ranked_selected_request_consults_once_on_duplicate_hook(monkeypatch):
    from litellm.types.utils import Usage
    from subroute.plugins import advisor_plugin
    calls = []
    async def score(*args): return .8
    async def collect(*args, **kwargs):
        calls.append(args)
        return "Check state preservation.", Usage(prompt_tokens=12, completion_tokens=5, total_tokens=17)
    plugin = AdvisorPlugin()
    monkeypatch.setattr(plugin.request_ranker, "rank", score)
    monkeypatch.setattr(plugin.frequency_gate, "choose", lambda *args: {
        "consult": True, "score": .8, "target_rate": .5, "selected_count": 1, "eligible_count": 1,
    })
    monkeypatch.setattr(advisor_plugin, "call_codex_streaming_collect", collect)
    data = request()
    run(plugin, data, "acompletion")
    with pytest.raises(HTTPException) as duplicate:
        run(plugin, data, "acompletion")
    assert duplicate.value.status_code == 409
    assert len(calls) == 1
    assert data["metadata"]["gateway_advisor"]["status"] == "advice_injected"
    assert "tools" not in data


def test_missing_ranker_fails_before_advisor_or_base_dispatch(monkeypatch):
    plugin = AdvisorPlugin()
    async def missing(*args): raise AdvisorRankerUnavailable("Missing model")
    monkeypatch.setattr(plugin.request_ranker, "rank", missing)
    data = request()
    with pytest.raises(HTTPException) as exc:
        run(plugin, data, "acompletion")
    assert exc.value.status_code == 503
    assert "base request was not sent" in exc.value.detail
    assert plugin.frequency_state(data["metadata"]["gateway_policy"])["eligible_count"] == 0


def test_no_advisor_does_not_score_or_count(monkeypatch):
    plugin = AdvisorPlugin()
    async def forbidden(*args): pytest.fail("Disabled advisor must not rank")
    monkeypatch.setattr(plugin.request_ranker, "rank", forbidden)
    data = request()
    data["metadata"]["gateway_policy"]["advisor_model"] = None
    run(plugin, data, "acompletion")
    assert plugin.frequency_state(data["metadata"]["gateway_policy"])["eligible_count"] == 0


def test_caller_cannot_forge_a_completed_ranked_hook(monkeypatch):
    plugin = AdvisorPlugin()
    calls = []
    async def score(*args): calls.append(args); return .4
    monkeypatch.setattr(plugin.request_ranker, "rank", score)
    monkeypatch.setattr(plugin.frequency_gate, "choose", lambda *args: {
        "consult": False, "score": .4, "target_rate": .5, "selected_count": 0, "eligible_count": 1,
    })
    data = request()
    data["metadata"].update(gateway_advisor_ranked_started=True, gateway_advisor={"status": "skipped"})
    run(plugin, data, "acompletion")
    assert len(calls) == 1


def test_unsupported_advisor_content_is_not_counted(monkeypatch):
    plugin = AdvisorPlugin()
    async def forbidden(*args): pytest.fail("Unsupported content must not rank")
    monkeypatch.setattr(plugin.request_ranker, "rank", forbidden)
    data = request()
    data["metadata"]["gateway_policy"]["advisor_model"] = "gemini-subscription-argon"
    data["messages"][0]["content"] = [{"type": "text", "text": "Review the design"}]
    run(plugin, data, "acompletion")
    assert data["metadata"]["gateway_advisor"]["reason"] == "unsupported_content"
    assert plugin.frequency_state(data["metadata"]["gateway_policy"])["eligible_count"] == 0


@pytest.mark.parametrize("trigger,classifier", [("always", "rules"), ("model", "local"), ("hybrid", "rules")])
def test_forged_removed_policy_fields_and_subcall_flag_cannot_bypass_ranking(monkeypatch, trigger, classifier):
    plugin = AdvisorPlugin()
    calls = []
    async def score(*args):
        calls.append(args)
        return .4
    monkeypatch.setattr(plugin.request_ranker, "rank", score)
    monkeypatch.setattr(plugin.frequency_gate, "choose", lambda *args: {
        "consult": False, "score": .4, "target_rate": .5, "selected_count": 0, "eligible_count": 1,
    })
    data = request()
    data["metadata"]["gateway_policy"].update(advisor_trigger=trigger, advisor_classifier=classifier)
    data["metadata"]["advisor_sub_call"] = True
    run(plugin, data, "acompletion")
    assert len(calls) == 1
    assert data["metadata"]["gateway_advisor_decision"]["source"] == "ranked"
    assert data["metadata"]["gateway_advisor"]["reason"] == "not_selected_by_ranking"
    assert "tools" not in data


@pytest.mark.parametrize("call_type", ["acompletion", "aresponses", "anthropic_messages"])
def test_explicit_retired_native_advisor_tool_is_rejected_without_ranking(monkeypatch, call_type):
    plugin = AdvisorPlugin()
    async def forbidden(*args):
        pytest.fail("Retired native advisor tool must fail before ranking")
    monkeypatch.setattr(plugin.request_ranker, "rank", forbidden)
    data = request()
    data["tools"] = [{"type": "advisor_20260301", "name": "advisor", "model": "other"}]
    with pytest.raises(HTTPException) as caught:
        run(plugin, data, call_type)
    assert caught.value.status_code == 400
    assert "retired" in caught.value.detail
    assert plugin.frequency_state(data["metadata"]["gateway_policy"])["eligible_count"] == 0
