import asyncio
import copy
import json
from pathlib import Path

import httpx
import litellm
import pytest
import yaml

from subroute.handlers import codex_advisor as advisor


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
IMAGE_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/N0sAAAAASUVORK5CYII="
)


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


def test_reserve_advisor_alias_uses_luna_reserve_model(monkeypatch):
    sent = {}

    def respond(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, text="".join(
            "data: " + json.dumps(event) + "\n\n"
            for event in [DELTA, {"type": "response.completed", "response": {
                "status": "completed",
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            }}]
        ))

    monkeypatch.setattr(advisor, "read_codex_credentials", lambda: ("fixture", "fixture"))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        advisor.httpx, "AsyncClient",
        lambda **kwargs: client_type(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )

    text, usage = asyncio.run(advisor.call_codex_streaming_collect(
        "gpt-reserve", [{"role": "user", "content": "hello"}]
    ))

    assert sent["model"] == "gpt-5.6-luna"
    assert sent["stream"] is True
    assert text == "partial"
    assert usage.total_tokens == 3


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


def test_images_text_and_tool_history_keep_order_without_mutating_input():
    messages = [
        {"role": "system", "content": "Review the screenshot."},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Checking source."},
            {"type": "tool_use", "id": "read_1", "name": "read_file", "input": {"path": "view.tsx"}},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "Full screenshot"},
            {"type": "image_url", "image_url": {"url": IMAGE_URL, "detail": "high"}},
            {"type": "text", "text": "Before source result"},
            {"type": "tool_result", "tool_use_id": "read_1", "content": "source contents"},
            {"type": "input_image", "image_url": "https://example.com/crop.png", "detail": "original"},
            {"type": "text", "text": "Crop after source result"},
        ]},
    ]
    original = copy.deepcopy(messages)
    result = advisor.build_responses_input(messages)

    assert messages == original
    assert [item["type"] for item in result] == [
        "message", "message", "function_call", "message", "function_call_output", "message",
    ]
    assert result[0]["role"] == "developer"
    assert result[1]["content"] == [{"type": "output_text", "text": "Checking source."}]
    assert result[3] == {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "Full screenshot"},
        {"type": "input_image", "image_url": IMAGE_URL, "detail": "high"},
        {"type": "input_text", "text": "Before source result"},
    ]}
    assert result[4] == {"type": "function_call_output", "call_id": "read_1", "output": "source contents"}
    assert result[5]["content"] == [
        {"type": "input_image", "image_url": "https://example.com/crop.png", "detail": "original"},
        {"type": "input_text", "text": "Crop after source result"},
    ]


@pytest.mark.parametrize("detail", ["auto", "low", "high", "original"])
def test_image_only_user_messages_preserve_detail(detail):
    result = advisor.build_responses_input([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": IMAGE_URL, "detail": detail}},
    ]}])
    assert result == [{"type": "message", "role": "user", "content": [
        {"type": "input_image", "image_url": IMAGE_URL, "detail": detail},
    ]}]


def test_image_without_detail_defaults_to_auto():
    result = advisor.build_responses_input([{"role": "user", "content": [
        {"type": "input_image", "image_url": IMAGE_URL},
    ]}])
    assert result[0]["content"][0]["detail"] == "auto"


@pytest.mark.parametrize("role", ["assistant", "developer", "system", "tool"])
def test_images_in_unsupported_roles_fail_visibly(role):
    with pytest.raises(ValueError):
        advisor.build_responses_input([{"role": role, "content": [
            {"type": "image_url", "image_url": {"url": IMAGE_URL}},
        ]}])


@pytest.mark.parametrize("part", [
    {"type": "image_url", "image_url": IMAGE_URL},
    {"type": "image_url", "image_url": {"url": IMAGE_URL}, "detail": "high"},
    {"type": "image_url", "image_url": {"url": IMAGE_URL, "unknown": True}},
    {"type": "input_image", "image_url": {"url": IMAGE_URL}},
    {"type": "input_image", "file_id": "file_1"},
    {"type": "input_image", "image_url": IMAGE_URL, "file_id": "file_1"},
    *[{"type": "input_image", "image_url": value} for value in [
        None, 12, "", "screenshot.png", "file:///screenshot.png", "https://", "https://example.com/a b.png",
        "data:image/png;base64,", "data:image/png;base64,??", "data:image/svg+xml;base64,PHN2Zz4=",
    ]],
    *[{"type": "input_image", "image_url": IMAGE_URL, "detail": value} for value in [
        None, 12, [], "tiny",
    ]],
])
def test_malformed_images_are_rejected_without_loss(part):
    with pytest.raises(ValueError):
        advisor.build_responses_input([{"role": "user", "content": [
            {"type": "text", "text": "Please inspect this"}, part,
        ]}])


@pytest.mark.parametrize("through_litellm", [False, True])
@pytest.mark.parametrize("model_name", ["gpt-6-sol", "gpt-6.1-sol"])
def test_custom_advisor_passes_pixels_and_reasoning_to_existing_upstream(monkeypatch, through_litellm, model_name):
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text="".join(
            "data: " + json.dumps(event) + "\n\n" for event in [
                DELTA, {"type": "response.completed", "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 19, "output_tokens": 7, "total_tokens": 26},
                }},
            ]
        ))

    client_type = httpx.AsyncClient
    monkeypatch.setattr(advisor, "read_codex_credentials", lambda: ("fixture", "fixture"))
    monkeypatch.setattr(advisor.httpx, "AsyncClient", lambda **kw: client_type(
        transport=httpx.MockTransport(respond), **kw,
    ))
    messages = [{"role": "user", "content": [
            {"type": "text", "text": "Inspect these exact pixels."},
            {"type": "image_url", "image_url": {"url": IMAGE_URL, "detail": "high"}},
    ]}]
    if through_litellm:
        monkeypatch.setattr(litellm, "custom_provider_map", [{
            "provider": "codex-advisor", "custom_handler": advisor.codex_advisor_handler,
        }])
        response = asyncio.run(litellm.acompletion(
            model=f"codex-advisor/{model_name}", messages=messages,
            reasoning_effort="high", allowed_openai_params=["reasoning_effort"],
        ))
    else:
        response = asyncio.run(advisor.CodexAdvisorLLM().acompletion(
            model=f"codex-advisor/{model_name}", messages=messages,
            optional_params={"reasoning_effort": "high"},
        ))
    assert sent == [{
        "model": model_name, "stream": True, "store": False,
        "reasoning": {"effort": "high"},
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Inspect these exact pixels."},
            {"type": "input_image", "image_url": IMAGE_URL, "detail": "high"},
        ]}],
    }]
    assert response.choices[0].message.content == "partial"
    assert response.usage.total_tokens == 26


def test_dedicated_experts_exposes_only_versioned_sol61_without_fallback():
    config = yaml.safe_load((Path(__file__).parents[1] / "config/litellm.experts.yaml").read_text())
    models = config["model_list"]
    assert len(models) == 1
    assert models[0]["model_name"] == "codex-gpt-6.1-sol-advisor"
    assert models[0]["litellm_params"]["model"] == "codex-advisor/gpt-6.1-sol"
    assert "vision" in models[0]["model_info"]["capabilities"]
    assert config["router_settings"]["num_retries"] == 0
    assert config["router_settings"]["fallbacks"] == []


def test_custom_advisor_rejects_bad_image_before_upstream_call(monkeypatch):
    monkeypatch.setattr(advisor, "read_codex_credentials", lambda: ("fixture", "fixture"))

    def unexpected_client(**kwargs):
        pytest.fail("Malformed image must not reach the provider")

    monkeypatch.setattr(advisor.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(advisor.CustomLLMError) as error:
        asyncio.run(advisor.CodexAdvisorLLM().acompletion(
            model="gpt-6-sol", messages=[{"role": "user", "content": [
                {"type": "input_image", "image_url": "screenshot.png"},
            ]}],
        ))
    assert error.value.status_code == 400


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


@pytest.mark.parametrize(("status", "gateway_status"), [(401, 502), (408, 504), (429, 429), (500, 502), (503, 502)])
def test_http_failures_are_not_success_and_keep_gateway_classification(monkeypatch, status, gateway_status):
    with pytest.raises(advisor.CodexAdvisorError, match="API error") as error:
        collect(monkeypatch, [], status=status)
    assert error.value.status_code == gateway_status


@pytest.mark.parametrize("body", ["data: not-json\n\n", "data: [DONE]\n\n", ""])
def test_invalid_or_empty_stream_fails(monkeypatch, body):
    with pytest.raises(RuntimeError):
        collect(monkeypatch, [], raw=body)


@pytest.mark.parametrize("error", [httpx.ConnectError("fixture")])
def test_transport_failure_is_not_retried(monkeypatch, error):
    with pytest.raises(RuntimeError, match="HTTP error"):
        collect(monkeypatch, [], error=error)


def test_timeout_is_classified_as_gateway_timeout_without_retry(monkeypatch):
    with pytest.raises(TimeoutError, match="timed out"):
        collect(monkeypatch, [], error=httpx.ReadTimeout("fixture"))


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
