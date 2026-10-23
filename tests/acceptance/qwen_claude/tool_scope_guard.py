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
# still bounded by exact-path access, per-operation quotas, and the transcript
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
            offset, limit = tool_input.get("offset", 1), tool_input.get("limit")
            ranged = (isinstance(offset, int) and not isinstance(offset, bool)
                      and isinstance(limit, int) and not isinstance(limit, bool))
            if line_count > MAX_READ_LINES and (
                    not ranged or offset < 1 or limit < 1 or limit > MAX_READ_LINES):
                return "Files over 200 lines require Read offset and limit (maximum 200)"
            if canonical_target in range_restricted:
                ranges_by_path = scope.get("read_ranges", {})
                ranges = ranges_by_path.get(canonical_target, []) if isinstance(ranges_by_path, dict) else []
                if "offset" not in tool_input or not ranged or offset < 1 or limit < 1 or limit > MAX_READ_LINES:
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


def claim_scout_attempt(tool: str, scope: dict) -> str | None:
    """Charge a scout Read/Grep only after exact scope authorization succeeds."""
    limit = scope.get("scout_tool_call_limit", 8)
    counter_dir = scope.get("scout_call_counter_dir")
    if (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
            or not isinstance(counter_dir, str) or not counter_dir.strip()):
        return "Source scout call counter is invalid"
    counter_path = Path(counter_dir)
    per_tool_limits = scope.get("scout_tool_limits", {})
    per_tool_limit = per_tool_limits.get(tool) if isinstance(per_tool_limits, dict) else None
    if not isinstance(per_tool_limit, int) or isinstance(per_tool_limit, bool) or per_tool_limit < 1:
        return "Source scout per-tool call budget is invalid"
    prefix = "search" if tool == "Grep" else "read"
    total_ticket = _claim_ticket(counter_path, "total", limit)
    if total_ticket is None:
        return f"Source scout is limited to {limit} combined Read/Grep attempts"
    if _claim_ticket(counter_path, prefix, per_tool_limit) is None:
        # Return the combined ticket: an exhausted per-tool quota must not
        # consume capacity reserved for other authorized operations.
        total_ticket.unlink(missing_ok=True)
        return f"Source scout is limited to {per_tool_limit} {prefix} attempts"
    return None


def claim_scout_denial(scope: dict) -> str | None:
    """Bound denied requests separately so they do not consume allowed reads."""
    counter_dir = scope.get("scout_call_counter_dir")
    limit = scope.get("scout_denied_attempt_limit", 4)
    if (not isinstance(counter_dir, str) or not counter_dir.strip()
            or not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        return "Source scout denied-attempt counter is invalid"
    if _claim_ticket(Path(counter_dir), "denied", limit) is None:
        return f"Source scout exceeded {limit} guard-denied attempts"
    return None


def claim_implementation_attempt(tool: str, scope: dict) -> str | None:
    """Hard-cap authorized implementation reads and searches per child run."""
    counter_dir = scope.get("implementation_call_counter_dir")
    limits = scope.get("implementation_tool_limits", {})
    per_tool_limit = limits.get("Read" if tool == "Read" else "Grep") if isinstance(limits, dict) else None
    if (not isinstance(counter_dir, str) or not counter_dir.strip()
            or not isinstance(per_tool_limit, int) or isinstance(per_tool_limit, bool)
            or per_tool_limit < 1):
        return "Implementation call counter is invalid"
    counter_path = Path(counter_dir)
    pre_edit_limit = scope.get("implementation_pre_edit_read_limit", 4)
    if (tool == "Read" and not (counter_path / "first-edit.ticket").exists()
            and isinstance(pre_edit_limit, int) and not isinstance(pre_edit_limit, bool)
            and pre_edit_limit > 0
            and len(list(counter_path.glob("pre-edit-read-*.ticket"))) >= pre_edit_limit):
        return (f"Implementation is limited to {pre_edit_limit} Reads before the first edit; "
                "edit an assigned file now")
    total_limit = limits.get("Read", 0) + limits.get("Grep", 0)
    total_ticket = _claim_ticket(counter_path, "total", total_limit)
    if total_ticket is None:
        return f"Implementation is limited to {total_limit} combined Read/Grep attempts; edit the assigned files now"
    prefix = "read" if tool == "Read" else "search"
    if _claim_ticket(counter_path, prefix, per_tool_limit) is None:
        total_ticket.unlink(missing_ok=True)
        return f"Implementation is limited to {per_tool_limit} {prefix} attempts; edit the assigned files now"
    if tool == "Read" and not (counter_path / "first-edit.ticket").exists():
        _claim_ticket(counter_path, "pre-edit-read", pre_edit_limit)
    return None


def mark_implementation_edit(scope: dict) -> None:
    counter_dir = scope.get("implementation_call_counter_dir")
    if isinstance(counter_dir, str) and counter_dir.strip():
        try:
            descriptor = os.open(Path(counter_dir) / "first-edit.ticket",
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return
        os.close(descriptor)


def claim_implementation_denial(scope: dict) -> None:
    """Record guard denials separately from the authorized operation budget."""
    counter_dir = scope.get("implementation_call_counter_dir")
    limit = scope.get("implementation_denied_attempt_limit", 4)
    if (isinstance(counter_dir, str) and counter_dir.strip()
            and isinstance(limit, int) and not isinstance(limit, bool) and limit > 0):
        _claim_ticket(Path(counter_dir), "denied", limit)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        tool = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        scope_path = Path(sys.argv[1])
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        reason = authorize(tool, tool_input if isinstance(tool_input, dict) else {}, scope)
        if scope.get("scout_mode") is True and tool in {"Read", "Grep"}:
            if reason:
                claim_scout_denial(scope)
                _emit_deny(reason)
                return 0
            ticket_reason = claim_scout_attempt(tool, scope)
            if ticket_reason:
                claim_scout_denial(scope)
                _emit_deny(ticket_reason)
                return 0
        if scope.get("implementation_mode") is True and tool in {"Read", "Grep"}:
            if reason:
                claim_implementation_denial(scope)
                _emit_deny(reason)
                return 0
            ticket_reason = claim_implementation_attempt(tool, scope)
            if ticket_reason:
                claim_implementation_denial(scope)
                _emit_deny(ticket_reason)
                return 0
        if scope.get("implementation_mode") is True and tool in {"Edit", "Write"} and reason is None:
            mark_implementation_edit(scope)
        if reason:
            _emit_deny(reason)
            return 0
    except Exception as exc:  # Fail closed if the scope or hook input is malformed.
        _emit_deny(f"File tool scope validation failed: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
