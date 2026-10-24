"""Caller-visible model restoration across mutable responses and streams."""
import asyncio
import json

from litellm.types.utils import ModelResponse, ModelResponseStream

from subroute.plugins.dynamic_router import _stamp_requested_model


def test_mutable_response_and_chunk_keep_requested_alias():
    alias = "openrouter-flash"
    response = ModelResponse(model="provider/model", choices=[])
    chunk = ModelResponseStream(model="provider/model", choices=[])

    _stamp_requested_model(response, alias)
    _stamp_requested_model(chunk, alias)

    assert response.model == chunk.model == alias


def test_messages_start_event_restores_nested_model_without_changing_payload():
    event = {"type": "message_start", "message": {"model": "provider/model", "content": []}}

    _stamp_requested_model(event, "legacy-alias")

    assert event == {"type": "message_start", "message": {"model": "legacy-alias", "content": []}}


def test_read_only_stream_is_not_assigned_or_consumed():
    class ReadOnlyStream:
        def __init__(self):
            self.advanced = False

        @property
        def model(self):
            return "provider/model"

        def __iter__(self):
            self.advanced = True
            yield {"model": "provider/model"}

    stream = ReadOnlyStream()

    _stamp_requested_model(stream, "caller-alias")

    assert stream.model == "provider/model"
    assert stream.advanced is False


def test_mutable_model_setter_errors_are_not_swallowed():
    class MutableButFailing:
        @property
        def model(self):
            return "provider/model"

        @model.setter
        def model(self, value):
            raise AttributeError("setter bug")

    import pytest

    with pytest.raises(AttributeError, match="setter bug"):
        _stamp_requested_model(MutableButFailing(), "caller-alias")


def test_native_anthropic_stream_preserves_lifecycle_and_stamps_message_start():
    from litellm.llms.anthropic.pass_through.adapters.streaming_iterator import (
        AnthropicSSEStream,
        AnthropicStreamWrapper,
    )
    from subroute.plugins.dynamic_router import DynamicRoutingPlugin

    lifecycle = {"advanced": 0, "closed": 0}

    async def source():
        try:
            lifecycle["advanced"] += 1
            yield ModelResponseStream(
                id="fixture", model="provider/model",
                choices=[{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}],
            )
            lifecycle["advanced"] += 1
            yield ModelResponseStream(
                id="fixture", model="provider/model",
                choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
            )
        finally:
            lifecycle["closed"] += 1

    async def check():
        stream = AnthropicSSEStream(AnthropicStreamWrapper(source(), model="provider/model"))
        assert isinstance(type(stream).model, property) and type(stream).model.fset is None
        _stamp_requested_model(stream, "caller-alias")
        assert lifecycle == {"advanced": 0, "closed": 0}

        plugin = DynamicRoutingPlugin(object())
        frames = [frame async for frame in plugin.async_post_call_streaming_iterator_hook(
            None, stream, {"model": "caller-alias", "metadata": {"routing": {"requested_model": "caller-alias"}}}
        )]
        events = [json.loads(line[6:]) for frame in frames for line in frame.decode().splitlines() if line.startswith("data: ")]
        start = next(event for event in events if event["type"] == "message_start")
        assert start["message"]["model"] == "caller-alias"
        assert lifecycle == {"advanced": 2, "closed": 1}

    asyncio.run(check())
