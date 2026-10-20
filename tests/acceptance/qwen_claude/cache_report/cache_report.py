"""Summarize JSONL inference events for a local model dashboard."""

from collections.abc import Iterable


def summarize_events(lines: Iterable[str]) -> dict[str, dict[str, int | float]]:
    """Return per-model request and cache statistics from JSONL lines.

    Valid records have a nonempty string model, integer prompt_tokens and
    cached_tokens, and finite nonnegative latency_ms. Token counts must be
    nonnegative and cached_tokens cannot exceed prompt_tokens. Skip malformed
    lines. Return an empty dict for no valid records.

    Each result contains requests, cache_hits (records with cached_tokens > 0),
    prompt_tokens, cached_tokens, and mean_latency_ms rounded to one decimal.
    """
    return {}
