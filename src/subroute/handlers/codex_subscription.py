"""Use LiteLLM's protocol bridges with the Codex streaming-only Responses backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import litellm
from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.types.utils import GenericStreamingChunk, ModelResponse


def _target_model(model: str) -> str:
    return model.rsplit("/", 1)[-1]


def _request_options(optional_params: dict[str, Any]) -> dict[str, Any]:
    options = dict(optional_params)
    # The ChatGPT subscription endpoint rejects these public OpenAI/LiteLLM
    # fields. Filter them only on this provider-bound request; keep the
    # caller's original Messages/Responses payload intact.
    for key in (
        "stream", "api_base", "api_key", "extra_headers", "timeout", "num_retries",
        "max_tokens", "max_output_tokens", "user",
    ):
        options.pop(key, None)
    return options


async def _codex_stream(
    model: str,
    messages: list[dict[str, Any]],
    api_base: str,
    api_key: str | None,
    headers: dict[str, Any],
    timeout: Any,
    optional_params: dict[str, Any],
    *,
    require_usage: bool = False,
) -> Any:
    request_options = _request_options(optional_params)
    if request_options.get("store") is None:
        # Codex subscription rejects Responses requests unless storage is
        # explicitly disabled. LiteLLM's public Responses-to-Chat conversion
        # can omit the caller's store=false before it reaches this adapter.
        request_options["store"] = False
    if require_usage:
        # LiteLLM's Anthropic Messages bridge needs completion usage even when
        # the caller requested a buffered (non-streaming) response. This
        # adapter always streams upstream, so ask the provider adapter to keep
        # the terminal provider usage chunk for that conversion.
        stream_options = request_options.get("stream_options")
        request_options["stream_options"] = {
            **(stream_options if isinstance(stream_options, dict) else {}),
            "include_usage": True,
        }
    return await litellm.acompletion(
        model=f"openai/responses/{_target_model(model)}",
        messages=messages,
        api_base=api_base,
        api_key=api_key,
        extra_headers=dict(headers),
        timeout=timeout,
        num_retries=0,
        stream=True,
        **request_options,
    )


def _is_anthropic_messages_call(logging_obj: Any) -> bool:
    call_type = getattr(logging_obj, "call_type", None)
    call_type = getattr(call_type, "value", call_type)
    return call_type in {"anthropic_messages", "aanthropic_messages"}


async def _close_stream(stream: Any) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()


def _terminal_finish_reason(chunk: Any) -> str | None:
    choices = chunk.get("choices", []) if isinstance(chunk, dict) else getattr(chunk, "choices", [])
    for choice in choices or []:
        reason = choice.get("finish_reason") if isinstance(choice, dict) else getattr(choice, "finish_reason", None)
        if reason is not None:
            return str(reason)
    return None


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _plain_usage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    raise CustomLLMError(status_code=502, message="Codex stream returned unsupported usage data")


def _generic_chunks(chunk: Any) -> list[GenericStreamingChunk]:
    """Translate Chat chunks to LiteLLM CustomLLM's generic stream contract."""
    usage = _plain_usage(_field(chunk, "usage"))
    choices = _field(chunk, "choices", []) or []
    output: list[GenericStreamingChunk] = []
    for choice in choices:
        delta = _field(choice, "delta")
        content = _field(delta, "content", "") or ""
        if not isinstance(content, str):
            raise CustomLLMError(
                status_code=502,
                message="Codex stream returned non-text Chat content unsupported by LiteLLM's generic stream contract",
            )
        tool_calls = _field(delta, "tool_calls", []) or []
        finish_reason = _field(choice, "finish_reason")
        choice_index = _field(choice, "index", 0)
        pieces: list[tuple[str, Any]] = []
        if content:
            pieces.append((content, None))
        for tool_call in tool_calls:
            pieces.append(("", tool_call))
        if not pieces and finish_reason is not None:
            pieces.append(("", None))
        for piece_index, (text, tool_use) in enumerate(pieces):
            is_last_piece = piece_index == len(pieces) - 1
            generic: GenericStreamingChunk = {
                "text": text,
                "is_finished": bool(finish_reason is not None and is_last_piece),
                "finish_reason": str(finish_reason or ""),
                "usage": usage if is_last_piece else None,
                "index": choice_index,
            }
            if tool_use is not None:
                generic["tool_use"] = tool_use
            output.append(generic)

    if not output and usage is not None:
        output.append({
            "text": "",
            "is_finished": False,
            "finish_reason": "",
            "usage": usage,
        })
    return output


class CodexSubscriptionLLM(CustomLLM):
    """Force only the upstream Codex call to stream; LiteLLM owns public protocols."""

    async def acompletion(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers=None,
        timeout=None,
        client=None,
    ) -> ModelResponse:
        require_usage = _is_anthropic_messages_call(logging_obj)
        stream = await _codex_stream(
            model, messages, api_base, api_key, headers or {}, timeout, optional_params,
            require_usage=require_usage,
        )
        chunks = []
        terminal_reason = None
        provider_usage_seen = False
        try:
            async for chunk in stream:
                chunks.append(chunk)
                terminal_reason = _terminal_finish_reason(chunk) or terminal_reason
                provider_usage_seen = provider_usage_seen or _field(chunk, "usage") is not None
        finally:
            await _close_stream(stream)

        if terminal_reason is None:
            raise CustomLLMError(
                status_code=502,
                message="Codex stream ended without a terminal finish reason",
            )
        response = litellm.stream_chunk_builder(chunks, messages=messages)
        if not isinstance(response, ModelResponse):
            raise CustomLLMError(
                status_code=502,
                message="LiteLLM could not assemble a completed Codex response",
            )
        if not response.choices or response.choices[0].finish_reason is None:
            raise CustomLLMError(
                status_code=502,
                message="LiteLLM assembled a Codex response without a terminal choice",
            )
        if not provider_usage_seen:
            response.usage = None
            if require_usage:
                raise CustomLLMError(
                    status_code=502,
                    message="Codex provider omitted usage required by the Anthropic Messages bridge",
                )
        response.model = _target_model(model)
        return response

    async def astreaming(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers=None,
        timeout=None,
        client=None,
    ) -> AsyncIterator[GenericStreamingChunk]:
        require_usage = _is_anthropic_messages_call(logging_obj)
        stream = await _codex_stream(
            model, messages, api_base, api_key, headers or {}, timeout, optional_params,
            require_usage=require_usage,
        )
        terminal_reason = None
        provider_usage_seen = False
        try:
            async for chunk in stream:
                terminal_reason = _terminal_finish_reason(chunk) or terminal_reason
                provider_usage_seen = provider_usage_seen or _field(chunk, "usage") is not None
                for generic_chunk in _generic_chunks(chunk):
                    yield generic_chunk
            if terminal_reason is None:
                raise CustomLLMError(
                    status_code=502,
                    message="Codex stream ended without a terminal finish reason",
                )
            if require_usage and not provider_usage_seen:
                raise CustomLLMError(
                    status_code=502,
                    message="Codex provider omitted usage required by the Anthropic Messages bridge",
                )
        finally:
            await _close_stream(stream)


codex_subscription_handler = CodexSubscriptionLLM()
