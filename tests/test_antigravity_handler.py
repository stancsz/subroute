import asyncio
import base64
import io
import json
import re
import wave

import httpx
import pytest
from litellm.exceptions import ContentPolicyViolationError

from subroute.handlers import antigravity


TOOLS = [{
    "name": "ping",
    "description": "Return the supplied text.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
}]


@pytest.mark.parametrize("stream", [False, True])
def test_multiple_client_tool_decisions_keep_distinct_inputs_and_order(monkeypatch, stream):
    inputs = ["ONE\n中文", r"TWO\n"]
    async def invoke(*args, **kwargs):
        content = json.dumps({"kind": "tool_calls", "calls": [{"kind": "tool_call", "name": "ping", "input": {"text": text}} for text in inputs]})
        return content, antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    request = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "Read both inputs."}],
               "optional_params": {"tools": TOOLS, "tool_choice": "auto", "parallel_tool_calls": True}}
    if stream:
        async def collect():
            return [chunk async for chunk in antigravity.antigravity_handler.astreaming(**request)]
        chunks = asyncio.run(collect())
        calls = [chunk["tool_use"] for chunk in chunks if chunk.get("tool_use")]
        assert [call["index"] for call in calls] == [0, 1]
        assert chunks[-1]["finish_reason"] == "tool_calls"
    else:
        result = asyncio.run(antigravity.antigravity_handler.acompletion(**request))
        calls = [call.model_dump() for call in result.choices[0].message.tool_calls]
        assert result.choices[0].finish_reason == "tool_calls"
    assert len({call["id"] for call in calls}) == 2
    assert [json.loads(call["function"]["arguments"])["text"] for call in calls] == inputs


@pytest.mark.parametrize("suffix,options", [
    ('{"kind":"tool_call","name":"unknown","input":{}}', {}),
    ('{"kind":"tool_call","name":"ping","input":{"text":42}}', {}),
    ('{"kind":"tool_call","name":"ping","input":{"text":"SECOND"}}', {"parallel_tool_calls": False}),
    ('{"kind":"text","text":"MIXED ANSWER"}', {}),
    ('{"kind":"tool_call","name":"ping","input":', {}),
    ("\n".join(json.dumps({"kind": "tool_call", "name": "ping", "input": {"text": str(n)}}) for n in range(8)), {}),
])
def test_invalid_additional_decision_exposes_no_partial_tools(monkeypatch, suffix, options):
    async def invoke(*args, **kwargs):
        return '{"kind":"tool_call","name":"ping","input":{"text":"FIRST"}}\n' + suffix, antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    async def collect():
        seen = []
        with pytest.raises(antigravity.CustomLLMError):
            async for chunk in antigravity.antigravity_handler.astreaming(
                model="gemini-3.8-flash", messages=[{"role": "user", "content": "Use tools."}],
                optional_params={"tools": TOOLS, "tool_choice": "auto", **options},
            ):
                seen.append(chunk)
        assert seen == []
    asyncio.run(collect())


def test_provider_cannot_inject_internal_batch_shape_to_bypass_parallel_opt_out(monkeypatch):
    async def invoke(*args, **kwargs):
        return json.dumps({"kind": "tool_calls", "calls": [{"kind": "tool_call", "name": "ping", "input": {"text": "NO"}}] * 9}), antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    with pytest.raises(antigravity.CustomLLMError):
        asyncio.run(antigravity.antigravity_handler.acompletion(
            model="gemini-3.8-flash", messages=[{"role": "user", "content": "Use one tool."}],
            optional_params={"tools": TOOLS, "tool_choice": "auto", "parallel_tool_calls": False},
        ))


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("content", [
    'I will use a tool:\n```json\n{"kind":"tool_call","name":"ping","input":{"text":"READ"}}\n```\nA guessed answer.',
    'Here is the decision: {"kind":"text","text":"ANSWER"}',
    r'Here is the decision: {"ki\u006ed":"tool_call","name":"ping","input":{"text":"READ"}}',
    r'Here is the decision: {"kind":"tool\u005fcall","name":"ping","input":{"text":"READ"}}',
])
def test_prose_prefixed_structured_decision_cannot_become_visible_text(monkeypatch, stream, content):
    async def invoke(*args, **kwargs):
        return content, antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    request = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "Use a client tool."}],
               "optional_params": {"tools": TOOLS, "tool_choice": "auto"}}
    with pytest.raises(antigravity.CustomLLMError, match="invalid structured tool JSON"):
        if stream:
            async def collect():
                return [chunk async for chunk in antigravity.antigravity_handler.astreaming(**request)]
            asyncio.run(collect())
        else:
            asyncio.run(antigravity.antigravity_handler.acompletion(**request))


def tiny_wav_attachment():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 1600)
    return {"type": "input_audio", "input_audio": {
        "data": base64.b64encode(output.getvalue()).decode("ascii"), "format": "wav",
    }}


@pytest.mark.parametrize("with_audio", [True, False])
def test_only_explicit_audio_filter_refusal_uses_litellm_content_policy_type(monkeypatch, with_audio):
    async def refuse(*args, **kwargs):
        raise antigravity.AntigravityBridgeError(
            502, "provider_content_filter phase=after_attachment_reads", code="provider_content_filter",
        )

    monkeypatch.setattr(antigravity, "invoke_agy", refuse)
    content = [tiny_wav_attachment()] if with_audio else "hello"
    messages = [{"role": "user", "content": content}]
    if with_audio:
        expected = ContentPolicyViolationError
    else:
        expected = antigravity.CustomLLMError
    with pytest.raises(expected):
        asyncio.run(antigravity.AntigravityLLM._complete("gemini-3.8-flash", messages, {}))


@pytest.mark.parametrize("content", [
    "[]", "null", "42",
    '```json\n{"kind":"text","text":"OK"}\n{}\n```',
    '```json\n{"kind":\n```',
    '{"kind":"text","text":"OK"}\nCommentary: '
    '{"kind":"tool_call","name":"finish","input":{}}'
    '{"other":true}',
])
def test_malformed_structured_output_is_a_provider_error(monkeypatch, content):
    async def invoke(*args, **kwargs):
        return content, antigravity.Usage(prompt_tokens=2, completion_tokens=1, total_tokens=3)

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    with pytest.raises(antigravity.CustomLLMError) as caught:
        asyncio.run(antigravity.antigravity_handler.acompletion(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "hello"}],
            optional_params={"tools": TOOLS, "tool_choice": "auto"},
        ))
    assert caught.value.status_code == 502


def test_openai_tool_history_allows_null_assistant_content():
    prompt = antigravity.prompt_from_messages([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "ping", "arguments": '{"text":"OK"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "pong"},
    ])
    assert "Tool call ping id=call_1" in prompt
    assert "pong" in prompt


def test_external_tool_schema_reference_is_rejected_before_provider_work(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("unsupported schemas must fail before provider dispatch")

    monkeypatch.setattr(antigravity, "invoke_agy", unexpected)
    with pytest.raises(antigravity.CustomLLMError) as caught:
        asyncio.run(antigravity.antigravity_handler.acompletion(
            model="gemini-3.8-flash", messages=[{"role": "user", "content": "hello"}],
            optional_params={"tools": [{"name": "ping", "input_schema": {
                "type": "object", "$ref": "http://untrusted.invalid/schema",
            }}]},
        ))
    assert caught.value.status_code == 400


def test_local_tool_schema_reference_still_validates(monkeypatch):
    async def invoke(*args, **kwargs):
        return '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}', antigravity.Usage(
            prompt_tokens=2, completion_tokens=1, total_tokens=3,
        )

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    response = asyncio.run(antigravity.antigravity_handler.acompletion(
        model="gemini-3.8-flash", messages=[{"role": "user", "content": "hello"}],
        optional_params={"tools": [{"name": "ping", "input_schema": {
            "type": "object", "properties": {"text": {"$ref": "#/$defs/text"}},
            "$defs": {"text": {"type": "string"}}, "required": ["text"],
        }}]},
    ))
    assert response.choices[0].message.tool_calls[0].function.name == "ping"


@pytest.mark.parametrize("stream", [False, True])
def test_backend_managed_output_limit_preserves_completed_output_and_usage(monkeypatch, stream):
    async def invoke(*args, **kwargs):
        return "A complete provider answer.", antigravity.Usage(
            prompt_tokens=4, completion_tokens=6, total_tokens=10,
        )

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    request = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hello"}],
               "optional_params": {"max_tokens": 1}}
    if stream:
        async def collect():
            return [chunk async for chunk in antigravity.antigravity_handler.astreaming(**request)]
        chunks = asyncio.run(collect())
        assert chunks[0]["text"] == "A complete provider answer."
        assert chunks[-1]["usage"]["completion_tokens"] == 6
    else:
        result = asyncio.run(antigravity.antigravity_handler.acompletion(**request))
        assert result.choices[0].message.content == "A complete provider answer."
        assert result.usage.completion_tokens == 6


@pytest.mark.parametrize("payload", [None, [], {"content": "OK", "usage": None}])
def test_invalid_bridge_success_payload_is_a_provider_error(monkeypatch, payload):
    client_type = httpx.AsyncClient
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://bridge.test")
    monkeypatch.setattr(antigravity.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=json.dumps(payload), headers={"content-type": "application/json"}
        )), **kwargs,
    ))
    with pytest.raises(antigravity.CustomLLMError) as caught:
        asyncio.run(antigravity.antigravity_handler.acompletion(
            model="gemini-3.8-flash", messages=[{"role": "user", "content": "hello"}],
            optional_params={},
        ))
    assert caught.value.status_code == 502


def test_subscription_tool_call_returns_unexecuted_litellm_tool_call(monkeypatch):
    captured = {}

    async def invoke(model, prompt, **kwargs):
        captured.update(model=model, prompt=prompt, **kwargs)
        return '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}', antigravity.Usage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    response = asyncio.run(antigravity.antigravity_handler.acompletion(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Use ping with OK."}],
        optional_params={"tools": TOOLS, "tool_choice": {"type": "tool", "name": "ping"}, "reasoning_effort": "low"},
    ))

    choice = response.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.content is None
    assert choice.message.tool_calls[0].function.name == "ping"
    assert json.loads(choice.message.tool_calls[0].function.arguments) == {"text": "OK"}
    assert captured["model"] == "gemini-3.8-flash-low"
    assert "client executes them" in captured["prompt"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("content", [
    'A plain answer without a structured decision.',
    '{"kind":"text","text":"AUTO-OK"}\nAUTO-OK\nAUTO-OK',
    '```json\n{"kind":"text","text":"AUTO-OK"}\n```',
    '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\nI have completed the task.',
    '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n{"kind":"tool_call","name":"finish","input":{}}',
    '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n{"kind":"tool_call","name":"ping","input":{"text":"OK"}}',
    '{"kind":"tool_call","name":"ping","input":{"text":"OK"},"toolAction":"Finishing task"}',
])
def test_lifecycle_prose_and_adjacent_decisions_are_not_native_schema_results(monkeypatch, stream, content):
    async def invoke(*args, **kwargs):
        return content, antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    request = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "Answer or use ping"}],
        "optional_params": {"tools": TOOLS, "tool_choice": "auto"}}
    with pytest.raises(antigravity.CustomLLMError) as caught:
        if stream:
            async def collect():
                seen = []
                try:
                    async for chunk in antigravity.antigravity_handler.astreaming(**request):
                        seen.append(chunk)
                finally:
                    assert seen == []
            asyncio.run(collect())
        else:
            asyncio.run(antigravity.antigravity_handler.acompletion(**request))
    assert caught.value.status_code == 502


def test_auto_text_decision_preserves_exact_answer_and_literal_escapes(monkeypatch):
    answer = 'An ordinary answer.\n中文 😀\n```python\nx = r"\\n"\n```'
    async def invoke(*args, **kwargs):
        return json.dumps({"kind": "text", "text": answer}), antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    response = asyncio.run(antigravity.antigravity_handler.acompletion(
        model="gemini-3.8-flash", messages=[{"role": "user", "content": "Answer without tools"}],
        optional_params={"tools": TOOLS, "tool_choice": "auto"},
    ))
    assert response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.content == answer
    assert response.choices[0].message.tool_calls is None


@pytest.mark.parametrize("second,options,count", [
    ({"kind": "tool_call", "name": "unknown", "input": {}}, {}, 2),
    ({"kind": "tool_call", "name": "ping", "input": {"text": 42}}, {}, 2),
    ({"kind": "tool_call", "name": "ping", "input": {"text": "SECOND"}}, {"parallel_tool_calls": False}, 2),
    ({"kind": "text", "text": "MIXED"}, {}, 2),
    ({"kind": "tool_call", "name": "ping", "input": {"text": "SECOND"}}, {}, 9),
    ({"kind": "tool_call", "name": "ping", "input": {"text": "SECOND"}}, {"tool_choice": "required"}, 2),
])
def test_invalid_native_batch_validates_every_call_before_streaming(monkeypatch, second, options, count):
    first = {"kind": "tool_call", "name": "ping", "input": {"text": "FIRST"}}
    async def invoke(*args, **kwargs):
        return json.dumps({"kind": "tool_calls", "calls": [first] + [second] * (count - 1)}), antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    async def collect():
        seen = []
        with pytest.raises(antigravity.CustomLLMError) as caught:
            async for chunk in antigravity.antigravity_handler.astreaming(
                model="gemini-3.8-flash", messages=[{"role": "user", "content": "Use tools"}],
                optional_params={"tools": TOOLS, "tool_choice": "auto", **options},
            ):
                seen.append(chunk)
        assert seen == [] and caught.value.status_code == 502
    asyncio.run(collect())


def test_subscription_stream_emits_generic_tool_use_chunk(monkeypatch):
    async def invoke(model, prompt, **kwargs):
        return '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}', antigravity.Usage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)

    async def collect():
        return [chunk async for chunk in antigravity.antigravity_handler.astreaming(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "Use ping."}],
            optional_params={"tools": TOOLS, "tool_choice": "required"},
        )]

    chunks = asyncio.run(collect())
    assert chunks[0]["tool_use"]["type"] == "function"
    assert chunks[0]["tool_use"]["function"]["name"] == "ping"
    assert chunks[-1]["is_finished"] is True
    assert chunks[-1]["finish_reason"] == "tool_calls"


def test_subscription_sse_adapter_waits_for_cli_completion_before_emitting(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def invoke(model, prompt, **kwargs):
        started.set()
        await release.wait()
        return "completed answer", antigravity.Usage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)

    async def check():
        stream = antigravity.antigravity_handler.astreaming(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "hello"}],
            optional_params={},
        )
        first_chunk = asyncio.create_task(anext(stream))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not first_chunk.done()

        release.set()
        chunk = await asyncio.wait_for(first_chunk, timeout=1)
        await stream.aclose()
        assert chunk["text"] == "completed answer"

    asyncio.run(check())


def test_subscription_rejects_arguments_outside_declared_tool_schema(monkeypatch):
    async def invoke(model, prompt, **kwargs):
        return '{"kind":"tool_call","name":"ping","input":{"text":"OK","extra":true}}', antigravity.Usage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    with pytest.raises(Exception, match="Additional properties are not allowed"):
        asyncio.run(antigravity.antigravity_handler.acompletion(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "Use ping."}],
            optional_params={"tools": TOOLS, "tool_choice": "required"},
        ))


def test_subscription_formats_tool_result_history_without_dropping_it():
    prompt = antigravity.prompt_from_messages([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "ping", "input": {"text": "OK"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "pong"}]},
    ])

    assert "Tool call ping id=call_1" in prompt
    assert "[Tool result id=call_1]: pong" in prompt


def test_subscription_rejects_unsupported_tool_content_blocks():
    with pytest.raises(ValueError, match="does not support 'image'"):
        antigravity.prompt_from_messages([{"role": "user", "content": [{"type": "image"}]}])


def test_bridge_capacity_status_is_preserved_as_gateway_429(monkeypatch):
    async def full_bridge(*args, **kwargs):
        raise antigravity.AntigravityBridgeError(429, "Antigravity bridge returned 429: CLI capacity")

    monkeypatch.setattr(antigravity, "invoke_agy", full_bridge)

    with pytest.raises(antigravity.CustomLLMError) as error:
        asyncio.run(antigravity.antigravity_handler.acompletion(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "hello"}],
            optional_params={},
        ))

    assert error.value.status_code == 429
    assert "CLI capacity" in str(error.value)


@pytest.mark.parametrize(
    "error_type,error_message,status_code",
    [
        (TimeoutError, "bridge request timed out", 504),
        (RuntimeError, "bridge connection failed", 502),
    ],
)
def test_target_transport_failures_keep_gateway_status(monkeypatch, error_type, error_message, status_code):
    async def fail(*args, **kwargs):
        raise error_type(error_message)

    monkeypatch.setattr(antigravity, "invoke_agy", fail)
    with pytest.raises(antigravity.CustomLLMError) as caught:
        asyncio.run(antigravity.antigravity_handler.acompletion(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "hello"}],
            optional_params={},
        ))
    assert caught.value.status_code == status_code


def test_bridge_failure_carries_transport_request_id(monkeypatch):
    captured = {}
    client_type = httpx.AsyncClient

    def respond(request):
        captured["request_id"] = request.headers.get("X-Subroute-Request-ID")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            502,
            json={"detail": "upstream failed"},
            headers={"X-Subroute-Request-ID": captured["request_id"]},
        )

    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://bridge.test")
    monkeypatch.setattr(
        antigravity.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )

    with pytest.raises(antigravity.AntigravityBridgeError) as caught:
        asyncio.run(antigravity.invoke_agy("gemini-3.8-flash", "hello"))

    assert captured["request_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", captured["request_id"])
    assert captured["payload"] == {
        "model": "Gemini 3.8 Flash (High)",
        "prompt": "hello",
        "json_schema": None,
    }
    assert captured["request_id"] not in json.dumps(captured["payload"])
    assert f"request_id={captured['request_id']}" in str(caught.value)


@pytest.mark.parametrize("usage,completed_reads,expected_usage,expected_reads", [
    ({"input_tokens": 10, "output_tokens": 2, "total_tokens": 15}, 1, "provider_usage=10,2,15", "completed_reads=1"),
    ({"input_tokens": True, "output_tokens": 2, "total_tokens": 15}, True, None, None),
    ({"input_tokens": -1, "output_tokens": 2, "total_tokens": 15}, -1, None, None),
    (None, "1", None, None),
])
def test_filter_error_preserves_only_validated_diagnostics(monkeypatch, usage, completed_reads, expected_usage, expected_reads):
    client_type = httpx.AsyncClient
    def respond(req):
        return httpx.Response(502, json={"detail": "provider_content_filter phase=after_attachment_reads conversation_id=fixture",
                                      "code": "provider_content_filter", "provider_usage": usage,
                                      "completed_reads": completed_reads})
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://fixture")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(antigravity.AntigravityBridgeError) as caught:
        asyncio.run(antigravity.invoke_agy("gemini-3.8-flash", "listen"))
    assert caught.value.status_code == 502
    if expected_usage:
        assert expected_usage in str(caught.value)
    else:
        assert "provider_usage=" not in str(caught.value)
    if expected_reads:
        assert expected_reads in str(caught.value)
    else:
        assert "completed_reads=" not in str(caught.value)
