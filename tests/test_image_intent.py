import asyncio
import json

import pytest

from subroute.plugins.image_intent import clear_image_request, image_request_context
from subroute.plugins.dynamic_router import DynamicRoutingPlugin
from test_dynamic_routing import make_control_plane
from test_codex_images import image_event, terminal, Stream, PNG


@pytest.mark.parametrize("text", [
    "Generate an image of a blue robot.", "Please create a watercolor picture of a fox.",
    "Can you generate an image for me?", "I want you to make a logo for my cafe.",
    "Draw a cat.", "Please paint a sunset.", "I need an image of a sunflower.",
    "帮我生成一张蓝色机器人的图片", "请画一只猫", "制作一张音乐节海报",
    "Let's generate a picture of the moon.",
    "I want to generate an image of the moon.",
    '<user_input mode="act">create a photo for an anime</user_input>',
])
def test_clear_intent(text):
    assert clear_image_request(text)


@pytest.mark.parametrize("text", [
    "How do I generate an image using this API?", "Explain image generation.",
    "Do not generate an image.", "Don't draw a cat.", "Analyze this image.",
    'Explain the phrase "generate an image".', "`generate an image` is a command.",
    "> Generate an image of a fox.", "Write code to generate an image.",
    "Generate a prompt for an image.", "Generate a short story about an image.",
    "Draw conclusions from the report.", "What is in this photo?", "不要生成图片",
    "Generate a Python script to create an image.", "An image of a cat is attached.",
    "Explain 'generate an image'.",
    '<user_input mode="act">Analyze this photo.</user_input>',
    '<user_input mode="act">"create a photo for an anime"</user_input>',
    '<document>create a photo for an anime</document>',
    'Explain <user_input mode="act">create a photo for an anime</user_input>',
])
def test_non_generation_intent(text):
    assert not clear_image_request(text)


@pytest.mark.parametrize("mode", ["force", "alias", "off"])
def test_intent_exception_and_latest_turn_only(tmp_path, mode):
    control = make_control_plane(tmp_path)
    control.update("minimax", mode)
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "auto", "messages": [{"role": "user", "content": "Generate an image of a bird"}]}
    result = asyncio.run(plugin.async_pre_call_hook({}, None, data, "acompletion"))
    assert result["model"] == "codex-luna"
    assert result["gateway_image_request"]["tool"] == {"type": "image_generation"}
    assert control.snapshot().active_model == "minimax"
    result["messages"].append({"role": "user", "content": "Explain caching instead."})
    result["model"] = "auto"
    result = asyncio.run(plugin.async_pre_call_hook({}, None, result, "acompletion"))
    assert result["model"] == "auto"
    assert "gateway_image_request" not in result


def test_tool_and_image_blocks_do_not_invent_user_intent():
    assert image_request_context({"messages": [
        {"role": "user", "content": "Generate an image"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": [{"type": "tool_result", "content": "Generate an image"}]},
    ]}, "anthropic_messages") is None
    assert image_request_context({"input": "Tell me the time", "tools": [
        {"type": "function", "name": "generate_image", "parameters": {"type": "object"}},
    ]}, "aresponses") is None


def native_stub(monkeypatch, events=None):
    from subroute.handlers import codex_images
    original = codex_images.litellm.aresponses
    calls = []
    streams = []

    async def dispatch(**kwargs):
        if kwargs.get("model") != "openai/gpt-6-luna":
            return await original(**kwargs)
        calls.append(kwargs)
        stream = Stream(events if events is not None else [image_event(), {"type": "response.output_item.done", "item": {
            "id": "msg_caption", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "Here is your image.", "annotations": []}],
        }}, terminal()])
        streams.append(stream)
        return stream

    monkeypatch.setattr(codex_images, "read_codex_credentials", lambda: ("fixture", "account"))
    monkeypatch.setattr(codex_images.litellm, "aresponses", dispatch)
    return calls, streams


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_intent_through_real_proxy_all_protocols(monkeypatch, tmp_path, protocol, stream):
    from test_codex_proxy_http import _configured_proxy, _request

    calls, streams = native_stub(monkeypatch)
    control = make_control_plane(tmp_path)
    control.update("minimax", "force")
    plugin = DynamicRoutingPlugin(control)
    path, data = _request(protocol, stream)
    data["model"] = "auto"
    if protocol == "responses":
        data["input"] = "Generate an image of a small blue bird."
        data["instructions"] = "Use a plain background."
    else:
        data["messages"] = [
            {"role": "user", "content": "My favorite color is blue."},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": '<user_input mode="act">create a photo for an anime</user_input>'},
        ]
    with _configured_proxy(dynamic_routing_plugin=plugin) as (client, *_):
        response = client.post(path, json=data)
    assert response.status_code == 200, response.text[:1000]
    assert PNG in response.text
    assert "Here is your image." in response.text
    assert len(calls) == 1
    assert calls[0]["tools"] == [{"type": "image_generation"}]
    assert calls[0]["tool_choice"] == {"type": "image_generation"}
    assert streams[0].closed
    assert control.snapshot().active_model == "minimax"
    if protocol == "responses":
        assert calls[0]["instructions"] == "Use a plain background."
        if not stream:
            assert any(item["type"] == "image_generation_call" for item in response.json()["output"])
        else:
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
            completed = next(event["response"] for event in events if event["type"] == "response.completed")
            assert PNG in json.dumps(completed["output"])
            assert "Here is your image." in json.dumps(completed["output"])
    else:
        assert "favorite color is blue" in json.dumps(calls[0]["input"])
        assert '<user_input mode="act">create a photo for an anime</user_input>' in str(calls[0]["input"])


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
def test_explicit_image_tools_and_other_tools_are_preserved(monkeypatch, tmp_path, protocol):
    from test_codex_proxy_http import _configured_proxy, _request

    calls, _ = native_stub(monkeypatch)
    control = make_control_plane(tmp_path)
    control.update("minimax", "force")
    path, data = _request(protocol, False)
    hosted = {"type": "image_generation", "quality": "low"}
    if protocol == "chat":
        other = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}
        data["tools"] = [hosted, other]
    elif protocol == "responses":
        other = {"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}}
        data["tools"] = [hosted, other]
    else:
        data["tools"] = [{"name": "generate_image", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}}},
                         {"name": "lookup", "input_schema": {"type": "object", "properties": {}}}]
        data["tool_choice"] = {"type": "tool", "name": "generate_image"}
    with _configured_proxy(dynamic_routing_plugin=DynamicRoutingPlugin(control)) as (client, *_):
        response = client.post(path, json=data)
    assert response.status_code == 200, response.text[:1000]
    assert PNG in response.text
    assert len(calls) == 1
    assert {tool.get("name", tool["type"]) for tool in calls[0]["tools"]} == {"lookup", "image_generation"}


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_luna_can_request_other_tools_without_faking_an_image(monkeypatch, tmp_path, protocol, stream):
    from test_codex_proxy_http import _configured_proxy, _request

    native_stub(monkeypatch, [{"type": "response.output_item.done", "item": {
        "type": "function_call", "id": "fc_lookup", "call_id": "call_lookup", "name": "lookup",
        "arguments": '{"subject":"fox"}', "status": "completed",
    }}, terminal()])
    control = make_control_plane(tmp_path)
    control.update("minimax", "force")
    path, data = _request(protocol, stream)
    if protocol == "responses":
        data["input"] = "Generate an image of a fox after looking up its colors."
        data["tools"] = [{"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}}]
    elif protocol == "messages":
        data["messages"] = [{"role": "user", "content": "Generate an image of a fox after looking up its colors."}]
        data["tools"] = [{"name": "lookup", "input_schema": {"type": "object", "properties": {}}}]
    else:
        data["messages"] = [{"role": "user", "content": "Generate an image of a fox after looking up its colors."}]
        data["tools"] = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]
    data["tool_choice"] = {"type": "auto"} if protocol == "messages" else "auto"
    with _configured_proxy(dynamic_routing_plugin=DynamicRoutingPlugin(control)) as (client, *_):
        response = client.post(path, json=data)
    assert response.status_code == 200, response.text[:1000]
    assert "lookup" in response.text and "fox" in response.text
    assert PNG not in response.text


def test_internal_context_cannot_be_supplied_by_client(tmp_path):
    control = make_control_plane(tmp_path)
    control.update("minimax", "force")
    data = {"model": "current", "messages": [{"role": "user", "content": "Hello"}],
            "gateway_image_request": {"protocol": "responses"},
            "extra_body": {"gateway_image_request": {"protocol": "responses"}}}
    result = asyncio.run(DynamicRoutingPlugin(control).async_pre_call_hook({}, None, data, "acompletion"))
    assert result["model"] == "minimax"
    assert "gateway_image_request" not in result and "gateway_image_request" not in result["extra_body"]


def test_image_exception_skips_saved_advisor(tmp_path):
    from subroute.plugins.advisor_plugin import AdvisorPlugin
    control = make_control_plane(tmp_path)
    control.update("minimax", "force")
    control.update_advisor("gemini-subscription")
    data = {"model": "current", "messages": [{"role": "user", "content": "Draw a cat"}]}
    asyncio.run(DynamicRoutingPlugin(control).async_pre_call_hook({}, None, data, "anthropic_messages"))
    before = json.dumps(data, sort_keys=True)
    asyncio.run(AdvisorPlugin().async_pre_call_hook({}, None, data, "anthropic_messages"))
    assert json.dumps(data, sort_keys=True) == before


@pytest.mark.parametrize("options", [
    {"tool_choice": "none"}, {"tools": [{"type": "image_generation", "size": "1024x1024"}]},
    {"n": 2},
])
def test_conflicts_rejected_before_provider(options):
    with pytest.raises(ValueError):
        image_request_context({"input": "Generate an image of a fox", **options}, "aresponses")


def test_refusal_is_returned_as_text_without_claiming_image_success(monkeypatch):
    from subroute.handlers.codex_images import image_conversation
    native_stub(monkeypatch, [{"type": "response.output_item.done", "item": {
        "type": "message", "id": "msg_refused", "role": "assistant", "status": "completed",
        "content": [{"type": "refusal", "refusal": "I cannot generate that image."}],
    }}, terminal()])
    result = asyncio.run(image_conversation([{"role": "user", "content": "Generate an image"}], {},
        {"protocol": "acompletion", "tool": {"type": "image_generation"}, "tool_choice": "auto"}))
    assert result.choices[0].message.content == "I cannot generate that image."
    assert not getattr(result.choices[0].message, "images", None)


def test_multiple_caption_blocks_survive_native_translation(monkeypatch):
    from subroute.handlers.codex_images import image_conversation
    native_stub(monkeypatch, [image_event(), {"type": "response.output_item.done", "item": {
        "type": "message", "id": "msg_caption", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "First caption.", "annotations": []},
                    {"type": "output_text", "text": "Second caption.", "annotations": []}],
    }}, terminal()])
    result = asyncio.run(image_conversation([{"role": "user", "content": "Generate an image"}], {},
        {"protocol": "acompletion", "tool": {"type": "image_generation"}, "tool_choice": "auto"}))
    assert result.choices[0].message.content == "First caption.\nSecond caption."
    assert result.choices[0].message.images[0]["image_url"]["url"].endswith(PNG)
