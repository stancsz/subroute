"""Replay local Claude Code traces through Sift without publishing raw logs."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from sift import CompactionError, compact_messages  # noqa: E402
from run import TASKS  # noqa: E402


TRACES = {
    "sse_relay": (
        "subroute-qwen-claude-bj83aywf", "test_missing_terminal_is_failure",
        "ERROR: test_missing_terminal_is_failure",
    ),
    "billing_ledger": (
        "subroute-qwen-claude-nt07p4t5", "test_conflicting_replay_and_excess_refund_fail",
        "ERROR: test_conflicting_replay_and_excess_refund_fail",
    ),
    "appointment_slots": (
        "subroute-qwen-claude-me15p39j", "test_rejects_nonexistent_or_ambiguous_wall_time",
        "ERROR: test_rejects_nonexistent_or_ambiguous_wall_time",
    ),
    "context_compaction": (
        "subroute-qwen-claude-xkb4u01k", "test_keeps_active_user_when_last_user_message_is_tool_result",
        "ERROR: test_keeps_active_user_when_last_user_message_is_tool_result",
    ),
    "policy_snapshot": (
        "subroute-qwen-claude-2cc0r8gl", "test_concurrent_resolves_never_mix_revisions",
        "ERROR: test_concurrent_resolves_never_mix_revisions",
    ),
    "build_planner": (
        "subroute-qwen-claude-ct5m1ux3", "test_cycles_and_missing_digests",
        "ERROR: test_cycles_and_missing_digests",
    ),
}


def estimate(messages: list[dict]) -> int:
    return (len(json.dumps(messages, ensure_ascii=False)) + 3) // 4


def replay(path: Path, task: str) -> list[dict]:
    messages = [{"role": "user", "content": TASKS[task][1]}]
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") not in {"assistant", "user"}:
            continue
        blocks = event.get("message", {}).get("content", [])
        if not isinstance(blocks, list) or not blocks:
            continue
        if event["type"] == "user" and any(
            isinstance(block, dict) and block.get("type") == "text" for block in blocks
        ):
            break  # Claude's own context snapshot begins a new epoch.
        messages.append({"role": event["type"], "content": blocks})
    return messages


def tool_turn_retention(history: list[dict], expected: str, target: int = 5_000) -> dict:
    compacted = 0
    retained = 0
    for end in range(2, len(history) + 1):
        content = history[end - 1].get("content")
        if not isinstance(content, list) or not any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        ):
            continue
        try:
            result = compact_messages(history[:end], estimate, target=target,
                                      protocol="anthropic")
        except CompactionError:
            continue
        if result.audit["compacted"]:
            compacted += 1
            retained += expected in str(result.messages)
    return {"compacted_tool_turns": compacted,
            "baseline_failure_retained_turns": retained}


def main() -> int:
    output = []
    for task, (folder, focus, expected) in TRACES.items():
        path = ROOT / "tests/acceptance/qwen_claude/.runs" / folder / task / "claude.jsonl"
        if not path.is_file():
            raise FileNotFoundError(path)
        history = replay(path, task)
        result = {"task": task, "messages": len(history), "raw_estimate": estimate(history)}
        result.update(tool_turn_retention(history, expected))
        raw_text = json.dumps(history, ensure_ascii=False)
        allowance = 5_000 * 4
        sliced = raw_text[:allowance // 4] + raw_text[-(allowance * 3 // 4):]
        result["naive_head_tail_retained"] = expected in sliced
        try:
            compacted = compact_messages(history, estimate, target=5_000, protocol="anthropic")
            result["single_turn_forwarded"] = compacted.audit["forwarded_tokens"]
            result["original_task_preserved"] = history[0] in compacted.messages
            result["latest_tool_message_preserved"] = compacted.messages[-1] == history[-1]
        except CompactionError as error:
            result["single_turn_error"] = error.code
        question = {"role": "user", "content": f"In the initial baseline, what error did {focus} produce?"}
        try:
            recalled = compact_messages(history + [question], estimate, target=5_000, protocol="anthropic")
            result["recall_forwarded"] = recalled.audit["forwarded_tokens"]
            result["baseline_failure_retained"] = expected in str(recalled.messages)
        except CompactionError as error:
            result["recall_error"] = error.code
        output.append(result)
    print(json.dumps(output, indent=2))
    return 0 if all(
        item.get("original_task_preserved") and item.get("latest_tool_message_preserved")
        and item.get("baseline_failure_retained") for item in output
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
