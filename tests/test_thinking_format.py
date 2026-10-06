"""Provider thinking must be collected exactly once by a Claude Code client."""

import asyncio
from copy import deepcopy
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path
from unittest.mock import patch

import litellm
import yaml

import pytest
from litellm.types.utils import ModelResponseStream

from subroute.plugins.dynamic_router import DynamicRoutingPlugin, RoutingControlPlane
from test_codex_proxy_http import _configured_proxy, _request
from test_codex_reasoning import reasoning_chunks
from test_dynamic_routing import make_control_plane
from subroute.thinking import repair_thinking_starts, _ordered_tool_deltas
from litellm.llms.custom_llm import CustomLLMError


def collect_messages(events):
    """Strict Anthropic client accumulator, including seeded start content."""
    blocks = []
    active = None
    saw_start = saw_stop = False
    for event in events:
        kind = event["type"]
        if kind == "message_start":
            assert not saw_start
            saw_start = True
        elif kind == "content_block_start":
            assert saw_start and active is None and not saw_stop
            assert event["index"] == len(blocks)
            active = deepcopy(event["content_block"])
        elif kind == "content_block_delta":
            assert active is not None and event["index"] == len(blocks)
            delta = event["delta"]
            field, block_type = {
                "thinking_delta": ("thinking", "thinking"),
                "signature_delta": ("signature", "thinking"),
                "text_delta": ("text", "text"),
                "input_json_delta": ("partial_json", "tool_use"),
            }[delta["type"]]
            assert active["type"] == block_type
            active[field] = active.get(field, "") + delta[field]
        elif kind == "content_block_stop":
            assert active is not None and event["index"] == len(blocks)
            if active["type"] == "tool_use" and "partial_json" in active:
                active["input"] = json.loads(active.pop("partial_json"))
            blocks.append(active)
            active = None
        elif kind == "message_stop":
            assert active is None and not saw_stop
            saw_stop = True
        elif kind == "error":
            pytest.fail(f"unexpected error event: {event}")
    assert saw_start and saw_stop and active is None
    return blocks


@pytest.mark.parametrize("shape", ["reasoning_content", "thinking_blocks"])
def test_codex_reasoning_through_messages_client(tmp_path, shape):
    control = make_control_plane(tmp_path)
    with control.config_path.open("a", encoding="utf-8") as config:
        config.write("\n  - model_name: codex-luna\n    litellm_params: {model: codex-subscription/gpt-6-luna}\n")
    control = RoutingControlPlane(control.config_path, control.state_path)
    control.update("minimax", "off")
    plugin = DynamicRoutingPlugin(control)
    chunks = reasoning_chunks()
    expected = "Plan\n\n中文 😀\\n" + "Continue **carefully**."
    for index, text in enumerate(["Plan\n\n中文 😀\\n", "Continue **carefully**."]):
        delta = chunks[index]["choices"][0]["delta"]
        delta.pop("reasoning_content", None)
        delta[shape] = text if shape == "reasoning_content" else [{"type": "thinking", "thinking": text, "signature": ""}]
    with _configured_proxy(upstream_chunks=chunks, dynamic_routing_plugin=plugin) as (client, calls, *_):
        path, payload = _request("messages", True)
        response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    blocks = collect_messages(events)
    assert [block["type"] for block in blocks] == ["thinking", "text", "tool_use"]
    assert blocks[0]["thinking"] == expected
    assert blocks[1]["text"] == "fixture answer"
    assert blocks[2]["input"] == {"q": 1}
    final = next(event for event in events if event["type"] == "message_delta")
    assert final["delta"]["stop_reason"] == "tool_use"
    assert final["usage"]["output_tokens"] == 7


@pytest.mark.parametrize("mixed_continuation", [False, True])
def test_interleaved_parallel_tool_fragments_reach_the_correct_blocks(mixed_continuation):
    chunks = reasoning_chunks()
    chunks[2]["choices"][0]["delta"]["tool_calls"].append({"index": 1, "id": "call_second", "type": "function", "function": {"name": "lookup", "arguments": '{"q":'}})
    chunks[3]["choices"][0]["delta"]["tool_calls"].append({"index": 1, "function": {"arguments": "2}"}})
    if mixed_continuation:
        chunks[3]["choices"][0]["delta"].update(reasoning_content="Later reasoning.", content="Later answer.")
    with _configured_proxy(upstream_chunks=chunks) as (client, *_):
        path, payload = _request("messages", True)
        response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    blocks = collect_messages(events)
    tools = [block for block in blocks if block["type"] == "tool_use"]
    assert [(tool["id"], tool["input"]) for tool in tools] == [("call_lookup", {"q": 1}), ("call_second", {"q": 2})]
    if mixed_continuation:
        assert [block["type"] for block in blocks] == ["thinking", "text", "tool_use", "tool_use", "thinking", "text"]
        assert blocks[-2]["thinking"] == "Later reasoning."
        assert blocks[-1]["text"] == "Later answer."


@pytest.mark.parametrize("failure", ["truncated", "invalid_json", "missing_id", "changed_id", "provider_error"])
def test_incomplete_tool_suffix_fails_without_exposing_tool_calls(failure):
    first = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "look", "arguments": '{"q":'}}
    continuation = {"index": 0, "function": {"name": "up", "arguments": "1}"}}
    if failure == "invalid_json":
        continuation["function"]["arguments"] = "oops"
    elif failure == "missing_id":
        del first["id"]
    elif failure == "changed_id":
        continuation["id"] = "call_2"
    output = []
    closed = []

    async def source():
        try:
            for tool in (first, continuation):
                yield ModelResponseStream(choices=[{"index": 0, "delta": {"tool_calls": [tool]}, "finish_reason": None}])
            if failure == "provider_error":
                raise RuntimeError("provider failed")
            if failure != "truncated":
                yield ModelResponseStream(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}])
        finally:
            closed.append(True)

    async def consume():
        async for chunk in _ordered_tool_deltas(source()):
            output.append(chunk)

    with pytest.raises(RuntimeError if failure == "provider_error" else CustomLLMError):
        asyncio.run(consume())
    assert output == [] and closed == [True]


def test_tool_name_fragments_and_raw_arguments_are_assembled_without_reencoding():
    arguments = '{ "q": 1, "path": "C:\\\\new\\\\test" }'

    async def source():
        for name, part, call_id in [("look", arguments[:10], "call_1"), ("up", arguments[10:], None)]:
            tool = {"index": 0, "function": {"name": name, "arguments": part}}
            if call_id:
                tool["id"] = call_id
            yield ModelResponseStream(choices=[{"index": 0, "delta": {"tool_calls": [tool]}, "finish_reason": None}])
        yield ModelResponseStream(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], usage={"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10})

    async def consume():
        return [chunk async for chunk in _ordered_tool_deltas(source())]

    result = asyncio.run(consume())
    tool = result[0].choices[0].delta.tool_calls[0]
    assert tool.function.name == "lookup" and tool.function.arguments == arguments
    assert result[-1].usage.total_tokens == 10


def test_cancelling_a_pending_tool_closes_the_provider():
    closed = []

    async def cancel():
        ready = asyncio.Event()
        blocker = asyncio.Event()

        async def source():
            try:
                yield ModelResponseStream(choices=[{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "lookup", "arguments": '{"q":'}}]}, "finish_reason": None}])
                ready.set()
                await blocker.wait()
            finally:
                closed.append(True)

        stream = _ordered_tool_deltas(source())
        task = asyncio.create_task(anext(stream))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()

    asyncio.run(cancel())
    assert closed == [True]


async def repaired(events):
    async def source():
        for event in events:
            yield event
    return [event async for event in repair_thinking_starts(source())]


@pytest.mark.parametrize("wire", ["dict", "bytes", "str"])
def test_repair_is_lossless_and_does_not_mutate_frames(wire):
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "Plan\n中文\\n", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Plan\n中文\\n"}},
    ]
    original = deepcopy(events)
    frames = events if wire == "dict" else [f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events]
    if wire == "bytes":
        frames = [frame.encode() for frame in frames]
    result = asyncio.run(repaired(frames))
    assert events == original
    if wire != "dict":
        result = [json.loads(frame.splitlines()[1][5:]) for frame in result]
    assert result[0]["content_block"]["thinking"] == ""
    assert result[1] == events[1]


@pytest.mark.parametrize("blocks", [
    [{"type": "thinking", "thinking": "signed\\n", "signature": "opaque-signature"}],
    [{"type": "redacted_thinking", "data": "opaque"}],
    [{"type": "thinking", "thinking": "one"}, {"type": "redacted_thinking", "data": "opaque"}],
    [{"type": "thinking", "thinking": ""}],
])
def test_signed_redacted_and_multiple_blocks_remain_verbatim(blocks):
    events = [{"type": "content_block_start", "index": index, "content_block": block} for index, block in enumerate(blocks)]
    events += [{"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "opaque"}}]
    original = deepcopy(events)
    assert asyncio.run(repaired(events)) == original
    assert events == original


def test_native_messages_and_other_protocol_events_are_not_reinterpreted():
    events = [b'event: content_block_delta\ndata: {"thinking":"signed\\\\n"}\n\n', {"type": "response.reasoning_summary_text.delta", "delta": "literal\\n"}, ModelResponseStream(**reasoning_chunks()[0])]
    result = asyncio.run(repaired(events))
    assert all(a is b for a, b in zip(result, events))


@pytest.mark.parametrize("next_event", [
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "different"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "thinking_delta", "thinking": "seed"}},
    {"type": "error", "error": {"type": "api_error"}},
    {"type": "content_block_stop", "index": 0},
])
def test_nonmatching_next_event_is_preserved(next_event):
    events = [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "seed"}}, next_event]
    assert asyncio.run(repaired(events)) == events


def test_truncated_opener_is_preserved_without_inventing_completion():
    events = [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "seed"}}]
    assert asyncio.run(repaired(events)) == events


def test_cancellation_closes_the_upstream_iterator():
    closed = []

    async def source():
        try:
            yield {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "seed"}}
            yield {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "seed"}}
            yield {"type": "message_stop"}
        finally:
            closed.append(True)

    async def cancel():
        stream = repair_thinking_starts(source())
        await anext(stream)
        await stream.aclose()

    asyncio.run(cancel())
    assert closed == [True]


def test_malformed_and_coalesced_frames_are_preserved():
    frames = [b'data: {"type":"content_block_start",', b'data: {"type":"ping"}\n\ndata: {"type":"ping"}\n\n',
              {"type": "content_block_start", "index": 0, "content_block": "invalid"},
              {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "seed"}},
              {"type": "content_block_delta", "index": 0, "delta": "invalid"}]
    assert asyncio.run(repaired(frames)) == frames


REASONING = "Plan\n\n中文 😀\\n"
ANSWER = "Answer\n\n```js\nconst s = '\\n';\n```"
TOOL_INPUT = {"q": 1, "model": "minimax", "path": "C:\\new\\test"}


def native_message():
    return {"id": "msg_native", "type": "message", "role": "assistant", "model": "native",
            "content": [{"type": "thinking", "thinking": REASONING, "signature": "signed-native"},
                        {"type": "redacted_thinking", "data": "opaque-data"},
                        {"type": "text", "text": ANSWER},
                        {"type": "tool_use", "id": "call_lookup", "name": "lookup", "input": TOOL_INPUT}],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 7}}


def native_stream():
    message = native_message()
    events = [{"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}]
    for index, block in enumerate(message["content"]):
        start = {**block}
        deltas = []
        if block["type"] == "thinking":
            start.update(thinking="", signature="")
            deltas = [{"type": "thinking_delta", "thinking": REASONING}, {"type": "signature_delta", "signature": block["signature"]}]
        elif block["type"] == "text":
            start["text"] = ""
            deltas = [{"type": "text_delta", "text": ANSWER}]
        elif block["type"] == "tool_use":
            start["input"] = {}
            arguments = json.dumps(TOOL_INPUT)
            deltas = [{"type": "input_json_delta", "partial_json": arguments[:8]}, {"type": "input_json_delta", "partial_json": arguments[8:]}]
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events.extend([{"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 7}}, {"type": "message_stop"}])
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


@contextmanager
def provider_server(native, reasoning_field, ollama=False):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            streaming = body.get("stream", False)
            if ollama:
                chunks = [
                    {"message": {"role": "assistant", "thinking": REASONING, "content": ""}, "done": False},
                    {"message": {"role": "assistant", "content": ANSWER}, "done": False},
                    {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "lookup", "arguments": TOOL_INPUT}}]}, "done": False},
                    {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 7},
                ]
                for chunk in chunks:
                    chunk.update(model="qwen3", created_at="2026-10-06T00:00:00Z")
                response = "".join(json.dumps(chunk) + "\n" for chunk in chunks) if streaming else json.dumps({**chunks[-1], "message": {**chunks[0]["message"], "content": ANSWER, "tool_calls": chunks[2]["message"]["tool_calls"]}})
            elif native:
                response = native_stream() if streaming else json.dumps(native_message())
            elif streaming:
                chunks = reasoning_chunks()
                chunks[0]["choices"][0]["delta"] = {reasoning_field: REASONING}
                chunks[1]["choices"][0]["delta"] = {"content": ANSWER}
                chunks[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = json.dumps(TOOL_INPUT)[:8]
                chunks[3]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = json.dumps(TOOL_INPUT)[8:]
                chunks[2]["choices"][0]["delta"]["tool_calls"].append({"index": 1, "id": "call_second", "type": "function", "function": {"name": "lookup", "arguments": '{"q":'}})
                chunks[3]["choices"][0]["delta"]["tool_calls"].append({"index": 1, "function": {"arguments": "2}"}})
                response = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            else:
                response = json.dumps({"id": "chatcmpl-fixture", "object": "chat.completion", "created": 1, "model": "fixture",
                    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": ANSWER,
                        reasoning_field: REASONING, "tool_calls": [{"id": "call_lookup", "type": "function", "function": {"name": "lookup", "arguments": json.dumps(TOOL_INPUT)}}]}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}})
            payload = response.encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream" if streaming else "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("model,native,field", [
    ("anthropic/claude-sonnet-4-6", True, None),
    ("minimax/MiniMax-M3", True, None),
    ("openrouter/minimax/minimax-m3", False, "reasoning"),
    ("openai/mimo-v2.6-pro", False, "reasoning_content"),
    ("openai/local-model", False, "reasoning_content"),
    ("ollama_chat/qwen3", False, "thinking"),
])
@pytest.mark.parametrize("stream", [True, False])
def test_native_provider_adapters_through_messages_http(tmp_path, model, native, field, stream):
    # Real LiteLLM router, HTTP transports and provider adapters. Only the
    # external provider is replaced with a local deterministic wire fixture.
    config = tmp_path / "providers.yaml"
    config.write_text(f"model_list:\n  - model_name: codex-luna\n    litellm_params: {{model: {model}}}\n")
    (tmp_path / "state.json").write_text(json.dumps({"active_model": "codex-luna", "mode": "off", "advisor_model": None}))
    control = RoutingControlPlane(config, tmp_path / "state.json")
    control.update("codex-luna", "off")
    plugin = DynamicRoutingPlugin(control)
    ollama = model.startswith("ollama_chat/")
    with provider_server(native, field, ollama) as (base, calls):
        models = [{"model_name": "codex-luna", "litellm_params": {"model": model, "api_base": base + ("/v1" if not native and not ollama else ""), "api_key": "fixture-key"}}]
        settings = yaml.safe_load((Path(__file__).parents[1] / "config/litellm.yaml").read_text(encoding="utf-8"))["litellm_settings"]
        assert settings["use_chat_completions_url_for_anthropic_messages"] is True
        with patch.object(litellm, "use_chat_completions_url_for_anthropic_messages", settings["use_chat_completions_url_for_anthropic_messages"]), _configured_proxy(provider_model_list=models, dynamic_routing_plugin=plugin) as (client, *_):
            path, payload = _request("messages", stream)
            response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert calls[0][0].endswith("/api/chat" if ollama else "/messages" if native else "/chat/completions")
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        blocks = collect_messages(events)
    else:
        blocks = response.json()["content"]
        assert response.json()["stop_reason"] == "tool_use"
    assert next(block for block in blocks if block["type"] == "thinking")["thinking"] == REASONING
    assert next(block for block in blocks if block["type"] == "text")["text"] == ANSWER
    assert next(block for block in blocks if block["type"] == "tool_use")["input"] == TOOL_INPUT
    if native:
        assert blocks == native_message()["content"]
    elif stream and not ollama:
        assert [(block["id"], block["input"]) for block in blocks if block["type"] == "tool_use"] == [("call_lookup", TOOL_INPUT), ("call_second", {"q": 2})]


@pytest.mark.parametrize("stream", [True, False])
def test_gemini_subscription_messages_preserve_formatting(tmp_path, monkeypatch, stream):
    from subroute.handlers import antigravity

    calls = []

    async def bridge(model, prompt, **kwargs):
        calls.append(model)
        return ANSWER, litellm.Usage(prompt_tokens=3, completion_tokens=7, total_tokens=10)

    monkeypatch.setattr(antigravity, "invoke_agy", bridge)
    config = tmp_path / "providers.yaml"
    config.write_text("model_list:\n  - model_name: codex-luna\n    litellm_params: {model: antigravity/gemini-3.8-flash}\n")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"active_model": "codex-luna", "mode": "off", "advisor_model": None}))
    plugin = DynamicRoutingPlugin(RoutingControlPlane(config, state))
    models = [{"model_name": "codex-luna", "litellm_params": {"model": "antigravity/gemini-3.8-flash"}}]
    with _configured_proxy(provider_model_list=models, dynamic_routing_plugin=plugin, additional_custom_providers=[{"provider": "antigravity", "custom_handler": antigravity.antigravity_handler}]) as (client, *_):
        path, payload = _request("messages", stream)
        response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    blocks = collect_messages([json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]) if stream else response.json()["content"]
    assert blocks == [{"type": "text", "text": ANSWER}]
