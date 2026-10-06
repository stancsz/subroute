"""Client lifecycle/replay contracts, independent of provider prose choices."""

import asyncio
from copy import deepcopy
import json

import pytest
from litellm.types.utils import ModelResponseStream
from openai.types.responses import ResponseReasoningItem

from test_codex_proxy_http import _configured_proxy, _request
from test_codex_reasoning import reasoning_chunks
from test_dynamic_routing import make_control_plane
from test_thinking_format import collect_messages
from subroute.plugins.dynamic_router import DynamicRoutingPlugin, RoutingControlPlane
from subroute.thinking import _ordered_tool_deltas


ANSWER = 'VISIBLE ANSWER\n中文 😀\n```python\nx = r"\\n"\n```'
THINKING_CASES = [
    [{"type": "thinking", "thinking": "PLAN A", "signature": "sigA"}],
    [{"type": "thinking", "thinking": "PLAN A", "signature": "sigA"},
     {"type": "thinking", "thinking": "PLAN B", "signature": "sigB"}],
    [{"type": "thinking", "thinking": "PLAN A", "signature": ""},
     {"type": "thinking", "thinking": "PLAN B", "signature": ""}],
    [{"type": "redacted_thinking", "data": "opaque\\n/signature"}],
    [{"type": "thinking", "thinking": "PLAN A", "signature": "sigA"},
     {"type": "redacted_thinking", "data": "opaque"},
     {"type": "thinking", "thinking": "PLAN B", "signature": "sigB"}],
]


@pytest.fixture
def format_plugin(tmp_path):
    control = make_control_plane(tmp_path)
    with control.config_path.open("a", encoding="utf-8") as config:
        config.write("\n  - model_name: codex-luna\n    litellm_params: {model: codex-subscription/gpt-6-luna}\n")
    control = RoutingControlPlane(control.config_path, control.state_path)
    control.update("minimax", "off")
    return DynamicRoutingPlugin(control)


def response_events(response):
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ") and line[6:] != "[DONE]"]


def request_chunks(blocks):
    chunks = reasoning_chunks()
    chunks[0]["choices"][0]["delta"] = {"role": "assistant", "thinking_blocks": deepcopy(blocks)}
    chunks[1]["choices"][0]["delta"] = {"content": ANSWER}
    return chunks


@pytest.mark.parametrize("blocks", THINKING_CASES)
@pytest.mark.parametrize("stream", [True, False])
def test_messages_preserve_every_thinking_block_and_exact_replay_signature(format_plugin, blocks, stream):
    with _configured_proxy(upstream_chunks=request_chunks(blocks), dynamic_routing_plugin=format_plugin) as (client, *_):
        path, payload = _request("messages", stream)
        response = client.post(path, json=payload)
    result = collect_messages(response_events(response)) if stream else response.json()["content"]
    reasoning = [block for block in result if block["type"] in {"thinking", "redacted_thinking"}]
    assert reasoning == blocks
    assert [block["text"] for block in result if block["type"] == "text"] == [ANSWER]
    assert [block["input"] for block in result if block["type"] == "tool_use"] == [{"q": 1}]


def collect_responses(events):
    """Validate the identities, indices, part types, deltas and final snapshot."""
    items = {}
    parts = set()
    text = {}
    arguments = {}
    completed = None
    closed = set()
    for event in events:
        kind = event["type"]
        if kind == "response.output_item.added":
            index, item = event["output_index"], event["item"]
            if item["type"] == "reasoning":
                ResponseReasoningItem.model_validate(item)
            assert index == len(items), event
            assert item["id"] not in {i["id"] for i in items.values()}
            items[index] = item
        elif "item_id" in event:
            index, identity = event["output_index"], event["item_id"]
            assert index in items and items[index]["id"] == identity, event
            assert identity not in closed, event
            item_type = items[index]["type"]
            if kind in {"response.content_part.added", "response.reasoning_summary_part.added"}:
                field = "summary_index" if "reasoning" in kind else "content_index"
                key = (identity, field, event[field])
                assert key not in parts
                parts.add(key)
            elif kind == "response.output_text.delta":
                assert item_type == "message"
                assert (identity, "content_index", event["content_index"]) in parts
                text[identity] = text.get(identity, "") + event["delta"]
            elif kind == "response.reasoning_summary_text.delta":
                assert item_type == "reasoning"
                assert (identity, "summary_index", event.get("summary_index", 0)) in parts
                text[identity] = text.get(identity, "") + event["delta"]
            elif kind == "response.reasoning_text.delta":
                assert item_type == "reasoning"
                assert (identity, "content_index", event["content_index"]) in parts
                text[identity] = text.get(identity, "") + event["delta"]
            elif kind == "response.content_part.done":
                assert event["part"]["type"] == ("reasoning_text" if item_type == "reasoning" else "output_text")
                assert event["part"]["text"] == text.get(identity, "")
            elif kind == "response.output_text.done":
                assert event["text"] == text.get(identity, "")
            elif kind == "response.function_call_arguments.delta":
                assert item_type == "function_call"
                arguments[identity] = arguments.get(identity, "") + event["delta"]
            elif kind == "response.function_call_arguments.done":
                assert event["arguments"] == arguments[identity]
                json.loads(event["arguments"])
        elif kind == "response.output_item.done":
            index, item = event["output_index"], event["item"]
            if item["type"] == "reasoning":
                ResponseReasoningItem.model_validate(item)
            assert index in items and items[index]["id"] == item["id"], event
            assert item["type"] == items[index]["type"]
            assert item["id"] not in closed, event
            closed.add(item["id"])
            items[index] = item
        elif kind == "response.completed":
            assert completed is None
            completed = event["response"]
            assert [i["id"] for i in completed["output"]] == [i["id"] for i in items.values()]
            assert closed == {i["id"] for i in items.values()}
            for item in completed["output"]:
                if item["type"] == "reasoning":
                    # Native Responses may return typed reasoning content.
                    # Only output_text inside reasoning is an answer leak.
                    assert all(part["type"] == "reasoning_text" for part in item.get("content", [])), item
                    assert "summary" in item
    assert completed is not None
    return completed["output"]


@pytest.mark.parametrize("part_type", ["reasoning_text", "output_text"])
def test_native_reasoning_content_is_typed_separately_from_answer(part_type):
    part = {"type": part_type, "text": "PRIVATE PLAN"}
    start = {"id": "rs_native", "type": "reasoning", "summary": [], "status": "in_progress"}
    done = {**start, "status": "completed", "content": [part], "encrypted_content": "opaque-replay"}
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": start},
        {"type": "response.content_part.added", "output_index": 0, "item_id": "rs_native", "content_index": 0, "part": {**part, "text": ""}},
        {"type": "response.reasoning_text.delta", "output_index": 0, "item_id": "rs_native", "content_index": 0, "delta": "PRIVATE PLAN"},
        {"type": "response.content_part.done", "output_index": 0, "item_id": "rs_native", "content_index": 0, "part": part},
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {"type": "response.completed", "response": {"output": [done]}},
    ]
    if part_type == "output_text":
        with pytest.raises(AssertionError):
            collect_responses(events)
    else:
        assert collect_responses(events) == [done]


@pytest.mark.parametrize("blocks", [None] + THINKING_CASES)
def test_responses_item_lifecycle_and_reasoning_snapshot_agree(format_plugin, blocks):
    chunks = reasoning_chunks() if blocks is None else request_chunks(blocks)
    with _configured_proxy(upstream_chunks=chunks, dynamic_routing_plugin=format_plugin) as (client, *_):
        path, payload = _request("responses", True)
        response = client.post(path, json=payload)
    output = collect_responses(response_events(response))
    assert [i["type"] for i in output] == ["reasoning", "message", "function_call"]
    message = next(i for i in output if i["type"] == "message")
    assert message["content"][0]["text"] == ("fixture answer" if blocks is None else ANSWER)
    reasoning = output[0]
    assert "content" not in reasoning  # Repaired Chat bridge uses summary.
    if blocks is None:
        assert reasoning["summary"] == [{"type": "summary_text", "text": "Planning the lookup."}]
    else:
        expected = "".join(b.get("thinking", "") for b in blocks)
        assert "".join(part["text"] for part in reasoning["summary"]) == expected
        signed = [b for b in blocks if b.get("signature") or b.get("data")]
        if signed:
            assert json.loads(reasoning["encrypted_content"]) == signed


def test_tool_ordering_preserves_opaque_tool_and_function_metadata():
    tools = [
        {"index": 0, "id": "call_a", "type": "function", "provider_specific_fields": {"thought_signature": "opaque-tool"},
         "function": {"name": "lookup", "arguments": '{"q":', "provider_specific_fields": {"thought_signature": "opaque-function"}}},
        {"index": 0, "provider_specific_fields": {"other": "metadata"}, "function": {"arguments": "1}"}},
    ]
    async def source():
        for tool in tools:
            yield ModelResponseStream(choices=[{"index": 0, "delta": {"tool_calls": [tool]}, "finish_reason": None}])
        yield ModelResponseStream(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}])
    async def consume():
        return [chunk async for chunk in _ordered_tool_deltas(source())]
    output = asyncio.run(consume())[0].choices[0].delta.tool_calls[0].model_dump(exclude_none=True)
    assert output["provider_specific_fields"] == {"thought_signature": "opaque-tool", "other": "metadata"}
    # LiteLLM Function does not accept nested metadata at construction; verify
    # the opaque fields actually carried by its supported tool object.
    assert output["function"]["arguments"] == '{"q":1}'


@pytest.mark.parametrize("deltas,expected", [
    ([{"content": "BEFORE"}, {"reasoning_content": "THOUGHT"}, {"content": "AFTER"}],
     [("message", "BEFORE"), ("reasoning", "THOUGHT"), ("message", "AFTER")]),
    ([{"reasoning_content": "FIRST"}, {"content": "BEFORE"}, {"reasoning_content": "SECOND"}, {"content": "AFTER"}],
     [("reasoning", "FIRST"), ("message", "BEFORE"), ("reasoning", "SECOND"), ("message", "AFTER")]),
    ([{"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
      {"reasoning_content": "THOUGHT"}, {"content": "ANSWER"}],
     [("function_call", "{}"), ("reasoning", "THOUGHT"), ("message", "ANSWER")]),
])
def test_responses_reasoning_can_resume_without_reusing_closed_items(format_plugin, deltas, expected):
    chunks = [{"id": "chatcmpl-transition", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
               "choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
    chunks[0]["choices"][0]["delta"]["role"] = "assistant"
    chunks.append({"id": "chatcmpl-transition", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    with _configured_proxy(upstream_chunks=chunks, dynamic_routing_plugin=format_plugin) as (client, *_):
        path, payload = _request("responses", True)
        response = client.post(path, json=payload)
    output = collect_responses(response_events(response))
    actual = [(item["type"], item["arguments"] if item["type"] == "function_call"
               else "".join(part["text"] for part in item.get("summary", item.get("content", [])))) for item in output]
    assert actual == expected
