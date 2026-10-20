"""PreToolUse guard for Qwen worker file operations.

Claude Code runs this hook before Read/Grep/Edit/Write. The transcript auditor
remains a second, independent check after the worker exits.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


MAX_READ_LINES = 200
MAX_SCOUT_READ_LINES = 80
# Grep's Qwen tool schema commonly defaults head_limit to 60. Scout output is
# still bounded by exact-path access, a six-call budget, and the transcript
# character ceiling, so tolerate that default instead of stalling on a deny.
MAX_SCOUT_GREP_HEAD_LIMIT = 100


def _canonical(root: Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("missing file path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def _emit_deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "QWEN_SCOPE_GUARD_DENY: " + reason,
    }}))


def _claim_ticket(counter_dir: Path, prefix: str, limit: int) -> Path | None:
    for index in range(1, limit + 1):
        ticket = counter_dir / f"{prefix}-{index}.ticket"
        try:
            descriptor = os.open(ticket, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            return ticket
        except FileExistsError:
            continue
    return None


def authorize(tool: str, tool_input: dict, scope: dict) -> str | None:
    root = Path(scope["root"]).resolve()
    read_allowed = {str(_canonical(root, value)) for value in scope["read_allowed"]}
    write_allowed = {str(_canonical(root, value)) for value in scope["write_allowed"]}

    if tool in {"Read", "Grep"}:
        raw = tool_input.get("file_path") if tool == "Read" else tool_input.get("path")
        target = _canonical(root, raw)
        if target != root and root not in target.parents:
            return "File access must stay inside the disposable workspace"
        if str(target) not in read_allowed:
            return f"{tool} is limited to exact assigned or declared-reference files"
        if not target.is_file():
            return f"{tool} requires an existing file, not a directory"
        if tool == "Grep" and scope.get("scout_mode") is True:
            pattern = tool_input.get("pattern")
            head_limit = tool_input.get("head_limit")
            if not isinstance(pattern, str) or len(pattern) > 240:
                return "Source scout Grep patterns must be strings of at most 240 characters"
            if (not isinstance(head_limit, int) or isinstance(head_limit, bool)
                    or not 1 <= head_limit <= MAX_SCOUT_GREP_HEAD_LIMIT):
                return f"Source scout Grep requires head_limit from 1 to {MAX_SCOUT_GREP_HEAD_LIMIT}"
            for key in ("-A", "-B", "-C"):
                context = tool_input.get(key)
                if context is not None and (not isinstance(context, int) or isinstance(context, bool)
                                            or not 0 <= context <= 5):
                    return "Source scout Grep context is limited to 5 lines"
        canonical_target = str(target)
        range_restricted = set(scope.get("range_restricted", []))
        if tool == "Grep" and canonical_target in range_restricted:
            return "Grep is unavailable for files limited to caller-verified source ranges"
        if tool == "Read":
            try:
                with target.open("r", encoding="utf-8", errors="replace") as source:
                    line_count = 0
                    for line_count, _ in enumerate(source, start=1):
                        if line_count > MAX_READ_LINES:
                            break
            except OSError:
                return "Read target is missing or unreadable"
            offset, limit = tool_input.get("offset"), tool_input.get("limit")
            ranged = (isinstance(offset, int) and not isinstance(offset, bool)
                      and isinstance(limit, int) and not isinstance(limit, bool))
            if line_count > MAX_READ_LINES and (
                    not ranged or offset < 1 or limit < 1 or limit > MAX_READ_LINES):
                return "Files over 200 lines require Read offset and limit (maximum 200)"
            if canonical_target in range_restricted:
                ranges_by_path = scope.get("read_ranges", {})
                ranges = ranges_by_path.get(canonical_target, []) if isinstance(ranges_by_path, dict) else []
                if not ranged or offset < 1 or limit < 1 or limit > MAX_READ_LINES:
                    return "Read must use offset and limit within a caller-verified source range"
                end = offset + limit - 1
                contained = any(
                    isinstance(item, dict)
                    and isinstance(item.get("offset"), int) and not isinstance(item.get("offset"), bool)
                    and isinstance(item.get("limit"), int) and not isinstance(item.get("limit"), bool)
                    and item["offset"] <= offset
                    and end <= item["offset"] + item["limit"] - 1
                    for item in ranges
                )
                if not contained:
                    return "Read range is outside caller-verified source ranges"
            if scope.get("scout_mode") is True and ranged and limit > MAX_SCOUT_READ_LINES:
                return f"Source scout Read limit is capped at {MAX_SCOUT_READ_LINES} lines"
    elif tool in {"Edit", "Write"}:
        raw = tool_input.get("file_path")
        target = _canonical(root, raw)
        if target != root and root not in target.parents:
            return "Writes must stay inside the disposable workspace"
        if str(target) not in write_allowed:
            return f"{tool} is limited to exact assigned edit files"
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        tool = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        scope_path = Path(sys.argv[1])
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        reason = authorize(tool, tool_input if isinstance(tool_input, dict) else {}, scope)
        if reason:
            _emit_deny(reason)
            return 0
        if scope.get("scout_mode") is True and tool in {"Read", "Grep"}:
            limit = scope.get("scout_tool_call_limit", 6)
            counter_dir = scope.get("scout_call_counter_dir")
            if (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
                    or not isinstance(counter_dir, str) or not counter_dir.strip()):
                _emit_deny("Source scout call counter is invalid")
                return 0
            counter_path = Path(counter_dir)
            per_tool_limits = scope.get("scout_tool_limits", {})
            per_tool_limit = per_tool_limits.get(tool) if isinstance(per_tool_limits, dict) else None
            if not isinstance(per_tool_limit, int) or isinstance(per_tool_limit, bool) or per_tool_limit < 1:
                _emit_deny("Source scout per-tool call budget is invalid")
                return 0
            prefix = "search" if tool in {"Grep", "Glob"} else "read"
            tool_ticket = _claim_ticket(counter_path, prefix, per_tool_limit)
            if tool_ticket is None:
                _emit_deny(f"Source scout is limited to {per_tool_limit} {prefix} calls")
                return 0
            if _claim_ticket(counter_path, "total", limit) is None:
                # Roll back the per-tool reservation so denied calls consume no
                # authorized-operation budget, even when calls race.
                tool_ticket.unlink(missing_ok=True)
                _emit_deny(f"Source scout is limited to {limit} combined Read/Grep calls")
                return 0
    except Exception as exc:  # Fail closed if the scope or hook input is malformed.
        _emit_deny(f"File tool scope validation failed: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
