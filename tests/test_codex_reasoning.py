"""Reasoning must survive the real proxy without becoming answer/tool text."""

from copy import deepcopy
import asyncio
import json

import litellm
from litellm.types.utils import ModelResponseStream

from subroute.handlers.codex_subscription import _codex_stream, _stream_chunks
from test_codex_proxy_http import _configured_proxy, _request


def reasoning_chunks():
    deltas = [
        {"role": "assistant", "reasoning_content": "Planning "},
        {"reasoning_content": "the lookup.", "content": "fixture answer"},
        {"tool_calls": [{"index": 0, "id": "call_lookup", "type": "function",
                         "function": {"name": "lookup", "arguments": '{"q":'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]},
        {},
    ]
    chunks = [{
        "id": "chatcmpl-reasoning", "object": "chat.completion.chunk",
        "created": 1, "model": "gpt-6.1-sol",
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": "tool_calls" if index == len(deltas) - 1 else None}],
    } for index, delta in enumerate(deltas)]
    chunks.append({
        "id": "chatcmpl-reasoning", "object": "chat.completion.chunk",
        "created": 1, "model": "gpt-6.1-sol", "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10,
                  "completion_tokens_details": {"reasoning_tokens": 4}},
    })
    return chunks


def verify_reasoning_through_proxy(protocol, stream):
    with _configured_proxy(upstream_chunks=reasoning_chunks()) as (client, calls, *_):
        path, payload = _request(protocol, stream)
        if protocol == "chat" and stream:
            payload["stream_options"] = {"include_usage": True}
        response = client.post(path, json=payload, headers={"anthropic-version": "2023-06-01"})
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line[6:] != "[DONE]"]
        if protocol == "messages":
            starts = [event for event in events if event["type"] == "content_block_start"]
            assert [event["content_block"]["type"] for event in starts] == ["thinking", "text", "tool_use"]
            assert len({event["index"] for event in starts}) == 3
            deltas = [event["delta"] for event in events if event["type"] == "content_block_delta"]
            assert "".join(d.get("thinking", "") for d in deltas) == "Planning the lookup."
            assert "".join(d.get("text", "") for d in deltas) == "fixture answer"
            assert "".join(d.get("partial_json", "") for d in deltas) == '{"q":1}'
            assert events[-1]["type"] == "message_stop"
            final = next(event for event in events if event["type"] == "message_delta")
            assert final["delta"]["stop_reason"] == "tool_use"
            assert final["usage"]["output_tokens"] == 7
        elif protocol == "chat":
            deltas = [choice["delta"] for event in events for choice in event.get("choices", [])]
            assert "".join(d.get("reasoning_content") or "" for d in deltas) == "Planning the lookup."
            assert "".join(d.get("content") or "" for d in deltas) == "fixture answer"
            assert next(event["usage"] for event in events if event.get("usage"))["total_tokens"] == 10
            assert any(c.get("finish_reason") == "tool_calls" for event in events for c in event.get("choices", []))
        else:
            assert "".join(e["delta"] for e in events if e["type"] == "response.reasoning_summary_text.delta") == "Planning the lookup."
            assert "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta") == "fixture answer"
            assert events[-1]["type"] == "response.completed"
            assert events[-1]["response"]["usage"]["total_tokens"] == 10
    else:
        body = response.json()
        if protocol == "messages":
            assert [b["type"] for b in body["content"]] == ["thinking", "text", "tool_use"]
            assert body["content"][0]["thinking"] == "Planning the lookup."
            assert body["content"][1]["text"] == "fixture answer"
            assert body["content"][2]["input"] == {"q": 1}
            assert body["usage"]["output_tokens"] == 7
            assert body["stop_reason"] == "tool_use"
        elif protocol == "chat":
            message = body["choices"][0]["message"]
            assert message["reasoning_content"] == "Planning the lookup."
            assert message["content"] == "fixture answer"
            assert message["tool_calls"][0]["function"]["arguments"] == '{"q":1}'
            assert body["usage"]["total_tokens"] == 10
        else:
            reasoning = next(item for item in body["output"] if item["type"] == "reasoning")
            assert "".join(s["text"] for s in reasoning.get("summary", reasoning.get("content", []))) == "Planning the lookup."
            assert body["usage"]["total_tokens"] == 10


def test_reasoning_through_all_public_protocols():
    for protocol in ("messages", "chat", "responses"):
        for stream in (True, False):
            verify_reasoning_through_proxy(protocol, stream)


def test_mixed_reasoning_finish_and_usage_are_each_forwarded_once():
    upstream = deepcopy(reasoning_chunks()[1])
    upstream["choices"][0]["finish_reason"] = "stop"
    upstream["usage"] = reasoning_chunks()[-1]["usage"]
    original = deepcopy(upstream)
    chunks = _stream_chunks(upstream)
    assert len(chunks) == 2
    assert isinstance(chunks[0], ModelResponseStream)
    assert chunks[0].choices[0].delta.reasoning_content == "the lookup."
    assert chunks[0].choices[0].finish_reason is None
    assert chunks[1]["text"] == "fixture answer"
    assert chunks[1]["is_finished"] is True
    assert chunks[1]["usage"] == upstream["usage"]
    assert upstream == original


def test_reasoning_with_usage_and_no_answer_preserves_usage():
    upstream = reasoning_chunks()[0]
    upstream["usage"] = reasoning_chunks()[-1]["usage"]
    chunks = _stream_chunks(upstream)
    assert len(chunks) == 2
    assert chunks[0].choices[0].delta.reasoning_content == "Planning "
    assert chunks[1]["usage"] == upstream["usage"]


def test_signed_thinking_blocks_keep_their_signature():
    upstream = reasoning_chunks()[0]
    upstream["choices"][0]["delta"] = {"thinking_blocks": [
        {"type": "thinking", "thinking": "fixture summary", "signature": "fixture-signature"},
    ]}
    chunk = _stream_chunks(upstream)[0]
    assert chunk.choices[0].delta.thinking_blocks[0]["signature"] == "fixture-signature"


def test_native_reasoning_chunks_keep_usage_and_do_not_mutate_upstream():
    upstream = ModelResponseStream(**reasoning_chunks()[0])
    upstream.usage = litellm.Usage(**reasoning_chunks()[-1]["usage"])
    original = upstream.model_dump()
    chunks = _stream_chunks(upstream)
    assert chunks[0].choices[0].delta.reasoning_content == "Planning "
    assert chunks[1]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 4
    assert upstream.model_dump() == original


def test_summary_requests_keep_caller_effort_and_explicit_summary_choice(monkeypatch):
    captured = []

    async def endpoint(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(litellm, "acompletion", endpoint)
    for supplied, expected in [
        ({}, {"summary": "auto"}),
        ({"reasoning_effort": "low"}, {"effort": "low", "summary": "auto"}),
        ({"reasoning_effort": {"effort": "high", "summary": None}}, {"effort": "high", "summary": None}),
    ]:
        original = deepcopy(supplied)
        asyncio.run(_codex_stream("gpt-6.1-sol", [], "https://fixture.invalid", "fixture", {}, 12, supplied))
        assert captured[-1]["reasoning_effort"] == expected
        assert supplied == original
