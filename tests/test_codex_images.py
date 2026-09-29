import asyncio
import base64

import pytest
from litellm.llms.custom_llm import CustomLLMError

from subroute.handlers import codex_images
from subroute.handlers.codex_subscription import CodexSubscriptionLLM
from subroute.plugins.dynamic_router import DynamicRoutingPlugin
from test_dynamic_routing import make_control_plane


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()


def image_event(result=PNG):
    return {"type": "response.output_item.done", "item": {
        "type": "image_generation_call", "id": "ig_fixture", "status": "completed",
        "result": result, "size": "1024x1024", "output_format": "png",
    }}


def terminal(status="completed"):
    return {"type": "response.completed", "response": {
        "id": "resp_fixture", "model": "gpt-6-luna", "status": status, "output": [],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }}


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.response = self

    async def aclose(self):
        self.closed = True

    async def __aiter__(self):
        for event in self.events:
            yield event


def stub(monkeypatch, events):
    stream = Stream(events)
    calls = []

    async def aresponses(**kwargs):
        calls.append(kwargs)
        return stream

    monkeypatch.setattr(codex_images, "read_codex_credentials", lambda: ("fixture", "account"))
    monkeypatch.setattr(codex_images.litellm, "aresponses", aresponses)
    return stream, calls


@pytest.mark.parametrize("mode", ["force", "alias", "off"])
@pytest.mark.parametrize("model", ["minimax", "auto", "gpt-image-2", None])
def test_image_route_precedes_every_saved_policy(tmp_path, mode, model):
    control = make_control_plane(tmp_path)
    control.update("minimax", mode)
    before = control.snapshot()
    plugin = DynamicRoutingPlugin(control)
    data = {"model": model, "prompt": "a circle", "quality": "low", "size": "auto"}
    result = asyncio.run(plugin.async_pre_call_hook({}, None, data, "aimage_generation"))
    assert result["model"] == "codex-luna"
    assert result["image_generation_options"] == {"quality": "low", "size": "auto"}
    assert control.snapshot() == before


@pytest.mark.parametrize("options", [{"n": 2}, {"response_format": "url"}, {"style": "vivid"}, {"stream": True}, {"size": "1024x1024"}])
def test_unsupported_options_fail_before_dispatch(options):
    with pytest.raises(CustomLLMError) as caught:
        codex_images.image_options(options)
    assert caught.value.status_code == 400


def test_image_in_output_item_survives_empty_terminal_output(monkeypatch):
    completed = terminal()
    completed["response"]["usage"]["cost"] = 0.01
    stream, calls = stub(monkeypatch, [image_event(), completed])
    result = asyncio.run(codex_images.generate_image("a circle", {"quality": "low"}))
    assert result.data[0].b64_json == PNG
    assert result.model == "gpt-6-luna"
    assert result.usage is None
    assert result.luna_usage["total_tokens"] == 15
    assert "cost" not in result.luna_usage
    assert result.image_usage is None
    assert stream.closed
    assert len(calls) == 1
    assert calls[0]["tools"] == [{"type": "image_generation", "quality": "low"}]
    assert calls[0]["model"] == "openai/gpt-6-luna"
    assert calls[0]["num_retries"] == 0


def test_reject_response_from_different_model(monkeypatch):
    completed = terminal()
    completed["response"]["model"] = "another-model"
    stream, calls = stub(monkeypatch, [image_event(), completed])
    with pytest.raises(CustomLLMError, match="Luna model"):
        asyncio.run(codex_images.generate_image("circle", {}))
    assert stream.closed


def test_accept_image_present_only_in_terminal(monkeypatch):
    completed = terminal()
    completed["response"]["output"] = [image_event()["item"]]
    stub(monkeypatch, [completed])
    result = asyncio.run(codex_images.generate_image("circle", {}))
    assert result.data[0].b64_json == PNG


@pytest.mark.parametrize("events", [
    [terminal()], [image_event()], [image_event(), terminal("incomplete")],
    [image_event("invalid"), terminal()], [image_event(base64.b64encode(b"not an image").decode()), terminal()],
    [{"type": "response.failed"}],
])
def test_no_false_success_and_always_close(monkeypatch, events):
    stream, calls = stub(monkeypatch, events)
    with pytest.raises(CustomLLMError) as caught:
        asyncio.run(codex_images.generate_image("a circle", {}))
    assert caught.value.status_code == 502
    assert stream.closed
    assert len(calls) == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_close_stream(monkeypatch, cancel):
    class WaitingStream(Stream):
        async def __aiter__(self):
            await asyncio.sleep(100)
            yield terminal()

    stream = WaitingStream([])

    async def aresponses(**kwargs):
        return stream

    monkeypatch.setattr(codex_images, "read_codex_credentials", lambda: ("fixture", "account"))
    monkeypatch.setattr(codex_images.litellm, "aresponses", aresponses)
    monkeypatch.setattr(codex_images, "IMAGE_TIMEOUT", .03)

    async def run():
        task = asyncio.create_task(codex_images.generate_image("a circle", {}))
        await asyncio.sleep(.01)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else CustomLLMError):
            await task

    asyncio.run(run())
    assert stream.closed


def test_concurrent_image_requests_are_bounded_and_slots_recover(monkeypatch):
    async def run():
        gate = asyncio.Event()
        handler = CodexSubscriptionLLM()

        async def generate(*args):
            await gate.wait()
            return "image"

        monkeypatch.setattr(codex_images, "generate_image", generate)
        async def call():
            return await handler.aimage_generation("gpt-6-luna", "circle", None, None, None, {}, None)

        first = asyncio.create_task(call())
        second = asyncio.create_task(call())
        await asyncio.sleep(0)
        with pytest.raises(CustomLLMError) as caught:
            await call()
        assert caught.value.status_code == 429
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        gate.set()
        assert await second == "image"
        assert await call() == "image"
        assert handler.active_image_requests == 0

    asyncio.run(run())


def test_proxy_images_force_exception_preserves_options(monkeypatch, tmp_path):
    from test_codex_proxy_http import _configured_proxy

    stream, calls = stub(monkeypatch, [image_event(), terminal()])
    control = make_control_plane(tmp_path)
    control.update("minimax", "force")
    plugin = DynamicRoutingPlugin(control)
    with _configured_proxy(dynamic_routing_plugin=plugin) as (client, *_):
        for path in ("/v1/images/generations", "/images/generations"):
            for model in ("minimax", "gpt-image-2", None):
                payload = {"prompt": "a blue circle", "n": 1, "quality": "low",
                           "size": "auto", "response_format": "b64_json"}
                if model is not None:
                    payload["model"] = model
                result = client.post(path, json=payload)
                assert result.status_code == 200, result.text
                assert result.json()["data"][0]["b64_json"] == PNG
        for invalid in ({"n": 2}, {"size": "1024x1024"}, {"response_format": "url"}):
            result = client.post("/v1/images/generations", json={"model": "auto", "prompt": "circle", **invalid})
            assert result.status_code == 400, result.text
    assert calls[0]["tools"] == [{"type": "image_generation", "quality": "low", "size": "auto"}]
    assert len(calls) == 6
    assert stream.closed
