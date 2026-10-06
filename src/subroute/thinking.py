"""Narrow repair for LiteLLM's duplicated unsigned Messages block opener.

LiteLLM 1.101.0 and 1.103.0 copy a thinking_blocks delta into both the
content_block_start and the first thinking_delta. Its mixed-payload splitter
fixes some shapes, but a reasoning-only first chunk still duplicates text.
Retire this repair once upstream opens an empty block for every such shape.
LiteLLM continues to own provider adaptation and protocol streaming.
"""

from copy import deepcopy
import json
from importlib.metadata import version

from litellm.llms.custom_llm import CustomLLMError
from litellm.types.utils import Delta


def _event(frame):
    if isinstance(frame, dict):
        return frame
    if not isinstance(frame, (str, bytes)):
        return None
    lines = frame.splitlines()
    prefix = b"data:" if isinstance(frame, bytes) else "data:"
    data = [line[len(prefix):].strip() for line in lines if line.startswith(prefix)]
    if len(data) != 1:
        return None
    try:
        event = json.loads(data[0])
        return event if isinstance(event, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def _empty_start(frame, event):
    event = deepcopy(event)
    event["content_block"]["thinking"] = ""
    if isinstance(frame, dict):
        return event
    text = f"event: content_block_start\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    return text.encode("utf-8") if isinstance(frame, bytes) else text


async def repair_thinking_starts(response):
    # Hold at most one populated unsigned opener, for one following event.
    # Native empty starts, signed snapshots, errors, partial frames, other
    # protocols and non-matching deltas all pass through without alteration.
    pending = None
    try:
        async for frame in response:
            event = _event(frame)
            if pending is not None:
                start_frame, start = pending
                delta = (event or {}).get("delta") or {}
                if not isinstance(delta, dict):
                    delta = {}
                duplicate = (
                    (event or {}).get("type") == "content_block_delta"
                    and event.get("index") == start.get("index")
                    and delta.get("type") == "thinking_delta"
                    and delta.get("thinking") == start["content_block"]["thinking"]
                )
                yield _empty_start(start_frame, start) if duplicate else start_frame
                pending = None
            block = (event or {}).get("content_block") or {}
            if not isinstance(block, dict):
                block = {}
            if ((event or {}).get("type") == "content_block_start"
                    and block.get("type") == "thinking"
                    and isinstance(block.get("thinking"), str) and block["thinking"]
                    and not block.get("signature")):
                pending = (frame, event)
            else:
                yield frame
        if pending is not None:
            yield pending[0]
    finally:
        close = getattr(response, "aclose", None)
        if close is not None:
            await close()


async def _ordered_tool_deltas(source):
    """Complete interleaved Chat tool deltas before LiteLLM opens their blocks.

    Keep the suffix's original order using one placeholder per tool index.
    Text/thinking before the first tool stays live; the tool suffix is held
    until the provider finishes, since Chat gives no per-tool terminal event.
    No public protocol or provider transport is implemented here.
    """
    calls = {}
    suffix = []
    try:
        async for chunk in source:
            choices = getattr(chunk, "choices", None) or []
            if len(choices) != 1:
                if calls:
                    suffix.append(chunk)
                else:
                    yield chunk
                continue
            choice = choices[0]
            tools = getattr(choice.delta, "tool_calls", None) or []
            if tools:
                # A continuation can carry reasoning/text as well as tool
                # arguments. LiteLLM's splitter deliberately leaves it mixed.
                # Keep those payloads in order instead of discarding them.
                fields = choice.delta.model_dump(exclude_none=True)
                fields.pop("tool_calls", None)
                if any(fields.get(field) for field in ("content", "reasoning_content", "thinking_blocks", "refusal")):
                    payload = chunk.model_copy(deep=True)
                    payload.choices[0].delta = Delta(**fields)
                    payload.choices[0].finish_reason = None
                    if hasattr(payload, "usage"):
                        payload.usage = None
                    from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import _CombinedChunkSplitter
                    for piece in _CombinedChunkSplitter._split_by_payload_kind(payload):
                        if calls:
                            suffix.append(piece)
                        else:
                            yield piece
                for tool in tools:
                    fields = tool.model_dump(exclude_none=True) if hasattr(tool, "model_dump") else tool
                    index = fields.get("index", 0)
                    if index not in calls:
                        calls[index] = {"index": index, "function": {"arguments": ""}}
                        placeholder = chunk.model_copy(deep=True)
                        placeholder.choices[0].delta = Delta(tool_calls=[calls[index]])
                        placeholder.choices[0].finish_reason = None
                        if hasattr(placeholder, "usage"):
                            placeholder.usage = None
                        suffix.append((index, placeholder))
                    call = calls[index]
                    for field in ("id", "type"):
                        if fields.get(field):
                            if call.get(field) and call[field] != fields[field]:
                                raise CustomLLMError(502, "Provider changed an in-flight tool identity")
                            call[field] = fields[field]
                    function = fields.get("function") or {}
                    if function.get("name"):
                        call["function"]["name"] = call["function"].get("name", "") + function["name"]
                    arguments = function.get("arguments") or ""
                    if not isinstance(arguments, str):
                        raise CustomLLMError(502, "Provider returned non-text tool argument deltas")
                    call["function"]["arguments"] += arguments
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.model_copy(deep=True)
                    usage.choices[0].delta = Delta()
                    usage.choices[0].finish_reason = None
                    suffix.append(usage)
                continue
            if choice.finish_reason is not None and calls:
                # Verify every pending tool before exposing any completed call.
                for call in calls.values():
                    if not call.get("id") or not call["function"].get("name"):
                        raise CustomLLMError(502, "Provider returned an incomplete tool identity")
                    try:
                        json.loads(call["function"]["arguments"])
                    except (ValueError, TypeError) as exc:
                        raise CustomLLMError(502, "Provider returned incomplete tool arguments") from exc
                for item in suffix:
                    if isinstance(item, tuple):
                        index, placeholder = item
                        call = calls[index]
                        placeholder.choices[0].delta = Delta(tool_calls=[call])
                        yield placeholder
                    else:
                        yield item
                calls.clear()
                suffix.clear()
                yield chunk
            elif calls:
                suffix.append(chunk)
            else:
                yield chunk
        if calls:
            raise CustomLLMError(502, "Provider stream ended before completing its tool calls")
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()


def install_messages_tool_ordering():
    """Patch the verified LiteLLM gap at its Chat chunk splitter, once.

    1.101.0 / 1.103.0 close tool 0 as soon as tool 1 opens, then put tool 0's
    remaining argument deltas into tool 1. There is no per-chunk callback
    before this conversion (the deployment hook runs only on the final chunk).
    Remove this version-bounded shim when the upstream interleaving regression
    passes. Sync SDK calls keep LiteLLM's implementation; Subroute uses async.
    """
    if version("litellm") not in {"1.101.0", "1.103.0"}:
        return
    from litellm.llms.anthropic.experimental_pass_through.adapters import streaming_iterator

    base = streaming_iterator._CombinedChunkSplitter
    if getattr(base, "_subroute_tool_ordering", False):
        return

    class OrderedToolSplitter(base):
        _subroute_tool_ordering = True

        def __init__(self, stream):
            super().__init__(stream)
            self._ordered = _ordered_tool_deltas(self._source())

        async def _source(self):
            try:
                while True:
                    try:
                        yield await super().__anext__()
                    except StopAsyncIteration:
                        return
            finally:
                close = getattr(self._stream, "aclose", None)
                if close is not None:
                    await close()

        async def __anext__(self):
            return await anext(self._ordered)

        async def aclose(self):
            await self._ordered.aclose()

    streaming_iterator._CombinedChunkSplitter = OrderedToolSplitter
