"""PreToolUse guard for Qwen worker file operations.

Claude Code runs this hook before Read/Grep/Edit/Write. The transcript auditor
remains a second, independent check after the worker exits.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


MAX_READ_LINES = 200


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
    except Exception as exc:  # Fail closed if the scope or hook input is malformed.
        _emit_deny(f"File tool scope validation failed: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
