"""Version-bounded repairs for LiteLLM's Chat-to-Messages conversion.

LiteLLM 1.101.0 and 1.103.0 copy a thinking_blocks delta into both the
content_block_start and the first thinking_delta. Its mixed-payload splitter
fixes some shapes, but a reasoning-only first chunk still duplicates text.
Signed/multiple thoughts and redacted blocks also lose boundaries in its
normalizer, and buffered assembly drops unsigned thoughts and signature tails.
Retire these patches when the client stream/replay contracts pass upstream.
LiteLLM continues to own provider adaptation and protocol streaming.
"""

from copy import deepcopy
import json
from importlib.metadata import version

from litellm.llms.custom_llm import CustomLLMError
from litellm.types.utils import Delta


def _merge_opaque(target, fields):
    """Preserve provider replay metadata; conflicting opaque values are unsafe."""
    for key, value in fields.items():
        if key not in target:
            target[key] = deepcopy(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _merge_opaque(target[key], value)
        elif target[key] != value:
            raise CustomLLMError(502, "Provider changed in-flight tool metadata")


def _thinking_deltas(chunk):
    """Let the Messages adapter see each thought and signature separately."""
    choices = getattr(chunk, "choices", None) or []
    if len(choices) != 1 or not getattr(choices[0].delta, "thinking_blocks", None):
        return [chunk]
    blocks = choices[0].delta.thinking_blocks
    pieces = []
    for index, block in enumerate(blocks):
        if block.get("type") not in {"thinking", "redacted_thinking"}:
            raise CustomLLMError(502, "Provider returned an unsupported reasoning block")
        parts = [deepcopy(block)]
        if block.get("thinking") and block.get("signature"):
            parts = [{**block, "signature": ""}, {"type": "thinking", "thinking": "", "signature": block["signature"]}]
        for part_index, part in enumerate(parts):
            piece = chunk.model_copy(deep=True)
            piece.choices[0].delta = Delta(thinking_blocks=[part])
            if part_index == 0 and (index > 0 or block.get("type") == "redacted_thinking" or (block.get("thinking") and block.get("signature"))):
                piece._hidden_params["_subroute_thinking_boundary"] = True
            pieces.append(piece)
    return pieces


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
                    _merge_opaque(call, {key: value for key, value in fields.items() if key not in {"index", "id", "type", "function"}})
                    for field in ("id", "type"):
                        if fields.get(field):
                            if call.get(field) and call[field] != fields[field]:
                                raise CustomLLMError(502, "Provider changed an in-flight tool identity")
                            call[field] = fields[field]
                    function = fields.get("function") or {}
                    _merge_opaque(call["function"], {key: value for key, value in function.items() if key not in {"name", "arguments"}})
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
    from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import LiteLLMAnthropicMessagesAdapter

    base = streaming_iterator._CombinedChunkSplitter
    if getattr(base, "_subroute_tool_ordering", False):
        return

    # The stock adapter seeds a populated opener and repeats the signature,
    # prefers signatures over text, and ignores redacted blocks. Split signed
    # snapshots first, then give LiteLLM empty openers for its own deltas.
    original_start = LiteLLMAnthropicMessagesAdapter._translate_streaming_openai_chunk_to_anthropic_content_block
    original_delta = LiteLLMAnthropicMessagesAdapter._translate_streaming_openai_chunk_to_anthropic

    def block_start(self, choices):
        blocks = getattr(choices[0].delta, "thinking_blocks", None) or []
        if blocks and blocks[0].get("type") == "redacted_thinking":
            return "redacted_thinking", deepcopy(blocks[0])
        kind, block = original_start(self, choices)
        if kind == "thinking":
            block = {**block, "thinking": "", "signature": ""}
        return kind, block

    def block_delta(self, choices):
        blocks = getattr(choices[0].delta, "thinking_blocks", None) or []
        if blocks and blocks[0].get("type") == "redacted_thinking":
            return "text_delta", {"type": "text_delta", "text": ""}
        return original_delta(self, choices)

    LiteLLMAnthropicMessagesAdapter._translate_streaming_openai_chunk_to_anthropic_content_block = block_start
    LiteLLMAnthropicMessagesAdapter._translate_streaming_openai_chunk_to_anthropic = block_delta

    wrapper = streaming_iterator.AnthropicStreamWrapper
    original_transition = wrapper._should_start_new_content_block

    def transition(self, chunk):
        changed = original_transition(self, chunk)
        if chunk._hidden_params.get("_subroute_thinking_boundary"):
            self.current_content_block_type, self.current_content_block_start = block_start(LiteLLMAnthropicMessagesAdapter(), chunk.choices)
            return True
        return changed

    wrapper._should_start_new_content_block = transition

    from litellm.litellm_core_utils.streaming_chunk_builder_utils import ChunkProcessor

    def combined_thinking(self, chunks):
        # Upstream drops unsigned thoughts and trailing signature fragments
        # when building a buffered response. Preserve the actual block order.
        result = []
        pending = None

        def flush():
            nonlocal pending
            if pending and (pending["thinking"] or pending["signature"]):
                result.append(pending)
            pending = None

        for chunk in chunks:
            for choice in chunk["choices"]:
                blocks = choice.get("delta", {}).get("thinking_blocks") or []
                for index, block in enumerate(blocks):
                    if block.get("type") == "redacted_thinking":
                        flush()
                        result.append(deepcopy(block))
                        continue
                    if index > 0 or (pending and pending["signature"] and block.get("thinking")):
                        flush()
                    if pending is None:
                        pending = {"type": "thinking", "thinking": "", "signature": ""}
                    pending["thinking"] += block.get("thinking") or ""
                    pending["signature"] += block.get("signature") or ""
        flush()
        return result or None

    ChunkProcessor.get_combined_thinking_content = combined_thinking

    class OrderedToolSplitter(base):
        _subroute_tool_ordering = True

        @staticmethod
        def _normalize_reasoning_fields(fields):
            # Collapsing unsigned lists can also drop redacted entries.
            return fields

        def __init__(self, stream):
            super().__init__(stream)
            self._ordered = _ordered_tool_deltas(self._source())

        async def _source(self):
            try:
                while True:
                    try:
                        chunk = await super().__anext__()
                        for piece in _thinking_deltas(chunk):
                            yield piece
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
