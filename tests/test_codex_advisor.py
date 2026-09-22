import asyncio
import json

import httpx
import pytest

from unified_llm_gateway.handlers import codex_advisor as advisor


def collect(monkeypatch, events, *, status=200, raw=None, error=None, effort=None):
    body = raw if raw is not None else "".join("data: " + json.dumps(event) + "\n\n" for event in events)

    def respond(request):
        payload = json.loads(request.content)
        assert payload.get("reasoning") == ({"effort": effort} if effort is not None else None)
        if error:
            raise error
        return httpx.Response(status, text=body)

    transport = httpx.MockTransport(respond)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(advisor, "read_codex_credentials", lambda: ("fixture", "fixture"))
    monkeypatch.setattr(advisor.httpx, "AsyncClient", lambda **kw: client_type(transport=transport, **kw))
    return asyncio.run(advisor.call_codex_streaming_collect("terra", [{"role": "user", "content": "hello"}], reasoning_effort=effort))


DELTA = {"type": "response.output_text.delta", "delta": "partial"}


@pytest.mark.parametrize("terminal", [None, "response.failed", "response.incomplete", "error"])
def test_partial_text_is_not_success(monkeypatch, terminal):
    events = [DELTA] + ([{"type": terminal}] if terminal else [])
    with pytest.raises(RuntimeError):
        collect(monkeypatch, events)


@pytest.mark.parametrize("effort", [None, "low", "high"])
def test_completed_response_preserves_provider_usage(monkeypatch, effort):
    text, usage = collect(monkeypatch, [DELTA, {
        "type": "response.completed", "response": {
            "status": "completed",
            "usage": {"input_tokens": 19, "output_tokens": 7, "total_tokens": 26},
        },
    }], effort=effort)
    assert text == "partial"
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (19, 7, 26)


def test_missing_usage_is_not_fabricated(monkeypatch):
    with pytest.raises(RuntimeError, match="usage"):
        collect(monkeypatch, [DELTA, {"type": "response.completed", "response": {"status": "completed"}}])


@pytest.mark.parametrize("message", [
    {"role": "tool", "content": "result"},
    {"role": "user", "content": [{"type": "image_url", "image_url": "fixture"}]},
    {"role": "assistant", "content": "text", "tool_calls": [{"id": "test"}]},
    {"role": "user", "content": {"unexpected": True}},
])
def test_unsupported_inputs_are_rejected_without_loss(message):
    with pytest.raises(ValueError):
        advisor.build_responses_input([message])


def test_instruction_and_assistant_roles_are_preserved():
    result = advisor.build_responses_input([
        {"role": "system", "content": "constraints"},
        {"role": "developer", "content": "more constraints"},
        {"role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
    ])
    assert [item["role"] for item in result] == ["developer", "developer", "assistant"]
    assert result[2]["content"] == [{"type": "output_text", "text": "answer"}]


def test_anthropic_tool_history_is_translated_to_responses_items():
    result = advisor.build_responses_input([
        {"role": "user", "content": "inspect the file"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "I will inspect it."},
            {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "contents"},
            {"type": "text", "text": "What should change?"},
        ]},
    ])

    assert result[2] == {
        "type": "function_call",
        "call_id": "toolu_1",
        "name": "read_file",
        "arguments": '{"path":"README.md"}',
    }
    assert result[3] == {
        "type": "function_call_output",
        "call_id": "toolu_1",
        "output": "contents",
    }
    assert result[4]["content"] == [{"type": "input_text", "text": "What should change?"}]


def test_openai_chat_tool_history_is_translated_to_responses_items():
    result = advisor.build_responses_input([
        {"role": "assistant", "content": "checking", "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
    ])

    assert [item["type"] for item in result] == [
        "message", "function_call", "function_call_output"
    ]
    assert result[1]["call_id"] == result[2]["call_id"] == "call_1"


@pytest.mark.parametrize("messages,match", [
    ([{"role": "assistant", "content": [{"type": "tool_use", "name": "read", "input": {}}]}], "call id"),
    ([{"role": "tool", "tool_call_id": "missing", "content": "result"}], "no matching call"),
    ([{"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "read", "arguments": "{"}}]}], "valid JSON"),
    ([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "image", "source": {}}]}]},
    ], "text only"),
])
def test_malformed_or_unsupported_tool_history_fails_closed(messages, match):
    with pytest.raises(ValueError, match=match):
        advisor.build_responses_input(messages)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_failures_are_not_success(monkeypatch, status):
    with pytest.raises(RuntimeError, match="API error"):
        collect(monkeypatch, [], status=status)


@pytest.mark.parametrize("body", ["data: not-json\n\n", "data: [DONE]\n\n", ""])
def test_invalid_or_empty_stream_fails(monkeypatch, body):
    with pytest.raises(RuntimeError):
        collect(monkeypatch, [], raw=body)


@pytest.mark.parametrize("error", [httpx.ReadTimeout("fixture"), httpx.ConnectError("fixture")])
def test_transport_failure_is_not_retried(monkeypatch, error):
    with pytest.raises(RuntimeError, match="HTTP error"):
        collect(monkeypatch, [], error=error)


def test_cancelled_request_propagates(monkeypatch):
    with pytest.raises(asyncio.CancelledError):
        collect(monkeypatch, [], error=asyncio.CancelledError())


@pytest.mark.parametrize("event", [[], None, {"type": "response.output_text.delta", "delta": 5},
                                     {"type": "response.completed", "response": {"status": "incomplete"}}])
def test_invalid_event_shape_fails(monkeypatch, event):
    with pytest.raises(RuntimeError):
        collect(monkeypatch, [event])


def test_multiline_sse_data_is_decoded(monkeypatch):
    completed = {"type": "response.completed", "response": {"status": "completed",
                 "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}}}
    raw = ': heartbeat\n\ndata:{"type":"response.output_text.delta",\ndata:"delta":"ok"}\n\n'
    raw += "data: " + json.dumps(completed) + "\n\ndata: [DONE]\n\n"
    assert collect(monkeypatch, [], raw=raw)[0] == "ok"
