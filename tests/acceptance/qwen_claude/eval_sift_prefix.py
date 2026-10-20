"""Measure prompt prefix stability between tool-result turns in private traces."""

from __future__ import annotations

import json
from pathlib import Path
import statistics

from eval_sift_trace import ROOT, TRACES, estimate, replay
from sift import CompactionError, compact_messages


def common_prefix_length(left: str, right: str) -> int:
    return next((index for index, (a, b) in enumerate(zip(left, right)) if a != b),
                min(len(left), len(right)))


def measure(path: Path, task: str, *, target: int = 5_000) -> dict:
    history = replay(path, task)
    previous = None
    similarities = []
    forwarded = []
    rejected = 0
    for end in range(2, len(history) + 1):
        content = history[end - 1].get("content")
        if not isinstance(content, list) or not any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        ):
            continue
        try:
            result = compact_messages(history[:end], estimate, target=target, protocol="anthropic")
        except CompactionError:
            rejected += 1
            previous = None
            continue
        if not result.audit["compacted"]:
            previous = None
            continue
        serialized = json.dumps(result.messages, ensure_ascii=False)
        forwarded.append(result.audit["forwarded_tokens"])
        if previous is not None:
            similarities.append(common_prefix_length(previous, serialized) / min(len(previous), len(serialized)))
        previous = serialized
    return {
        "task": task, "compacted_requests": len(forwarded), "rejected_prefixes": rejected,
        "median_serialized_prefix_reuse": round(statistics.median(similarities), 3) if similarities else None,
        "mean_forwarded_tokens": round(statistics.mean(forwarded)) if forwarded else None,
    }


if __name__ == "__main__":
    rows = [measure(ROOT / "tests/acceptance/qwen_claude/.runs" / folder / task / "claude.jsonl", task)
            for task, (folder, _, _) in TRACES.items()]
    print(json.dumps(rows, indent=2))
