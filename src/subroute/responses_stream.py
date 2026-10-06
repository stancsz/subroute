"""Version-bounded repair of LiteLLM's Chat-to-Responses item lifecycle.

1.101.0/1.103.0 reserve index zero for both thought and answer, omit the
answer opener after thinking, and put reasoning text into message parts and
final reasoning.content. Codex consumes those as visible answer text. Keep
LiteLLM's transport, event classes, response envelope and usage accounting;
repair only its bridge's identities, part types and completed item ordering.
Retire when the client stream contract regressions pass on an upstream release.
"""

from copy import deepcopy
from importlib.metadata import version
import uuid

from litellm.types.utils import ModelResponse
from litellm.types.llms.openai import BaseLiteLLMOpenAIResponseObject as Event


def _plain(value):
    return value if isinstance(value, dict) else value.model_dump(exclude_none=True)


async def _thinking_source(source):
    """Keep opaque blocks for replay and expose their displayable summary."""
    from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import _CombinedChunkSplitter
    try:
        async for chunk in source:
            choices = getattr(chunk, "choices", None) or []
            if len(choices) == 1:
                blocks = getattr(choices[0].delta, "thinking_blocks", None) or []
                if blocks and not getattr(choices[0].delta, "reasoning_content", None):
                    chunk = chunk.model_copy(deep=True)
                    chunk.choices[0].delta.reasoning_content = "".join(b.get("thinking", "") for b in blocks)
            for combined in _CombinedChunkSplitter._split(chunk):
                for piece in _CombinedChunkSplitter._split_by_payload_kind(combined):
                    yield piece
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()


async def _repair_events(source):
    indices = {}
    opened = {}
    done = {}
    parts = set()
    ignored = set()
    sequence = 0
    try:
        async for original in source:
            event = _plain(original)
            kind = event.get("type", "")
            queued = []
            item = event.get("item") or {}
            identity = event.get("item_id") or item.get("id")
            if kind == "response.output_text.done" and identity not in indices and not event.get("text"):
                # The bridge emits a phantom empty message after tool-only or
                # reasoning-only output. It has no observed content or opener.
                ignored.add(identity)
            if identity in ignored:
                continue
            if kind == "response.output_item.added":
                if item.get("type") == "reasoning":
                    # Codex rejects this item without the required summary
                    # array, leaving subsequent summary deltas without an item.
                    item.setdefault("summary", [])
                indices.setdefault(identity, len(indices))
                opened[identity] = item
            elif identity and identity not in indices:
                # This exact iterator omits the message opener after thought.
                # Use its already assigned ID, never manufacture tool identity.
                if kind.startswith("response.output_text."):
                    item = {"id": identity, "type": "message", "role": "assistant", "status": "in_progress", "content": []}
                    indices[identity] = len(indices)
                    opened[identity] = item
                    queued.append({"type": "response.output_item.added", "output_index": indices[identity], "item": item})
            if identity in indices:
                event["output_index"] = indices[identity]
            if kind in {"response.content_part.added", "response.reasoning_summary_part.added"}:
                field = "summary_index" if "reasoning" in kind else "content_index"
                parts.add((identity, field, event.get(field, 0)))
            elif kind.startswith(("response.output_text.", "response.reasoning_summary_text.")):
                reasoning = "reasoning_summary" in kind
                field = "summary_index" if reasoning else "content_index"
                index = event.setdefault(field, 0)
                key = (identity, field, index)
                if key not in parts and identity in indices:
                    parts.add(key)
                    queued.append({"type": "response.reasoning_summary_part.added" if reasoning else "response.content_part.added",
                                   "item_id": identity, "output_index": indices[identity], field: index,
                                   "part": {"type": "summary_text" if reasoning else "output_text", "text": "", **({} if reasoning else {"annotations": []})}})
            if kind == "response.output_item.done":
                done[identity] = item
            elif kind == "response.completed":
                snapshots = {i["id"]: i for i in map(_plain, event["response"].get("output", [])) if i["id"] not in ignored}
                output = []
                for identity in indices:
                    snapshot = {**snapshots.pop(identity, {}), **done.get(identity, opened[identity])}
                    if snapshot.get("type") == "reasoning":
                        snapshot.pop("content", None)
                        snapshot.pop("role", None)
                        snapshot.setdefault("summary", [])
                    output.append(snapshot)
                # Preserve other supported hosted output types owned by LiteLLM.
                output.extend(snapshots.values())
                event["response"]["output"] = output
            for data in queued + [event]:
                sequence += 1
                data["sequence_number"] = sequence
                if data is event and hasattr(original, "model_copy"):
                    updates = {field: data[field] for field in ("sequence_number", "output_index", "summary_index") if field in data}
                    if kind == "response.output_item.added" and item.get("type") == "reasoning":
                        original_item = getattr(original, "item", None)
                        updates["item"] = (original_item.model_copy(update={"summary": item["summary"]})
                                           if hasattr(original_item, "model_copy") else item)
                    if kind == "response.completed":
                        updates["response"] = original.response.model_copy(update={"output": data["response"]["output"]})
                    yield original.model_copy(update=updates)
                else:
                    yield Event(**data)
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()


def install_responses_stream_repair():
    if version("litellm") not in {"1.101.0", "1.103.0"}:
        return
    from litellm.responses.litellm_completion_transformation.streaming_iterator import LiteLLMCompletionStreamingIterator as Iterator
    if getattr(Iterator, "_subroute_item_repair", False):
        return
    Iterator._subroute_item_repair = True
    original_init = Iterator.__init__
    original_next = Iterator.__anext__
    original_ensure = Iterator._ensure_output_item_for_chunk
    original_part_done = Iterator.create_output_content_part_done_event
    original_text_done = Iterator.create_output_text_done_event
    original_item_done = Iterator.create_output_item_done_event

    class ThinkingSource:
        def __init__(self, source):
            self.source = source
            self.iterator = _thinking_source(source)

        def __getattr__(self, name):
            return getattr(self.source, name)

        def __aiter__(self):
            return self

        def __iter__(self):
            return iter(self.source)

        async def __anext__(self):
            return await anext(self.iterator)

        async def aclose(self):
            await self.iterator.aclose()

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.litellm_custom_stream_wrapper = ThinkingSource(self.litellm_custom_stream_wrapper)
        self._subroute_kind = None
        self._subroute_text = []

    def text_response(self, response=None):
        result = deepcopy(response) if response is not None else ModelResponse()
        result.choices[0].message.content = "".join(self._subroute_text)
        result.choices[0].message.reasoning_content = None
        return result

    def item_done(self, response):
        previous = self.litellm_model_response
        try:
            self.litellm_model_response = text_response(self, response)
            return original_item_done(self, self.litellm_model_response)
        finally:
            self.litellm_model_response = previous

    def text_done(self, response):
        return original_text_done(self, text_response(self, response))

    def ensure(self, chunk):
        delta = chunk.choices[0].delta
        kind = ("reasoning" if getattr(delta, "reasoning_content", None) or getattr(delta, "thinking_blocks", None)
                else "message" if getattr(delta, "content", None)
                else "tool" if getattr(delta, "tool_calls", None) else None)
        if kind is not None and kind != self._subroute_kind:
            if self._subroute_kind == "reasoning" and self._reasoning_active:
                # Use LiteLLM's own done factories before opening the next item.
                identity = self._cached_reasoning_item_id
                text = "".join(self._accumulated_reasoning_content_parts)
                for factory in (self.create_reasoning_summary_text_done_event,
                                self.create_reasoning_summary_part_done_event,
                                self.create_reasoning_output_item_done_event):
                    self._sequence_number += 1
                    self._pending_response_events.append(factory(identity, text, self._sequence_number))
                self._reasoning_active = False
                self._reasoning_done_emitted = True
            elif self._subroute_kind == "message":
                response = text_response(self)
                self._pending_response_events.extend([text_done(self, response), part_done(self, response), item_done(self, response)])
                self._cached_item_id = None
                self._subroute_text = []
            self.sent_output_item_added_event = False
            if kind == "reasoning":
                self._cached_reasoning_item_id = None
                self._reasoning_item_id = None
                self._reasoning_done_emitted = False
                self._accumulated_reasoning_content_parts = []
            elif kind == "message":
                self.sent_content_part_added_event = False
                self._subroute_text = []
            self._subroute_kind = kind
        if kind == "message":
            self._subroute_text.append(delta.content)
        if getattr(delta, "thinking_blocks", None) and not getattr(delta, "reasoning_content", None) and not self.sent_output_item_added_event:
            # Redacted-only reasoning has no displayable text, but its replay
            # item must exist before the subsequent message changes indices.
            self.sent_output_item_added_event = True
            self._reasoning_active = True
            self._cached_reasoning_item_id = self._reasoning_item_id = f"rs_{uuid.uuid4()}"
            self._pending_response_events.append(Event(type="response.output_item.added", output_index=0,
                item={"id": self._cached_reasoning_item_id, "type": "reasoning", "status": "in_progress", "summary": []}))
            return
        original_ensure(self, chunk)

    def part_done(self, response):
        # The message part factory erroneously prefers reasoning over text.
        return original_part_done(self, text_response(self, response))

    async def source(self):
        try:
            while True:
                try:
                    yield await original_next(self)
                except StopAsyncIteration:
                    return
        finally:
            await self.litellm_custom_stream_wrapper.aclose()

    async def next_event(self):
        if not hasattr(self, "_subroute_events"):
            self._subroute_events = _repair_events(source(self))
        event = await anext(self._subroute_events)
        if event.type == "response.completed":
            self.completed_response = event
        return event

    Iterator.__init__ = init
    Iterator._ensure_output_item_for_chunk = ensure
    Iterator.create_output_content_part_done_event = part_done
    Iterator.create_output_text_done_event = text_done
    Iterator.create_output_item_done_event = item_done
    Iterator.__anext__ = next_event
