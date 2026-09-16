import pytest
from litellm.types.utils import ModelResponse

from unified_llm_gateway import (
    AnthropicMessagesProtocol,
    OpenAIChatProtocol,
    OpenAIResponsesProtocol,
)


def response(finish_reason="stop"):
    return ModelResponse(
        id="chatcmpl_test",
        created=123,
        model="test-model",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": finish_reason,
            }
        ],
        usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    )


def test_openai_chat_round_trip_boundary():
    request = OpenAIChatProtocol.to_request(
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "max_completion_tokens": 12,
        }
    )
    assert request.max_tokens == 12
    output = OpenAIChatProtocol.from_response(response())
    assert output["object"] == "chat.completion"
    assert output["choices"][0]["message"]["content"] == "hello"


def test_openai_responses_text_boundary():
    request = OpenAIResponsesProtocol.to_request(
        {"model": "test-model", "input": "hi", "max_output_tokens": 12}
    )
    assert request.messages == [{"role": "user", "content": "hi"}]
    output = OpenAIResponsesProtocol.from_response(response())
    assert output["object"] == "response"
    assert output["output"][0]["content"][0]["text"] == "hello"
    assert output["usage"] == {
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
    }


def test_openai_responses_rejects_stateful_items():
    with pytest.raises(ValueError, match="Responses input"):
        OpenAIResponsesProtocol.to_request(
            {"model": "test-model", "input": [{"type": "item_reference", "id": "x"}]}
        )


def test_openai_chat_rejects_tools_and_multimodal_messages():
    with pytest.raises(ValueError, match="Unsupported OpenAI Chat fields"):
        OpenAIChatProtocol.to_request(
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [],
            }
        )
    with pytest.raises(ValueError, match="plain text"):
        OpenAIChatProtocol.to_request(
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            }
        )


def test_anthropic_messages_text_boundary():
    request = AnthropicMessagesProtocol.to_request(
        {
            "model": "test-model",
            "system": "be concise",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 12,
        }
    )
    assert request.messages[0] == {"role": "system", "content": "be concise"}
    output = AnthropicMessagesProtocol.from_response(response("length"))
    assert output["type"] == "message"
    assert output["content"] == [{"type": "text", "text": "hello"}]
    assert output["stop_reason"] == "max_tokens"


def test_anthropic_messages_rejects_non_message_items():
    with pytest.raises(ValueError, match="Anthropic messages"):
        AnthropicMessagesProtocol.to_request(
            {
                "model": "test-model",
                "messages": [{"role": "tool", "content": "x"}],
                "max_tokens": 12,
            }
        )


def test_protocol_outputs_reject_multiple_choices():
    value = response()
    value.choices.append(value.choices[0])
    for protocol in (OpenAIChatProtocol, OpenAIResponsesProtocol, AnthropicMessagesProtocol):
        with pytest.raises(ValueError, match="one choice"):
            protocol.from_response(value)
