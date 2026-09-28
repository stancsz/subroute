import asyncio

import litellm
import pytest
from litellm.llms.custom_llm import CustomLLMError
from litellm.responses.litellm_completion_transformation.transformation import (
    LiteLLMCompletionResponsesConfig,
)

from subroute.handlers.codex_subscription import codex_subscription_handler


class FakeStream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


def tool_call_chunks():
    return [
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-6-luna",
            "choices": [{
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_fixture",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{\"q\":1}"},
                    }],
                },
                "finish_reason": None,
            }],
        },
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-6-luna",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-6-luna",
            "choices": [],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        },
    ]


def call_handler(**overrides):
    args = {
        "model": "gpt-6-luna",
        "messages": [{"role": "user", "content": "look this up"}],
        "api_base": "https://chatgpt.com/backend-api/codex",
        "custom_prompt_dict": {},
        "model_response": litellm.types.utils.ModelResponse(),
        "print_verbose": lambda *_: None,
        "encoding": None,
        "api_key": "fixture-key",
        "logging_obj": None,
        "optional_params": {
            "stream": False, "max_tokens": 64, "max_output_tokens": 96,
            "user": "client-user", "store": False,
        },
        "headers": {"User-Agent": "fixture"},
        "timeout": 12,
    }
    args.update(overrides)
    return asyncio.run(codex_subscription_handler.acompletion(**args))


def test_nonstreaming_collects_native_chat_stream_with_tool_usage_and_terminal_state(monkeypatch):
    captured = {}
    stream = FakeStream(tool_call_chunks())

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    response = call_handler()

    assert captured["model"] == "openai/responses/gpt-6-luna"
    assert captured["stream"] is True
    assert captured["num_retries"] == 0
    assert captured["timeout"] == 12
    assert captured["api_base"] == "https://chatgpt.com/backend-api/codex"
    assert "max_tokens" not in captured
    assert "max_output_tokens" not in captured
    assert "user" not in captured
    assert captured["store"] is False
    assert response.model == "gpt-6-luna"
    assert response.choices[0].finish_reason == "tool_calls"
    assert response.choices[0].message.tool_calls[0].function.name == "lookup"
    assert response.choices[0].message.tool_calls[0].function.arguments == '{"q":1}'
    assert response.usage.total_tokens == 6
    assert stream.closed


def test_nonstreaming_rejects_a_stream_without_a_terminal_finish_reason(monkeypatch):
    stream = FakeStream([{
        "id": "chatcmpl-fixture",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-6-luna",
        "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
    }])

    async def fake_acompletion(**kwargs):
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    with pytest.raises(CustomLLMError, match="without a terminal finish reason"):
        call_handler()
    assert stream.closed


def test_nonstreaming_does_not_label_estimated_usage_as_provider_usage(monkeypatch):
    stream = FakeStream([
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-6-luna",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-6-luna",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ])

    async def fake_acompletion(**kwargs):
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    response = call_handler()

    assert response.choices[0].message.content == "done"
    assert response.usage is None


def test_streaming_passes_native_chunks_through_and_closes_on_cancellation(monkeypatch):
    stream = FakeStream(tool_call_chunks())
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    iterator = codex_subscription_handler.astreaming(
        model="gpt-6-luna",
        messages=[{"role": "user", "content": "look this up"}],
        api_base="https://chatgpt.com/backend-api/codex",
        custom_prompt_dict={},
        model_response=litellm.types.utils.ModelResponse(),
        print_verbose=lambda *_: None,
        encoding=None,
        api_key="fixture-key",
        logging_obj=None,
        optional_params={"stream": True},
        headers={"User-Agent": "fixture"},
        timeout=12,
    )
    first = asyncio.run(iterator.__anext__())
    asyncio.run(iterator.aclose())

    assert first["tool_use"]["id"] == "call_fixture"
    assert first["is_finished"] is False
    assert captured["stream"] is True
    assert stream.closed


def test_streaming_rejects_missing_terminal_finish_reason_and_closes_upstream(monkeypatch):
    stream = FakeStream([{
        "id": "chatcmpl-fixture",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-6-luna",
        "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
    }])

    async def fake_acompletion(**kwargs):
        assert kwargs["stream"] is True
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    iterator = codex_subscription_handler.astreaming(
        model="gpt-6-luna",
        messages=[{"role": "user", "content": "say hello"}],
        api_base="https://chatgpt.com/backend-api/codex",
        custom_prompt_dict={},
        model_response=litellm.types.utils.ModelResponse(),
        print_verbose=lambda *_: None,
        encoding=None,
        api_key="fixture-key",
        logging_obj=None,
        optional_params={"stream": True},
        headers={"User-Agent": "fixture"},
        timeout=12,
    )

    async def consume():
        with pytest.raises(CustomLLMError, match="without a terminal finish reason"):
            _ = [chunk async for chunk in iterator]

    asyncio.run(consume())
    assert stream.closed


def test_litellm_responses_bridge_preserves_roleless_tool_items():
    items = [
        {
            "type": "function_call",
            "call_id": "call_fixture",
            "name": "lookup",
            "arguments": '{"q":1}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_fixture",
            "output": "found",
        },
    ]

    messages = LiteLLMCompletionResponsesConfig.transform_responses_api_input_to_messages(
        input=items,
        responses_api_request={},
    )

    assert messages == [
        {
            "tool_calls": [{
                "id": "call_fixture",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":1}'},
                "index": 0,
            }],
            "role": "assistant",
            "content": None,
        },
        {"role": "tool", "content": "found", "tool_call_id": "call_fixture"},
    ]


def test_litellm_chat_dispatch_uses_registered_codex_adapter(monkeypatch):
    original_acompletion = litellm.acompletion
    monkeypatch.setattr(litellm, "custom_provider_map", [{
        "provider": "codex-subscription",
        "custom_handler": codex_subscription_handler,
    }])
    stream = FakeStream(tool_call_chunks())

    async def fake_inner_acompletion(**kwargs):
        assert kwargs["model"] == "openai/responses/gpt-6-luna"
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_inner_acompletion)
    response = asyncio.run(original_acompletion(
        model="codex-subscription/gpt-6-luna",
        messages=[{"role": "user", "content": "look this up"}],
        api_base="https://chatgpt.com/backend-api/codex",
        api_key="fixture-key",
        extra_headers={"ChatGPT-Account-ID": "fixture-account"},
        stream=False,
        store=False,
        max_tokens=64,
    ))

    assert response.choices[0].finish_reason == "tool_calls"
    assert response.choices[0].message.tool_calls[0].id == "call_fixture"
    assert response.usage.total_tokens == 6
    assert stream.closed


def test_litellm_chat_stream_dispatch_emits_tool_and_terminal_chunks(monkeypatch):
    original_acompletion = litellm.acompletion
    monkeypatch.setattr(litellm, "custom_provider_map", [{
        "provider": "codex-subscription",
        "custom_handler": codex_subscription_handler,
    }])
    stream = FakeStream(tool_call_chunks())

    async def fake_inner_acompletion(**kwargs):
        if kwargs.get("custom_llm_provider") == "codex-subscription":
            return await original_acompletion(**kwargs)
        assert kwargs["model"].startswith("openai/responses/")
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_inner_acompletion)

    async def collect_chunks():
        response_stream = await original_acompletion(
            model="codex-subscription/gpt-6-luna",
            messages=[{"role": "user", "content": "look this up"}],
            api_base="https://chatgpt.com/backend-api/codex",
            api_key="fixture-key",
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks = []
        async for chunk in response_stream:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect_chunks())

    assert chunks[0].choices[0].delta.tool_calls[0].id == "call_fixture"
    assert chunks[0].model == "gpt-6-luna"
    assert any(chunk.choices[0].finish_reason == "tool_calls" for chunk in chunks)
    assert any(getattr(chunk, "usage", None) and chunk.usage.total_tokens == 6 for chunk in chunks)
    assert stream.closed


def test_litellm_responses_dispatch_preserves_roleless_tool_history(monkeypatch):
    original_acompletion = litellm.acompletion
    original_aresponses = litellm.aresponses
    monkeypatch.setattr(litellm, "custom_provider_map", [{
        "provider": "codex-subscription",
        "custom_handler": codex_subscription_handler,
    }])
    stream = FakeStream(tool_call_chunks())
    received_messages = []
    nested_call = {}
    original_handler_acompletion = codex_subscription_handler.acompletion

    async def capture_handler_messages(**kwargs):
        received_messages.extend(kwargs["messages"])
        return await original_handler_acompletion(**kwargs)

    monkeypatch.setattr(codex_subscription_handler, "acompletion", capture_handler_messages)

    async def fake_inner_acompletion(**kwargs):
        if kwargs["model"].startswith("openai/responses/"):
            nested_call.update(kwargs)
            return stream
        return await original_acompletion(**kwargs)

    monkeypatch.setattr(litellm, "acompletion", fake_inner_acompletion)
    response = asyncio.run(original_aresponses(
        model="codex-subscription/gpt-6-luna",
        input=[
            {"type": "function_call", "call_id": "call_prior", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_prior", "output": "previous result"},
            {"role": "user", "content": "continue"},
        ],
        instructions="Use the supplied lookup tool.",
        max_output_tokens=64,
        reasoning={"effort": "high"},
        allowed_openai_params=["reasoning_effort"],
        user="request-owner",
        api_base="https://chatgpt.com/backend-api/codex",
        api_key="fixture-key",
        extra_headers={"ChatGPT-Account-ID": "fixture-account"},
        stream=False,
        store=False,
    ))

    assert received_messages, f"response={response!r}"
    assert received_messages[0] == {
        "role": "system", "content": "Use the supplied lookup tool."
    }
    assert received_messages[1]["role"] == "assistant"
    assert received_messages[1]["tool_calls"][0]["id"] == "call_prior"
    assert received_messages[1]["tool_calls"][0]["function"]["name"] == "lookup"
    assert received_messages[2] == {
        "role": "tool", "content": "previous result", "tool_call_id": "call_prior"
    }
    assert received_messages[3]["role"] == "user"
    assert nested_call["stream"] is True
    assert "max_tokens" not in nested_call
    assert "max_output_tokens" not in nested_call
    assert nested_call["reasoning_effort"] == "high"
    assert "user" not in nested_call
    assert response.status == "completed"
    assert response.model == "gpt-6-luna"
    assert stream.closed


def test_litellm_responses_streaming_dispatch_emits_terminal_events(monkeypatch):
    original_acompletion = litellm.acompletion
    original_aresponses = litellm.aresponses
    monkeypatch.setattr(litellm, "custom_provider_map", [{
        "provider": "codex-subscription",
        "custom_handler": codex_subscription_handler,
    }])
    stream = FakeStream(tool_call_chunks())

    async def fake_inner_acompletion(**kwargs):
        if kwargs.get("custom_llm_provider") == "codex-subscription":
            return await original_acompletion(**kwargs)
        assert kwargs["model"].startswith("openai/responses/")
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_inner_acompletion)

    async def collect_events():
        result = await original_aresponses(
            model="codex-subscription/gpt-6-luna",
            input="look this up",
            api_base="https://chatgpt.com/backend-api/codex",
            api_key="fixture-key",
            extra_headers={"ChatGPT-Account-ID": "fixture-account"},
            stream=True,
            store=False,
        )
        events = []
        async for event in result:
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    event_types = [event.type for event in events]

    assert "response.output_item.added" in event_types
    assert "response.function_call_arguments.done" in event_types
    assert event_types[-1] == "response.completed"
    assert events[-1].response.model == "gpt-6-luna"
    assert events[-1].response.usage.total_tokens == 6
    assert stream.closed


def test_litellm_messages_nonstream_dispatch_returns_tool_use(monkeypatch):
    original_acompletion = litellm.acompletion
    original_anthropic_acreate = litellm.anthropic.acreate
    monkeypatch.setattr(litellm, "custom_provider_map", [{
        "provider": "codex-subscription",
        "custom_handler": codex_subscription_handler,
    }])
    stream = FakeStream(tool_call_chunks())

    async def fake_inner_acompletion(**kwargs):
        if kwargs.get("custom_llm_provider") == "codex-subscription":
            return await original_acompletion(**kwargs)
        assert kwargs["model"].startswith("openai/responses/")
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_inner_acompletion)
    response = asyncio.run(original_anthropic_acreate(
        model="codex-subscription/gpt-6-luna",
        messages=[{"role": "user", "content": "look this up"}],
        max_tokens=64,
        api_base="https://chatgpt.com/backend-api/codex",
        api_key="fixture-key",
    ))

    content = response["content"] if isinstance(response, dict) else response.content
    usage = response["usage"] if isinstance(response, dict) else response.usage
    stop_reason = response["stop_reason"] if isinstance(response, dict) else response.stop_reason
    tool_use = content[0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["id"] == "call_fixture"
    assert tool_use["name"] == "lookup"
    assert usage["input_tokens"] == 4
    assert stop_reason == "tool_use"
    assert response["model"] == "gpt-6-luna"
    assert stream.closed


def test_litellm_messages_stream_dispatch_emits_terminal_event(monkeypatch):
    original_acompletion = litellm.acompletion
    original_anthropic_acreate = litellm.anthropic.acreate
    monkeypatch.setattr(litellm, "custom_provider_map", [{
        "provider": "codex-subscription",
        "custom_handler": codex_subscription_handler,
    }])
    stream = FakeStream(tool_call_chunks())

    async def fake_inner_acompletion(**kwargs):
        if kwargs.get("custom_llm_provider") == "codex-subscription":
            return await original_acompletion(**kwargs)
        assert kwargs["model"].startswith("openai/responses/")
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake_inner_acompletion)

    async def collect_events():
        response_stream = await original_anthropic_acreate(
            model="codex-subscription/gpt-6-luna",
            messages=[{"role": "user", "content": "look this up"}],
            max_tokens=64,
            api_base="https://chatgpt.com/backend-api/codex",
            api_key="fixture-key",
            stream=True,
        )
        events = []
        async for event in response_stream:
            events.append(event)
        return events

    events = asyncio.run(collect_events())
    event_types = []
    for event in events:
        if hasattr(event, "type"):
            event_types.append(event.type)
        elif isinstance(event, dict):
            event_types.append(event["type"])
        else:
            text = event.decode() if isinstance(event, bytes) else str(event)
            event_types.extend(
                line.removeprefix("event: ").strip()
                for line in text.splitlines()
                if line.startswith("event: ")
            )

    assert "content_block_start" in event_types
    assert "content_block_delta" in event_types
    assert event_types[-1] == "message_stop"
    assert any(
        (event.get("message", {}).get("model") == "gpt-6-luna")
        for event in events
        if isinstance(event, dict)
    ) or any(
        b'"model": "gpt-6-luna"' in event
        for event in events
        if isinstance(event, bytes)
    )
    assert stream.closed
