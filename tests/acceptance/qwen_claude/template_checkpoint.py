"""Run caller-owned syntax diagnostics after Qwen edits a scoped EJS batch.

This PostToolBatch hook gives the worker actionable diagnostics before its next
turn. It never edits files, and the autonomous runner's validators remain the
acceptance gate after the worker returns.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


MAX_CONTEXT_CHARS = 4000
MAX_OUTPUT_CHARS = 1200
MAX_VALIDATORS = 4
VALIDATOR_TIMEOUT = 30
TOTAL_VALIDATOR_TIMEOUT = 25


def _canonical(root: Path, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return None


def _successful(response: object) -> bool:
    """Fail closed on PostToolBatch's serialized tool_result content."""
    if isinstance(response, dict):
        if (response.get("is_error") is True or response.get("error")
                or response.get("success") is False):
            return False
        if isinstance(response.get("text"), str):
            return _successful(response["text"])
        return True
    if isinstance(response, list):
        if not response:
            return False
        return all(_successful(item) for item in response)
    if not isinstance(response, str) or not response.strip():
        return False
    try:
        decoded = json.loads(response)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, (dict, list)):
        return _successful(decoded)
    first_line = response.lstrip().splitlines()[0].strip().lower()
    return not (first_line.startswith(("error:", "failed:", "failure:"))
                or first_line.startswith("tool error")
                or "old_string not found" in first_line
                or "permission denied" in first_line)


def changed_allowed_paths(payload: dict, scope: dict) -> list[str]:
    root = Path(scope["root"]).resolve()
    allowed = {_canonical(root, value) for value in scope.get("write_allowed", [])}
    allowed.discard(None)
    found = set()
    for call in payload.get("tool_calls", []):
        if not isinstance(call, dict) or call.get("tool_name") not in {"Edit", "Write"}:
            continue
        if not _successful(call.get("tool_response")):
            continue
        tool_input = call.get("tool_input")
        if not isinstance(tool_input, dict):
            continue
        target = _canonical(root, tool_input.get("file_path"))
        if target in allowed:
            found.add(target.relative_to(root).as_posix())
    return sorted(found)


def _claim_once(receipt: Path) -> bool:
    receipt.parent.mkdir(parents=True, exist_ok=True)
    if receipt.exists():
        return False
    lock_path = receipt.with_suffix(receipt.suffix + ".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"started_at": time.time()}, stream)
    return True


def _write_receipt(receipt: Path, result: dict) -> None:
    temporary = receipt.with_suffix(receipt.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, receipt)


def _emit(context: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolBatch",
        "additionalContext": context[:MAX_CONTEXT_CHARS],
    }}))


def run_checkpoint(payload: dict, scope: dict, receipt_path: Path) -> dict | None:
    if payload.get("agent_type") != "qwen-autonomous-implementer":
        return None
    paths = changed_allowed_paths(payload, scope)
    if not paths or not _claim_once(receipt_path):
        return None

    root = Path(scope["root"]).resolve()
    ejs_paths = [path for path in paths if Path(path).suffix.lower() == ".ejs"]
    if not ejs_paths:
        result = {"status": "progress", "paths": paths, "completed_at": time.time()}
        _emit("First scoped implementation edit recorded for " + ", ".join(paths) +
              ". Continue the assigned step; EJS syntax feedback will run after a template edit.")
        _write_receipt(receipt_path, result)
        try:
            receipt_path.with_suffix(receipt_path.suffix + ".lock").unlink()
        except OSError:
            pass
        return result

    validators = scope.get("validators")
    if (not isinstance(validators, list) or not 1 <= len(validators) <= MAX_VALIDATORS
            or any(not isinstance(argv, list) or not argv
                   or any(not isinstance(part, str) for part in argv)
                   for argv in validators)):
        result = {"status": "invalid_checkpoint_configuration", "paths": paths}
        _emit("Early EJS checkpoint could not run: caller validator configuration is invalid. "
              "Continue implementation; the runner will apply final acceptance checks.")
    else:
        temp_dir = scope.get("checkpoint_temp_dir")
        if isinstance(temp_dir, str) and temp_dir.strip():
            env = dict(os.environ)
            env["TEMP"] = temp_dir
            env["TMP"] = temp_dir
        else:
            env = None
        diagnostics = []
        passed = True
        started = time.monotonic()
        for argv in validators:
            remaining = TOTAL_VALIDATOR_TIMEOUT - (time.monotonic() - started)
            if remaining <= 0:
                passed = False
                diagnostics.append({"argv": argv, "exit": 124, "stdout": "",
                                    "stderr": f"total checkpoint validator budget exhausted after {TOTAL_VALIDATOR_TIMEOUT}s"})
                break
            try:
                proc = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      timeout=min(VALIDATOR_TIMEOUT, max(1, int(remaining))), shell=False,
                                      env=env)
                exit_code = proc.returncode
                stdout, stderr = proc.stdout or "", proc.stderr or ""
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                stdout, stderr = str(exc.stdout or ""), f"validator timed out after {VALIDATOR_TIMEOUT}s"
            except OSError as exc:
                exit_code = 127
                stdout, stderr = "", f"validator could not start: {type(exc).__name__}"
            passed = passed and exit_code == 0
            diagnostics.append({"argv": argv, "exit": exit_code,
                                "stdout": stdout[-MAX_OUTPUT_CHARS:],
                                "stderr": stderr[-MAX_OUTPUT_CHARS:]})
        result = {"status": "passed" if passed else "failed", "paths": ejs_paths,
                  "validators": diagnostics, "completed_at": time.time()}
        if passed:
            context = ("Early EJS syntax checkpoint passed for the current template batch: "
                       + ", ".join(ejs_paths)
                       + ". Continue the assigned step; the runner will still run final acceptance checks.")
        else:
            errors = "\n".join(
                f"$ {' '.join(item['argv'])} (exit {item['exit']})\n"
                f"{item['stdout']}\n{item['stderr']}"
                for item in diagnostics if item["exit"] != 0)
            context = ("Early EJS syntax checkpoint failed for " + ", ".join(ejs_paths)
                       + ". Fix these diagnostics before continuing; do not weaken tests.\n"
                       + errors)
        _emit(context)

    _write_receipt(receipt_path, result)
    try:
        receipt_path.with_suffix(receipt_path.suffix + ".lock").unlink()
    except OSError:
        pass
    return result


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        payload = json.loads(sys.stdin.read())
        scope = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        root = Path(scope["root"]).resolve()
        receipt_path = Path(sys.argv[1]).with_suffix(".receipt.json")
        if isinstance(payload, dict):
            run_checkpoint(payload, scope, receipt_path)
    except Exception as exc:
        _emit("Early EJS checkpoint hook could not complete (" + type(exc).__name__ + "). "
              "Continue implementation; the runner will apply final acceptance checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
