"""PostToolUse cap for Qwen's read-only source inspection results.

Claude Code accepts ``updatedToolOutput`` only when its value keeps the
original tool result's top-level shape. Keep the transformation deliberately
small and record sizes without logging source contents.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import os
import tempfile
import uuid


MAX_RESULT_CHARS = 4_000
TRUNCATION_PREFIX = "[QWEN_RESULT_TRUNCATED tool="


def _encoded_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _walk_text(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_text(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_text(child)


TEXT_KEYS = {"content", "text", "stdout", "stderr", "output", "result"}


def _clip_text(value: object, budget: int, marker: str,
               in_text: bool = False, marker_used: list[bool] | None = None) -> tuple[object, int, bool]:
    if marker_used is None:
        marker_used = [False]
    if isinstance(value, str):
        if not in_text:
            return value, budget, False
        if marker_used[0]:
            return "", budget, False
        if len(value) <= budget:
            return value, budget - len(value), False
        prefix_size = max(0, budget - len(marker))
        marker_used[0] = True
        return value[:prefix_size] + marker, 0, True
    if isinstance(value, dict):
        result = {}
        changed = False
        remaining = budget
        for key, child in value.items():
            result[key], remaining, child_changed = _clip_text(
                child, remaining, marker, key in TEXT_KEYS, marker_used)
            changed = changed or child_changed
        return result, remaining, changed
    if isinstance(value, list):
        result = []
        changed = False
        remaining = budget
        for child in value:
            clipped, remaining, child_changed = _clip_text(
                child, remaining, marker, in_text and isinstance(child, str), marker_used)
            result.append(clipped)
            changed = changed or child_changed
        return result, remaining, changed
    return value, budget, False


def cap_output(value: object, limit: int = MAX_RESULT_CHARS,
               marker_id: str = "", tool_name: str = "tool") -> tuple[object, dict]:
    """Return a same-root-shape result whose JSON encoding fits ``limit``."""
    original_size = _encoded_size(value)
    original_text_chars = sum(len(text) for text in _walk_text(value))
    if original_size <= limit:
        return value, {"changed": False, "original_chars": original_size,
                       "delivered_chars": original_size}

    marker = (f"{TRUNCATION_PREFIX}{tool_name} id={marker_id}; "
              f"original_chars={original_size}; limit={limit}. "
              "Narrow Read/Grep and retry before relying on missing matches.]")
    # Binary search by text characters, then verify the serialized output. The
    # verification accounts for JSON escaping, Unicode, CRLF, nested metadata,
    # and native output-array overhead.
    low, high = 0, min(original_text_chars, limit)
    best = None
    while low <= high:
        budget = (low + high) // 2
        if original_text_chars > budget:
            budget = max(0, budget - len(marker))
        candidate, _, _ = _clip_text(value, budget, marker, in_text=isinstance(value, str))
        size = _encoded_size(candidate)
        if size <= limit:
            best = candidate
            low = (low + high) // 2 + 1
        else:
            high = (low + high) // 2 - 1
    if best is None:
        # Structure/metadata alone exceeds the ceiling. Do not emit an invalid
        # or oversized replacement; the caller records that it could not cap.
        return value, {"changed": False, "cap_failed": True,
                       "original_chars": original_size, "delivered_chars": original_size}
    delivered_size = _encoded_size(best)
    return best, {"changed": delivered_size < original_size,
                  "original_chars": original_size, "delivered_chars": delivered_size,
                  "truncated": True}


def _append_audit(path: Path, record: dict) -> None:
    """Persist each receipt separately so parallel PostToolUse hooks cannot lose records."""
    receipt_dir = path.with_name(path.name + ".receipts")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    tool_use_id = record.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise ValueError("missing tool-use id for result receipt")
    # Unique names preserve duplicate receipts for the caller's audit to reject.
    destination = receipt_dir / f"{tool_use_id}.{uuid.uuid4().hex}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=receipt_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    payload = json.loads(sys.stdin.read())
    tool = payload.get("tool_name")
    scope = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if (scope.get("result_cap_enabled") is not True
            or tool not in {"Read", "Grep"}):
        return 0
    response = payload.get("tool_response")
    limit = scope.get("result_char_limit", MAX_RESULT_CHARS)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 128:
        raise ValueError("invalid result character limit")
    marker_id = uuid.uuid4().hex[:12]
    replacement, audit = cap_output(response, limit, marker_id, tool)
    audit.update({"tool": tool, "tool_use_id": payload.get("tool_use_id")})
    audit["marker_id"] = marker_id
    audit_path = scope.get("result_audit_path")
    if not isinstance(audit_path, str) or not audit_path.strip():
        raise ValueError("missing result audit path")
    _append_audit(Path(audit_path), audit)
    if audit.get("changed"):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": replacement,
        }}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
