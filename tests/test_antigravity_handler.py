import asyncio
import json
import re

import httpx
import pytest

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


def test_subscription_accepts_only_the_known_agy_structured_output_footer(monkeypatch):
    async def invoke(model, prompt, **kwargs):
        content = (
            '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n'
            "I do not have access to any external execution tools or a finish tool in this environment. "
            "If you need any further assistance, please let me know."
        )
        return content, antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    response = asyncio.run(antigravity.antigravity_handler.acompletion(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Use ping."}],
        optional_params={"tools": TOOLS, "tool_choice": "required"},
    ))

    assert response.choices[0].finish_reason == "tool_calls"
    assert response.choices[0].message.tool_calls[0].function.name == "ping"


def test_subscription_auto_tool_choice_preserves_a_plain_text_answer(monkeypatch):
    content = "Here is the answer.\nI have completed the task."

    async def invoke(model, prompt, **kwargs):
        return content, antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    response = asyncio.run(antigravity.antigravity_handler.acompletion(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Answer without using the available tools."}],
        optional_params={"tools": TOOLS, "tool_choice": "auto"},
    ))

    assert response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.content == content
    assert response.choices[0].message.tool_calls is None


def test_subscription_extracts_text_decision_from_agy_fenced_result(monkeypatch):
    content = (
        '```json\n{"kind":"text","text":"AUTO-OK"}\n```\n'
        '```json\n{"kind":"tool_call","name":"finish","input":{}}\n```\n'
        "AUTO-OK\nAUTO-OK"
    )

    async def invoke(model, prompt, **kwargs):
        return content, antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    response = asyncio.run(antigravity.antigravity_handler.acompletion(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Answer without tools."}],
        optional_params={"tools": TOOLS, "tool_choice": "auto"},
    ))

    assert response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.content == "AUTO-OK"
    assert response.choices[0].message.tool_calls is None


def test_subscription_ignores_plain_postamble_after_structured_tool_call(monkeypatch):
    async def invoke(model, prompt, **kwargs):
        return (
            '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n'
            "This is unrelated provider output."
        ), antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    response = asyncio.run(antigravity.antigravity_handler.acompletion(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Use ping."}],
        optional_params={"tools": TOOLS, "tool_choice": "required"},
    ))

    assert response.choices[0].message.tool_calls[0].function.name == "ping"


def test_subscription_rejects_client_tool_json_embedded_in_plain_postamble(monkeypatch):
    async def invoke(model, prompt, **kwargs):
        return (
            '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n'
            'Additional result: {"kind":"tool_call","name":"delete_file","input":{"path":"important.txt"}}'
        ), antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    with pytest.raises(Exception, match="different additional structured result"):
        asyncio.run(antigravity.antigravity_handler.acompletion(
            model="gemini-3.8-flash",
            messages=[{"role": "user", "content": "Use ping."}],
            optional_params={"tools": TOOLS, "tool_choice": "required"},
        ))


def test_subscription_accepts_known_no_tools_cli_completion_postamble(monkeypatch):
    async def invoke(model, prompt, **kwargs):
        return (
            '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n'
            "I do not have access to any external execution tools or a finish tool in this environment. "
            "If you need any further assistance, please let me know.\n"
            "I do not have access to a finish tool or other external execution tools. "
            "If there is anything else you need help with directly, please let me know.\n"
            "I do not have access to any additional tools, including a finish tool. The requested task is complete.\n"
            "There are no available tools to call in this environment, and the task has been completed."
        ), antigravity.Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    monkeypatch.setattr(antigravity, "invoke_agy", invoke)
    response = asyncio.run(antigravity.antigravity_handler.acompletion(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "Use ping."}],
        optional_params={"tools": TOOLS, "tool_choice": "required"},
    ))

    assert response.choices[0].message.tool_calls[0].function.name == "ping"


def test_subscription_strips_only_agy_internal_finish_marker():
    output = (
        '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n'
        '{"kind":"tool_call","name":"finish","input":{}}\n'
        "I have completed the task.\n"
        "All requested tasks have been completed."
    )

    assert antigravity.antigravity_handler._parse_tool_decision(output) == {
        "kind": "tool_call", "name": "ping", "input": {"text": "OK"}
    }


def test_subscription_accepts_repeated_identical_agy_tool_decisions():
    item = '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}'
    output = "\n".join([item] * 4)

    assert antigravity.antigravity_handler._parse_tool_decision(output) == {
        "kind": "tool_call", "name": "ping", "input": {"text": "OK"}
    }


def test_subscription_strips_finish_marker_with_runtime_metadata():
    output = (
        '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n'
        '{"kind":"tool_call","name":"finish","input":{},"id":"internal-finish"}'
    )

    assert antigravity.antigravity_handler._parse_tool_decision(output)["name"] == "ping"


def test_subscription_accepts_agy_completion_footer_variants():
    output = (
        '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n'
        "I have completed the task.\n"
        "The finish tool is not available in this environment. I have completed the task.\n"
        "I have completed all requested tasks. There is no finish tool available to call."
    )

    assert antigravity.antigravity_handler._parse_tool_decision(output)["name"] == "ping"


def test_subscription_does_not_strip_a_second_client_tool_call():
    output = (
        '{"kind":"tool_call","name":"ping","input":{"text":"OK"}}\n'
        '{"kind":"tool_call","name":"delete_file","input":{"path":"important.txt"}}'
    )

    with pytest.raises(Exception, match="additional structured result"):
        antigravity.antigravity_handler._parse_tool_decision(output)


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
