import asyncio
from copy import deepcopy

import pytest

from subroute.plugins.advisor_plugin import (
    ADVISOR_TOOL_TYPE,
    AdvisorPlugin,
    TARGET_SYSTEM_PROMPT,
    expert_route,
)


def run(plugin: AdvisorPlugin, data: dict, call_type: str = "anthropic_messages"):
    return asyncio.run(plugin.async_pre_call_hook({}, None, data, call_type))


def prepare_message_request(plugin: AdvisorPlugin, data: dict):
    async def prepare():
        hook_result = await plugin.async_pre_call_hook({}, None, data, "anthropic_messages")
        request = await plugin.async_pre_request_hook(
            data.get("model", "target"), data.get("messages", []),
            {"tools": data.get("tools", [])},
        )
        return hook_result, request

    return asyncio.run(prepare())


def test_messages_get_optional_advisor_tool_and_default_system_prompt():
    data = {
        "model": "target",
        "messages": [{"role": "user", "content": "Review this decision."}],
        "max_tokens": 4000,
        "tools": [{"name": "caller_tool", "input_schema": {"type": "object"}}],
        "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
    }
    original = deepcopy(data)

    plugin = AdvisorPlugin()
    hook_result, request = prepare_message_request(plugin, data)
    assert hook_result is None
    tool, = [item for item in request["tools"] if item.get("type") == ADVISOR_TOOL_TYPE]
    assert tool["name"] == "advisor"
    assert tool["model"] == "experts/codex-luna-advisor"
    assert tool["max_uses"] == 1
    assert request["tools"][0] == original["tools"][0]
    assert data == {**original, "system": TARGET_SYSTEM_PROMPT}


def test_pre_request_advisor_policy_is_request_local_and_consumed():
    plugin = AdvisorPlugin()

    async def scenario():
        async def request(advisor_model):
            data = {
                "model": "target",
                "messages": [{"role": "user", "content": "Review."}],
                "metadata": {"gateway_policy": {"advisor_model": advisor_model}},
            }
            await plugin.async_pre_call_hook({}, None, data, "anthropic_messages")
            await asyncio.sleep(0)
            result = await plugin.async_pre_request_hook("target", data["messages"], {"tools": []})
            next_request = await plugin.async_pre_request_hook("target", [], {"tools": []})
            return result, next_request

        async def advisor_sub_call():
            data = {
                "model": "experts/codex-luna-advisor",
                "metadata": {
                    "gateway_policy": {"advisor_model": "codex-luna-advisor"},
                    "advisor_sub_call": True,
                },
            }
            await plugin.async_pre_call_hook({}, None, data, "anthropic_messages")
            return await plugin.async_pre_request_hook(data["model"], [], {"tools": []})

        async def unsupported_call_type():
            data = {
                "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
            }
            await plugin.async_pre_call_hook({}, None, data, "anthropic_messages")
            await plugin.async_pre_call_hook({}, None, data, "acompletion")
            return await plugin.async_pre_request_hook("target", [], {"tools": []})

        first, second, sub_call = await asyncio.gather(
            request("codex-luna-advisor"),
            request("gemini-subscription"),
            advisor_sub_call(),
        )
        unsupported = await unsupported_call_type()
        return first, second, sub_call, unsupported

    (first, first_followup), (second, second_followup), sub_call, unsupported = asyncio.run(scenario())

    def advisor_route(result):
        tool, = [tool for tool in result["tools"] if tool.get("type") == ADVISOR_TOOL_TYPE]
        return tool["model"]

    assert advisor_route(first) == "experts/codex-luna-advisor"
    assert advisor_route(second) == "experts/gemini-3.8-flash-advisor"
    assert first_followup is second_followup is sub_call is unsupported is None


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
    if call_type == "acompletion":
        assert data == {**original, "messages": [
            {"role": "system", "content": TARGET_SYSTEM_PROMPT}, *original["messages"],
        ]}
    else:
        assert data == {**original, "instructions": f"{TARGET_SYSTEM_PROMPT}\n\nKeep this exact."}


def test_disabled_or_missing_policy_does_not_add_tool():
    for metadata in ({}, {"gateway_policy": {"advisor_model": None}}):
        data = {"model": "target", "messages": [], "metadata": metadata}
        original = deepcopy(data)
        assert run(AdvisorPlugin(), data) is None
        assert data == {**original, "system": TARGET_SYSTEM_PROMPT}

    unconfigured = {"tools": [{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "attacker"}]}
    with pytest.raises(Exception, match="Requested Advisor model is not available"):
        run(AdvisorPlugin(), unconfigured)


def test_target_prompt_is_once_and_excludes_private_advisor_requests():
    plugin = AdvisorPlugin()
    target = {"model": "target", "system": [{"type": "text", "text": "Keep this."}]}
    run(plugin, target)
    run(plugin, target)
    assert target["system"] == [
        {"type": "text", "text": TARGET_SYSTEM_PROMPT},
        {"type": "text", "text": "Keep this."},
    ]
    embedded = {"model": "target", "system": [{
        "type": "text", "text": f"Header\n{TARGET_SYSTEM_PROMPT}\nFooter",
    }]}
    run(plugin, embedded)
    assert len(embedded["system"]) == 1
    chat = {"model": "target", "messages": [{
        "role": "system", "content": f"Header\n{TARGET_SYSTEM_PROMPT}\nFooter",
    }]}
    run(plugin, chat, "acompletion")
    assert len(chat["messages"]) == 1

    for model, metadata in (
        ("experts/codex-luna-advisor", {}),
        ("codex-luna-advisor", {}),
        ("target", {"advisor_sub_call": True}),
    ):
        data = {"model": model, "messages": [{"role": "user", "content": "Check."}],
                "metadata": metadata}
        original = deepcopy(data)
        run(plugin, data)
        assert data == original


def test_request_selected_advisor_uses_allowlisted_route_and_fixed_limit():
    data = {
        "messages": [{"role": "user", "content": "Review."}],
        "tools": [{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "gemini-subscription-argon", "max_uses": 99}],
        "metadata": {"gateway_policy": {"advisor_model": "gemini-subscription-argon"}},
    }
    plugin = AdvisorPlugin()
    hook_result, request = prepare_message_request(plugin, data)
    assert hook_result is None
    tool, = request["tools"]
    assert tool["model"] == "experts/gemini-4-argon-advisor"
    assert tool["max_uses"] == 1


@pytest.mark.parametrize(("requested", "expected"), [
    ("gemini-subscription", "experts/gemini-3.8-flash-advisor"),
    ("gemini-3.8-flash-advisor", "experts/gemini-3.8-flash-advisor"),
    ("gemini-4-argon-advisor", "experts/gemini-4-argon-advisor"),
    ("codex-sol-advisor", "experts/codex-sol-advisor"),
    ("claude-opus-5-5-low-advisor", "experts/claude-opus-5-5-low-advisor"),
])
def test_advisor_tool_can_choose_an_allowlisted_expert_per_request(requested, expected):
    data = {
        "model": "target",
        "messages": [{"role": "user", "content": "Review this."}],
        "tools": [{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": requested}],
        "metadata": {"gateway_policy": {"advisor_model": "codex-gpt-6.1-sol-advisor"}},
    }
    _, request = prepare_message_request(AdvisorPlugin(), data)
    tool, = [item for item in request["tools"] if item.get("type") == ADVISOR_TOOL_TYPE]
    assert tool["model"] == expected
    assert tool["max_uses"] == 1


def test_request_selected_expert_works_without_a_saved_default():
    data = {
        "messages": [{"role": "user", "content": "Review this."}],
        "tools": [{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "gemini-subscription"}],
        "metadata": {"gateway_policy": {"advisor_model": None}},
    }
    _, request = prepare_message_request(AdvisorPlugin(), data)
    tool, = [item for item in request["tools"] if item.get("type") == ADVISOR_TOOL_TYPE]
    assert tool["model"] == "experts/gemini-3.8-flash-advisor"


def test_unlisted_request_selected_expert_is_rejected():
    data = {
        "messages": [],
        "tools": [{"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "experts/private"}],
        "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
    }
    with pytest.raises(Exception, match="Requested Advisor model is not available"):
        run(AdvisorPlugin(), data)


def test_unknown_saved_expert_is_rejected():
    data = {"messages": [], "metadata": {"gateway_policy": {"advisor_model": "unlisted"}}}
    plugin = AdvisorPlugin()
    with pytest.raises(Exception, match="not available on the experts service"):
        prepare_message_request(plugin, data)


@pytest.mark.parametrize("select_advisor", [False, True])
def test_native_litellm_tool_consults_only_after_model_selects_it(monkeypatch, select_advisor):
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor as litellm_advisor
    from litellm.proxy import proxy_server
    from subroute.plugins.expert_sift import ExpertSiftHook

    target_calls = []
    events = []
    sift_hook = ExpertSiftHook()
    sift_hook.model_targets["codex-luna-advisor"] = 256
    sift_hook.max_target = 512

    class CountingRouter:
        @staticmethod
        def _count_pre_call_check_tokens(*, messages, input, request_kwargs):
            return sum(len(str(message.get("content", ""))) for message in messages or []) // 4

    monkeypatch.setattr(proxy_server, "llm_router", CountingRouter())

    async def executor(**kwargs):
        target_calls.append(deepcopy(kwargs))
        if len(target_calls) == 1 and select_advisor:
            events.append("target_selected_advisor")
            return {"content": [{"type": "tool_use", "id": "advisor-call-1", "name": "advisor",
                                 "input": {"question": "Check my reasoning."}}]}
        events.append("target_final" if select_advisor else "target_no_selection")
        return {"content": [{"type": "text", "text": "Final answer."}]}

    expert_calls = []
    sift_inputs = []

    class ExpertRouter:
        async def aanthropic_messages(self, **kwargs):
            events.append("sift")
            sift_inputs.append((kwargs.get("model"), deepcopy(kwargs["messages"])))
            request = {
                "model": "codex-luna-advisor",
                "messages": kwargs["messages"],
                "max_tokens": 64,
            }
            result = await sift_hook.async_pre_call_hook({}, None, request, "aanthropic_messages")
            expert_calls.append({"expert_route": kwargs.get("model"), **(result or request)})
            return {"content": [{"type": "text", "text": "One concise risk."}]}

    monkeypatch.setattr(litellm_advisor, "_call_messages_handler", executor)
    monkeypatch.setattr(litellm_advisor, "_resolve_advisor_router", lambda model: ExpertRouter())
    original_messages = []
    for index in range(8):
        original_messages.extend([
            {"role": "user", "content": f"Historical record {index}: " + "detail " * 80},
            {"role": "assistant", "content": f"Earlier result {index}: " + "summary " * 24},
        ])
    original_messages.append({"role": "user", "content": "Review this latest conclusion."})
    data = {
        "model": "target",
        "messages": deepcopy(original_messages),
        "tools": [],
        "metadata": {"gateway_policy": {"advisor_model": "codex-luna-advisor"}},
    }
    plugin = AdvisorPlugin()
    hook_result, message_request = prepare_message_request(plugin, data)
    assert hook_result is None
    handler = litellm_advisor.AdvisorOrchestrationHandler()
    result = asyncio.run(handler.handle(
        model="target", messages=data["messages"], tools=message_request["tools"], stream=False,
        max_tokens=200, custom_llm_provider="openai",
    ))

    assert data["messages"] == original_messages
    assert target_calls[0]["messages"] == original_messages
    if select_advisor:
        assert result["content"][0]["text"] == "Final answer."
        assert len(target_calls) == 2
        assert len(expert_calls) == len(sift_inputs) == 1
        assert events == ["target_selected_advisor", "sift", "target_final"]
        continued = target_calls[1]["messages"]
        assert continued[:len(original_messages)] == original_messages
        assert continued[-2] == {"role": "assistant", "content": [{
            "type": "tool_use", "id": "advisor-call-1", "name": "advisor",
            "input": {"question": "Check my reasoning."},
        }]}
        assert continued[-1] == {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "advisor-call-1", "content": "One concise risk.",
        }]}
        assert sift_inputs[0][0] == "experts/codex-luna-advisor"
        assert len(expert_calls[0]["messages"]) < len(sift_inputs[0][1])
        assert expert_calls[0]["metadata"]["sift"]["compacted"] is True
        assert expert_calls[0]["metadata"]["sift"]["input_tokens"] > expert_calls[0]["metadata"]["sift"]["forwarded_tokens"]
        assert expert_calls[0]["metadata"]["sift"]["forwarded_tokens"] <= 512
        assert expert_calls[0]["expert_route"] == "experts/codex-luna-advisor"
        assert expert_calls[0]["messages"][-1]["content"] == "Check my reasoning."
    else:
        assert result["content"][0]["text"] == "Final answer."
        assert len(target_calls) == 1
        assert expert_calls == sift_inputs == []
        assert events == ["target_no_selection"]


@pytest.mark.parametrize("select_advisor", [False, True])
def test_public_messages_request_only_consults_selected_advisor_and_compacts_private_input(
    monkeypatch, tmp_path, select_advisor,
):
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor as litellm_advisor
    from litellm.proxy import proxy_server
    from subroute.handlers.codex_advisor import codex_advisor_handler
    from subroute.plugins.dynamic_router import DynamicRoutingPlugin
    from subroute.plugins.expert_sift import ExpertSiftHook
    from test_dynamic_routing import make_control_plane
    from test_codex_proxy_http import _configured_proxy

    events = []
    target_calls = []
    expert_calls = []
    interceptor_checks = []
    sift_hook = ExpertSiftHook()
    sift_hook.model_targets["codex-luna-advisor"] = 256
    sift_hook.max_target = 512
    control_plane = make_control_plane(tmp_path)
    control_plane.update_advisor("codex-luna-advisor")
    dynamic_router = DynamicRoutingPlugin(control_plane)

    def count_messages(*, messages, input, request_kwargs):
        return sum(len(str(message.get("content", ""))) for message in messages or []) // 4

    def message_response(content, stop_reason, model):
        return {
            "id": "msg-fixture",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }

    async def target_executor(**kwargs):
        target_calls.append(deepcopy(kwargs))
        if len(target_calls) == 1 and select_advisor:
            events.append("target_selected_advisor")
            return message_response([{
                "type": "tool_use", "id": "advisor-call-http-1", "name": "advisor",
                "input": {"question": "Check this conclusion."},
            }], "tool_use", "minimax")
        events.append("target_final" if len(target_calls) > 1 else "target_no_selection")
        return message_response([{"type": "text", "text": "Final answer."}], "end_turn", "minimax")

    class ExpertRouter:
        async def aanthropic_messages(self, **kwargs):
            events.append("sift")
            request = {
                "model": "codex-luna-advisor",
                "messages": deepcopy(kwargs["messages"]),
                "max_tokens": kwargs["max_tokens"],
                "metadata": deepcopy(kwargs.get("metadata") or {}),
            }
            input_tokens = count_messages(
                messages=request["messages"], input=None,
                request_kwargs={"model": request["model"], "messages": request["messages"]},
            )
            compacted = await sift_hook.async_pre_call_hook({}, None, request, "anthropic_messages")
            result = compacted or request
            result["input_tokens_before_sift"] = input_tokens
            expert_calls.append(result)
            return message_response([{"type": "text", "text": "One concise risk."}], "end_turn", kwargs["model"])

    class UnavailableExpertRouter:
        async def aanthropic_messages(self, **kwargs):
            pytest.fail("Experts/Sift dispatch occurred although the target did not select Advisor")

    monkeypatch.setattr(litellm_advisor, "_call_messages_handler", target_executor)
    if select_advisor:
        monkeypatch.setattr(litellm_advisor, "_resolve_advisor_router", lambda model: ExpertRouter())
    else:
        monkeypatch.setattr(litellm_advisor, "_resolve_advisor_router", lambda model: UnavailableExpertRouter())
    original_can_handle = litellm_advisor.AdvisorOrchestrationHandler.can_handle

    def observe_can_handle(handler, tools, provider):
        interceptor_checks.append((deepcopy(tools), provider))
        return original_can_handle(handler, tools, provider)

    monkeypatch.setattr(litellm_advisor.AdvisorOrchestrationHandler, "can_handle", observe_can_handle)
    advisor_plugin = AdvisorPlugin()
    pre_call_observations = []
    pre_request_observations = []
    original_pre_call_hook = advisor_plugin.async_pre_call_hook
    original_pre_request_hook = advisor_plugin.async_pre_request_hook
    request_fields = ("model", "messages", "system", "tools", "max_tokens", "max_output_tokens", "metadata")

    def request_snapshot(data):
        return {key: deepcopy(data[key]) for key in request_fields if key in data}

    async def observe_pre_call_hook(user_api_key_dict, cache, data, call_type):
        before = request_snapshot(data)
        result = await original_pre_call_hook(user_api_key_dict, cache, data, call_type)
        after = request_snapshot(data)
        pre_call_observations.append((before, after, result))
        return result

    async def observe_pre_request_hook(model, messages, kwargs):
        before = deepcopy(kwargs.get("tools", []))
        result = await original_pre_request_hook(model, messages, kwargs)
        after = deepcopy(result.get("tools", [])) if isinstance(result, dict) else None
        pre_request_observations.append((before, after))
        return result

    advisor_plugin.async_pre_call_hook = observe_pre_call_hook
    advisor_plugin.async_pre_request_hook = observe_pre_request_hook
    target_router_model = {
        "model_name": "minimax",
        "litellm_params": {
            "model": "codex-advisor/gpt-6-luna",
            "api_key": "fixture-key",
            "allowed_openai_params": ["reasoning_effort"],
        },
    }
    history = []
    for index in range(8):
        history.extend([
            {"role": "user", "content": f"Historical record {index}: " + "detail " * 80},
            {"role": "assistant", "content": f"Earlier result {index}: " + "summary " * 24},
        ])
    history.append({"role": "user", "content": "Review this latest conclusion."})
    original_history = deepcopy(history)

    with _configured_proxy(
        provider_model_list=[target_router_model],
        additional_custom_providers=[{
            "provider": "codex-advisor",
            "custom_handler": codex_advisor_handler,
        }],
        additional_callbacks=[dynamic_router, advisor_plugin, sift_hook],
    ) as (client, *_):
        monkeypatch.setattr(proxy_server.llm_router, "_count_pre_call_check_tokens", count_messages)
        response = client.post("/v1/messages", json={
            "model": "minimax",
            "system": "Preserve the safety instructions.",
            "messages": history,
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
            "max_tokens": 64,
        }, headers={"anthropic-version": "2023-06-01"})

    assert response.status_code == 200, response.text
    assert response.json()["content"][0]["text"] == "Final answer."
    assert len(interceptor_checks) == 1
    assert len(pre_call_observations) == 1
    pre_call_before, pre_call_after, pre_call_result = pre_call_observations[0]
    assert pre_call_result is None
    assert pre_call_after == {
        **pre_call_before,
        "system": f"{TARGET_SYSTEM_PROMPT}\n\nPreserve the safety instructions.",
    }
    assert not any(tool.get("type") == ADVISOR_TOOL_TYPE for tool in pre_call_after.get("tools", []))
    assert len(pre_request_observations) == 1
    before_pre_request, after_pre_request = pre_request_observations[0]
    assert not any(tool.get("type") == ADVISOR_TOOL_TYPE for tool in before_pre_request)
    assert sum(tool.get("type") == ADVISOR_TOOL_TYPE for tool in after_pre_request) == 1
    assert interceptor_checks[0][1] == "codex-advisor"
    assert any(tool.get("type") == ADVISOR_TOOL_TYPE for tool in interceptor_checks[0][0])
    assert target_calls[0]["messages"] == original_history
    assert target_calls[0]["system"] == (
        f"{TARGET_SYSTEM_PROMPT}\n\nPreserve the safety instructions."
    )
    assert target_calls[0]["max_tokens"] == 64
    assert {tool.get("name") for tool in target_calls[0]["tools"]} >= {"lookup", "advisor"}
    lookup_tool, = [tool for tool in target_calls[0]["tools"] if tool.get("name") == "lookup"]
    assert lookup_tool["input_schema"] == {"type": "object"}
    if not select_advisor:
        assert events == ["target_no_selection"]
        assert len(target_calls) == 1
        assert expert_calls == []
        return

    assert events == ["target_selected_advisor", "sift", "target_final"], (events, target_calls, response.text)
    assert len(target_calls) == 2
    continued = target_calls[1]["messages"]
    assert continued[:len(original_history)] == original_history
    assert continued[-2] == {"role": "assistant", "content": [{
        "type": "tool_use", "id": "advisor-call-http-1", "name": "advisor",
        "input": {"question": "Check this conclusion."},
    }]}
    assert continued[-1] == {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "advisor-call-http-1", "content": "One concise risk.",
    }]}
    assert len(expert_calls) == 1
    assert expert_calls[0]["messages"][-1]["content"] == "Check this conclusion."
    assert expert_calls[0]["metadata"]["sift"]["compacted"] is True
    assert expert_calls[0]["metadata"]["sift"]["input_tokens"] == expert_calls[0]["input_tokens_before_sift"]
    assert expert_calls[0]["metadata"]["sift"]["input_tokens"] > expert_calls[0]["metadata"]["sift"]["forwarded_tokens"]
    assert expert_calls[0]["metadata"]["sift"]["forwarded_tokens"] <= 512


def test_selected_advisor_failure_propagates_without_retry(monkeypatch):
    import litellm.router as litellm_router
    from litellm import Router
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor as litellm_advisor
    from litellm.router_utils.cooldown_handlers import is_advisor_orchestration_failure

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
    plugin = AdvisorPlugin()
    hook_result, message_request = prepare_message_request(plugin, data)
    assert hook_result is None
    with pytest.raises(RuntimeError, match="expert unavailable") as raised:
        asyncio.run(litellm_advisor.AdvisorOrchestrationHandler().handle(
            model="target", messages=[], tools=message_request["tools"], stream=False,
            max_tokens=100, custom_llm_provider="openai",
        ))
    assert len(target_calls) == 1
    assert is_advisor_orchestration_failure(raised.value) is True

    async def forbidden_fallback(*args, **kwargs):
        pytest.fail("An Advisor provider failure must not enter target fallback")

    monkeypatch.setattr(litellm_router, "run_async_fallback", forbidden_fallback)
    router = Router(model_list=[
        {"model_name": "target", "litellm_params": {"model": "openai/gpt-4o", "api_key": "sk-test"}},
        {"model_name": "target-fallback", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-test"}},
    ], disable_cooldowns=True)
    with pytest.raises(RuntimeError, match="expert unavailable"):
        asyncio.run(router.async_function_with_fallbacks_common_utils(
            raised.value, False, [{"target": ["target-fallback"]}], None, None, "target", (),
            {"model": "target", "messages": [], "tools": data["tools"]},
        ))


def test_target_failure_after_successful_advisor_uses_fallback_without_reconsulting(monkeypatch):
    from litellm import Router
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor as litellm_advisor
    from litellm.router_utils.cooldown_handlers import is_advisor_orchestration_failure

    target_calls = []
    expert_calls = []

    async def executor(**kwargs):
        target_calls.append(kwargs)
        if len(target_calls) == 1:
            return {"content": [{"type": "tool_use", "id": "advisor-call-1", "name": "advisor",
                                 "input": {"question": "Review."}}]}
        raise RuntimeError("target failed after consultation")

    class ExpertRouter:
        async def aanthropic_messages(self, **kwargs):
            expert_calls.append(kwargs)
            return {"content": [{"type": "text", "text": "One concise risk."}]}

    monkeypatch.setattr(litellm_advisor, "_call_messages_handler", executor)
    monkeypatch.setattr(litellm_advisor, "_resolve_advisor_router", lambda model: ExpertRouter())
    tools = [
        {"type": ADVISOR_TOOL_TYPE, "name": "advisor", "model": "experts/codex-luna-advisor", "max_uses": 1},
        {"type": "function", "name": "caller_tool"},
    ]
    original_tools = deepcopy(tools)
    router_attempts = []
    router = Router(model_list=[
        {"model_name": "target", "litellm_params": {"model": "openai/gpt-4o", "api_key": "sk-test"}},
        {"model_name": "target-fallback", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-test"}},
    ], fallbacks=[{"target": ["target-fallback"]}], disable_cooldowns=True)

    async def local_retries(*args, **kwargs):
        router_attempts.append({"model": kwargs["model"], "tools": deepcopy(kwargs.get("tools"))})
        if kwargs["model"] == "target":
            try:
                return await litellm_advisor.AdvisorOrchestrationHandler().handle(**kwargs)
            except RuntimeError as error:
                assert str(error) == "target failed after consultation"
                assert getattr(error, "_subroute_advisor_selected", False) is True
                assert is_advisor_orchestration_failure(error) is False
                raise
        assert kwargs["model"] == "target-fallback"
        assert kwargs["tools"] == [original_tools[1]]
        return {"content": [{"type": "text", "text": "Fallback answer."}]}

    monkeypatch.setattr(router, "async_function_with_retries", local_retries)
    result = asyncio.run(router.async_function_with_fallbacks(
        model="target", messages=[], tools=tools, stream=False,
        max_tokens=100, custom_llm_provider="openai", metadata={},
    ))

    assert result["content"][0]["text"] == "Fallback answer."
    assert [call["model"] for call in router_attempts] == ["target", "target-fallback"]
    assert router_attempts[1]["tools"] == [original_tools[1]]
    assert tools == original_tools
    assert len(target_calls) == 2
    assert len(expert_calls) == 1


@pytest.mark.parametrize("invocation", ["positional", "keyword"])
def test_advisor_fallback_binds_request_kwargs_by_parameter_name(monkeypatch, invocation):
    import litellm.router as litellm_router
    from litellm import Router

    AdvisorPlugin()
    advisor_tool = {
        "type": ADVISOR_TOOL_TYPE,
        "name": "advisor",
        "model": "experts/codex-luna-advisor",
        "max_uses": 1,
    }
    caller_tool = {"type": "function", "name": "caller_tool"}
    request_kwargs = {
        "model": "target",
        "messages": [{"role": "user", "content": "Preserve caller input."}],
        "tools": [advisor_tool, caller_tool],
        "temperature": 0.2,
        "metadata": {"marker": "keep"},
    }
    original_request = deepcopy(request_kwargs)
    fallback_dispatches = []

    async def capture_fallback(*args, **kwargs):
        fallback_dispatches.append({key: deepcopy(kwargs.get(key)) for key in original_request})
        return {"content": [{"type": "text", "text": "Fallback answer."}]}

    monkeypatch.setattr(litellm_router, "run_async_fallback", capture_fallback)
    router = Router(
        model_list=[
            {"model_name": "target", "litellm_params": {"model": "openai/gpt-4o", "api_key": "fixture"}},
            {"model_name": "target-fallback", "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "fixture"}},
        ],
        disable_cooldowns=True,
    )
    error = RuntimeError("target failed after Advisor selection")
    error._subroute_advisor_selected = True

    async def run_fallback():
        call = router.async_function_with_fallbacks_common_utils
        if invocation == "keyword":
            return await call(
                e=error,
                disable_fallbacks=False,
                fallbacks=[{"target": ["target-fallback"]}],
                context_window_fallbacks=None,
                content_policy_fallbacks=None,
                model_group="target",
                args=(),
                kwargs=request_kwargs,
            )
        return await call(
            error, False, [{"target": ["target-fallback"]}], None, None,
            "target", (), request_kwargs,
        )

    result = asyncio.run(run_fallback())

    assert result["content"][0]["text"] == "Fallback answer."
    assert fallback_dispatches == [{**original_request, "tools": [caller_tool]}]
    assert request_kwargs == original_request


def test_advisor_fallback_hook_shape_is_validated_before_global_patching(monkeypatch):
    from litellm.router import Router
    from litellm.llms.anthropic.pass_through.messages.interceptors import advisor
    from subroute.plugins.advisor_plugin import _prevent_advisor_replay_on_parent_fallback

    def find_tool_use(response):
        return None

    async def handle(self, *args, **kwargs):
        return None

    async def incompatible_fallback(self, error, **request):
        return None

    monkeypatch.setattr(advisor, "_find_advisor_tool_use", find_tool_use)
    monkeypatch.setattr(advisor.AdvisorOrchestrationHandler, "handle", handle)
    monkeypatch.setattr(Router, "async_function_with_fallbacks_common_utils", incompatible_fallback)

    with pytest.raises(RuntimeError, match="unsupported LiteLLM Router fallback hook signature"):
        _prevent_advisor_replay_on_parent_fallback()

    assert advisor._find_advisor_tool_use is find_tool_use
    assert advisor.AdvisorOrchestrationHandler.handle is handle


@pytest.mark.parametrize("advisor,route", [
    ("codex-gpt-6.1-sol-advisor", "experts/codex-gpt-6.1-sol-advisor"),
    ("gemini-subscription", "experts/gemini-3.8-flash-advisor"),
    ("gemini-subscription-argon", "experts/gemini-4-argon-advisor"),
    ("claude-opus-5-5-low-advisor", "experts/claude-opus-5-5-low-advisor"),
])
def test_expert_route_is_fixed_and_allowlisted(advisor, route):
    assert expert_route(advisor) == route


@pytest.mark.parametrize("advisor", [
    "gemini-subscription-3.7-flash",
    "gemini-subscription-3.6-flash",
    "gemini-subscription-pro",
])
def test_deprecated_gemini_expert_routes_are_unavailable(advisor):
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="Saved Advisor is not available"):
        expert_route(advisor)
