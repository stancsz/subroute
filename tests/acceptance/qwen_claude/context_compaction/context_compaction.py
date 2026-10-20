"""Plan a bounded, loss-aware request for the local 32K Qwen endpoint.

This standalone fixture models the gateway's compaction boundary. Implement
compact_messages without changing the public signature or mutating input.
"""

from __future__ import annotations

from copy import deepcopy
import json


class ContextError(ValueError):
    """The request cannot be represented safely in the local context."""


def estimate_tokens(messages: list[dict]) -> int:
    """Deterministic surrogate for the gateway tokenizer in this fixture."""
    return sum((len(json.dumps(message, ensure_ascii=False)) + 3) // 4 for message in messages)


def compact_messages(
    messages: list[dict], *, target: int = 24_576, raw_limit: int = 1_000_000
) -> tuple[list[dict], dict]:
    """Fit older text dialogue around the active user turn and pending tool work.

    Return (forwarded_messages, audit). Audit has input_tokens,
    forwarded_tokens, omitted_tokens, and compacted. Older turns may be
    replaced by one clearly marked, untrusted user-role historical excerpt.
    Preserve all leading system/developer instructions and the latest real
    user turn with every message after it. A user tool-result message is not a
    real user turn. Reject unsupported media and late instructions when
    compaction is needed. Reject an irreducible active turn; never trim it.
    """
    raise NotImplementedError
