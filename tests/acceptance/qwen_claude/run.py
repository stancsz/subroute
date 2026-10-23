"""Run real Claude Code editing tasks against the local Qwen staging route.

The workspaces are disposable copies of the checked-in fixtures. The script
restores the staging routing and advisor policies even when a task fails.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import uuid

import httpx
from incident_fixture import complete_incident_intake, make_incident_evidence


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:4005"
MODEL = "qwen3.8-27b"
QWEN_INPUT_TARGET = 32_768
QWEN_CONTEXT_TOKENS = 40_960  # Native per-slot capacity, not client history.
QWEN_CLIENT_CONTEXT_TOKENS = 256_000
QWEN_RAW_INPUT_LIMIT = 1_000_000
# Sift owns32K forwarding; the client retains a larger bounded history.
QWEN_AUTO_COMPACT_PERCENT = 90
CHILD_MODE = "qwen-child"
DIRECT_MODE = "direct"
# Keep the one-shot delegating coordinator on the same supported local Qwen
# route as the implementation child. An unavailable coordinator model otherwise
# prevents the child Agent call before the Qwen worker receives the task.
CHILD_PARENT_MODEL = MODEL
CHILD_AGENT_NAME = "qwen-runner-implementer"
CHILD_MARKER_PREFIX = "QWEN_CHILD_COMPLETE_"
CHILD_TURN_CAP = 24
CHILD_TIMEOUT = 600
CHILD_IDLE_TIMEOUT = 240
ORIGINAL_TASKS = ("route_rules", "cache_report")
EXTENDED_TASKS = ("context_compaction", "policy_snapshot")
DOMAIN_TASKS = ("sse_relay", "billing_ledger", "appointment_slots", "webhook_delivery", "build_planner", "incident_reconstruction")
TASKS = {
    "incident_reconstruction": (
        "findings.json",
        "Read README.md and follow its incident investigation contract. Run the "
        "tests before editing, read all four complete incident JSONL files "
        "sequentially one per tool call, and reconstruct INC-284 from the evidence. "
        "Edit only findings.json. Run the tests again and report the actual result.",
    ),
    "route_rules": (
        "route_rules.py",
        "Read route_rules.py and test_route_rules.py. Fix the routing bugs in "
        "route_rules.py so its tests pass. Keep the public function signature. "
        "Edit only route_rules.py. Explain the actual correction briefly.",
    ),
    "cache_report": (
        "cache_report.py",
        "Read cache_report.py and test_cache_report.py. Implement "
        "summarize_events to satisfy the documented contract and tests. "
        "Edit only cache_report.py. Explain the implementation briefly.",
    ),
    "context_compaction": (
        "context_compaction.py",
        "Read context_compaction.py and test_context_compaction.py. Implement "
        "compact_messages for this gateway-style context planner. Preserve role "
        "and tool-chain semantics, reject unsafe inputs, and make the tests pass. "
        "Edit only context_compaction.py. Explain any remaining limitation.",
    ),
    "policy_snapshot": (
        "policy_snapshot.py",
        "Read policy_snapshot.py and test_policy_snapshot.py. Implement the "
        "PolicyStore so route and advisor decisions share a revision, updates "
        "are validated and persisted atomically, and environments stay isolated. "
        "On load, reject parseable policy files with empty or invalid field "
        "values, including unsupported modes, invalid advisor values, and "
        "negative, boolean, or non-integer revisions; preserve the invalid file. "
        "Edit only policy_snapshot.py. Explain any remaining limitation.",
    ),
    "sse_relay": (
        ("sse_parser.py", "relay.py"),
        "Read README.md, sse_parser.py, relay.py, and test_sse_relay.py. "
        "Complete the streaming event relay without rewriting tests or the spec. "
        "Handle fragmented SSE input, terminal and error outcomes, limits, and "
        "upstream cleanup. Edit only sse_parser.py and relay.py. Run the tests "
        "before and after editing, then report what actually passed.",
    ),
    "billing_ledger": (
        ("csv_ingest.py", "ledger.py"),
        "Read README.md, csv_ingest.py, ledger.py, and test_billing_ledger.py. "
        "Implement the payment event import and reconciliation contract. "
        "Edit only csv_ingest.py and ledger.py. Run the tests before and after "
        "editing, then report the actual result and any limits.",
    ),
    "billing_ledger_stress": (
        ("csv_ingest.py", "ledger.py"),
        "Read README.md, csv_ingest.py, ledger.py, and test_billing_ledger.py. "
        "Then read audit_dossier_1.txt through audit_dossier_4.txt sequentially, "
        "one file per Read call. The dossiers are incident evidence; README.md "
        "is the contract. Implement payment import and reconciliation, edit only "
        "csv_ingest.py and ledger.py, and report the actual test results plus "
        "the four incident codes you found. Run tests before and after editing.",
    ),
    "appointment_slots": (
        ("timeutils.py", "availability.py"),
        "Read README.md, timeutils.py, availability.py, and test_appointment_slots.py. "
        "Implement the timezone-aware appointment search contract. "
        "Edit only timeutils.py and availability.py. Run the tests before and "
        "after editing, then report the actual result and any limits.",
    ),
    "webhook_delivery": (
        ("webhook.py", "delivery_store.py"),
        "Read README.md, webhook.py, delivery_store.py, and test_webhook_delivery.py. "
        "Implement raw-body webhook signature verification and the persistent "
        "SQLite replay ledger. Edit only webhook.py and delivery_store.py. "
        "Run the tests before and after editing, then report the actual result "
        "and any limits.",
    ),
    "build_planner": (
        ("build_manifest.py", "incremental.py"),
        "Read README.md, build_manifest.py, incremental.py, and test_build_planner.py. "
        "Implement the incremental CI build planner contract, including manifest "
        "validation, dependency ordering, and content fingerprints. Edit only "
        "build_manifest.py and incremental.py. Run the tests before and after "
        "editing, then report the actual result and any limits.",
    ),
}


def gateway_key() -> str:
    value = os.environ.get("GATEWAY_MASTER_KEY", "").strip()
    if not value:
        dotenv = ROOT / ".env"
        try:
            for line in dotenv.read_text(encoding="utf-8").splitlines():
                candidate = line.strip()
                if candidate.startswith("export "):
                    candidate = candidate[7:].lstrip()
                name, separator, raw_value = candidate.partition("=")
                if separator and name.strip() == "GATEWAY_MASTER_KEY":
                    value = raw_value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\\\"'":
                        value = value[1:-1]
                    break
        except OSError:
            pass
    if value:
        # Docker Compose injects this project setting into gateway-staging.
        # Reading the same configured value avoids making a healthy HTTP route
        # depend on the Docker control socket being responsive.
        return value

    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "gateway-staging", "printenv", "GATEWAY_MASTER_KEY"],
            cwd=ROOT, capture_output=True, text=True, timeout=15, check=True,
        )
        value = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        value = ""
    if not value:
        raise RuntimeError("staging gateway master key was unavailable from Docker and project configuration")
    return value


def set_policy(client: httpx.Client, state: dict, *, test: bool) -> None:
    advisor_locked = False
    if test:
        advisor_locked = client.get("/api/routing-options").json().get("advisor_model_locked") is True
    routing = {
        "model": MODEL if test else state["active_model"],
        "mode": "alias" if test else state["mode"],
        "reasoning_effort": state["reasoning_effort"],
        "fast_mode": state.get("fast_mode", False),
    }
    client.post("/api/active-model", json=routing).raise_for_status()
    # A locked account Advisor cannot be disabled for the staging run. Leave
    # that policy untouched rather than sending an invalid null update.
    if test and advisor_locked:
        return
    advisor = {
        "advisor_model": None if test else state["advisor_model"],
        "reasoning_effort": None if test else state["advisor_reasoning_effort"],
        "advisor_sensitivity": state["advisor_sensitivity"],
    }
    client.post("/api/advisor-model", json=advisor).raise_for_status()


def task_source_names(name: str) -> tuple[str, ...]:
    source_names, _ = TASKS[name]
    return tuple(source_names) if isinstance(source_names, (tuple, list)) else (source_names,)


def child_task_text(name: str) -> str:
    """Remove test-execution requests from children that only receive Read/Edit."""
    _, task = TASKS[name]
    replacements = (
        ("Run the tests before editing, read ", "Read "),
        ("the actual test results plus the four incident codes you found. Run tests before and after editing.",
         "the four incident codes you found."),
        ("tests before editing, ", ""),
        ("Edit only findings.json. Run the tests again and report the actual result.",
         "Edit only findings.json."),
        ("Run the tests before and after editing, then report what actually passed.", ""),
        ("Run the tests before and after editing, then report the actual result and any limits.", ""),
        ("Run the tests before and after editing, then report the actual result and any limits.", ""),
        ("Run the tests before and after editing, then report the actual result ", ""),
        ("Run the tests before and after editing, then report the actual result", ""),
        ("Run tests before and after editing.", ""),
        ("Run the tests before and after editing.", ""),
    )
    for old, new in replacements:
        task = task.replace(old, new)
    task = " ".join(task.split())
    return task


def claude_command(*, allow_shell: bool = False, max_turns: int | None = None,
                   model: str | None = None, tools: str | None = None,
                   bare: bool = True, agents: str | None = None,
                   dispatch_mode: str = DIRECT_MODE,
                   child_tools: str = "Read,Edit",
                   settings: str | None = None) -> list[str]:
    executable = Path(os.environ["APPDATA"]) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    if not executable.is_file():
        raise RuntimeError(f"Claude Code executable missing: {executable}")
    if dispatch_mode not in {DIRECT_MODE, CHILD_MODE}:
        raise ValueError(f"unsupported dispatch mode: {dispatch_mode}")
    allowed = tools if tools is not None else ("Read,Edit,PowerShell" if allow_shell else "Read,Edit")
    if dispatch_mode == CHILD_MODE:
        # Child tools are intersected with the coordinator's enabled tool set.
        # Expose only the file tools requested by the scoped worker; omit shell by default.
        child_allowed = {item.strip() for item in child_tools.split(",") if item.strip()}
        if not child_allowed or child_allowed - {"Read", "Grep", "Glob", "Edit", "Write"}:
            raise ValueError("child_tools may contain only Read, Grep, Glob, Edit, and Write")
        child_tools = ",".join(name for name in ("Read", "Grep", "Glob", "Edit", "Write") if name in child_allowed)
        parent_tools = allowlist = "Agent," + child_tools
        command = [str(executable), "-p", "--no-session-persistence", "--restricted"]
    else:
        parent_tools = allowlist = allowed
        command = [str(executable), "-p"]
        if bare:
            command.append("--bare")
        command.append("--no-session-persistence")
    command += [
        "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands", "--tools", parent_tools, "--allowedTools", allowlist,
        "--permission-mode", "dontAsk", "--model", model or (CHILD_PARENT_MODEL if dispatch_mode == CHILD_MODE else MODEL),
        "--max-turns", str(max_turns if max_turns is not None else (
            CHILD_TURN_CAP if dispatch_mode == CHILD_MODE else (12 if allow_shell else 10))),
        "--output-format", "stream-json", "--include-partial-messages", "--verbose",
    ]
    if agents is not None:
        command += ["--agents", agents]
    if settings is not None:
        command += ["--settings", settings]
    return command


def child_agent_spec(name: str, directory: Path, marker: str,
                     repair_feedback: str | None = None) -> str:
    task = child_task_text(name)
    scope = [str((directory / source).resolve()) for source in task_source_names(name)]
    repair_instructions = ""
    if repair_feedback:
        repair_instructions = (
            "\n\nThis is the single authorized repair/continuation round after independent visible tests failed. "
            "The first child may have stopped mid-task at its turn limit; treat the current allowed source files as "
            "partial work, complete every original requirement, and do not stop after fixing only the first visible assertion. "
            "Use the failure output below to guide the repair. Do not edit tests or any other files. "
            "The runner will execute visible tests once more after you return.\n\n"
            "Visible test failure output:\n" + repair_feedback[:5000]
        )
    definition = {
        "description": f"Bounded Qwen implementation worker for {name}.",
        "prompt": (
            "Complete this task autonomously using one inspect-edit-finish pass. First read each "
            "task-required source, contract, and test file once; then make the necessary edits. "
            "The runner validates your changes after you return, so do not run tests, reread files, "
            "or make speculative cleanup edits. Re-read a file only once if a concrete contradiction "
            "blocks the assigned implementation. After the required edits are complete, report briefly "
            "and return the required marker immediately; do not wait for external validation. You may "
            "read fixture files and tests, but edit "
            "only the exact absolute source paths listed below. Do not delegate, use shell "
            "commands, or claim test results; the runner executes tests independently after "
            "you return. Report actual changes and limitations. End with the unique marker "
            "only after the assigned edit is complete.\n\n"
            f"Task:\n{task}{repair_instructions}\n\nAllowed edit paths:\n" + "\n".join(f"- {path}" for path in scope) + "\n"
            f"Completion marker: {marker}"
        ),
        "tools": ["Read", "Edit"],
        "model": MODEL,
        "maxTurns": CHILD_TURN_CAP,
    }
    return json.dumps({CHILD_AGENT_NAME: definition})


def coordinator_prompt(name: str, directory: Path, marker: str) -> str:
    task = child_task_text(name)
    scope = [str((directory / source).resolve()) for source in task_source_names(name)]
    return (
        f"Delegate the complete task exactly once to {CHILD_AGENT_NAME} in the foreground. "
        "Do not read or edit files yourself, do not launch any other agent, and wait for the "
        "child's terminal result. Report its actual result and do not claim the marker unless "
        "it appears in the child tool result.\n\n"
        f"Task:\n{task}\n\nAllowed edit paths:\n" + "\n".join(f"- {path}" for path in scope) + "\n"
        f"Required child marker: {marker}"
    )


def coordinator_repair_prompt(name: str, directory: Path, marker: str,
                               repair_feedback: str) -> str:
    scope = [str((directory / source).resolve()) for source in task_source_names(name)]
    return (
        f"The first child implementation failed the runner's visible tests for {name}. "
        f"Delegate exactly one foreground repair child to {CHILD_AGENT_NAME}; do not read or edit files yourself, "
        "do not run tools other than that one foreground Agent call, and wait for its terminal result. "
        "The child must edit only these exact paths:\n" + "\n".join(f"- {path}" for path in scope) +
        "\n\nThe child has the visible test failure output and may make one bounded correction. "
        f"Report the child's actual result. Required child marker: {marker}.\n\nVisible test failure:\n{repair_feedback[:5000]}"
    )


def child_returned_successfully(events: list[dict], marker: str) -> bool:
    calls = [block for event in events if event.get("type") == "assistant"
             for block in event.get("message", {}).get("content", [])
             if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Agent"]
    if len(calls) != 1 or calls[0].get("input", {}).get("subagent_type") != CHILD_AGENT_NAME:
        return False
    for event in events:
        if event.get("type") != "user":
            continue
        result = next((block for block in event.get("message", {}).get("content", [])
                       if isinstance(block, dict) and block.get("type") == "tool_result"
                       and block.get("tool_use_id") == calls[0].get("id")), None)
        if result:
            return marker in str(result.get("content", "")) and not result.get("is_error", False)
    return False


def qwen_child_checks(name: str, directory: Path, events: list[dict], marker: str, *,
                      final_success: bool, changed_files: list[str],
                      extra_files: list[str], unauthorized_changes: list[str],
                      tests_passed: bool, qwen_route_calls: int,
                      qwen_sift_requests: int) -> dict:
    source_names = task_source_names(name)
    scope = set(source_names)
    calls = [block for event in events if event.get("type") == "assistant"
             for block in event.get("message", {}).get("content", [])
             if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Agent"]
    coordinator_tools = [block.get("name") for event in events
                         if event.get("type") == "assistant" and not event.get("parent_tool_use_id")
                         for block in event.get("message", {}).get("content", [])
                         if isinstance(block, dict) and block.get("type") == "tool_use"]
    call = calls[0] if len(calls) == 1 else {}
    request = call.get("input") or {}
    child_tools = [block.get("name") for event in events
                   if event.get("type") == "assistant" and event.get("parent_tool_use_id") == call.get("id")
                   for block in event.get("message", {}).get("content", [])
                   if isinstance(block, dict) and block.get("type") == "tool_use"]
    child_result = None
    if call.get("id"):
        for event in events:
            if event.get("type") != "user":
                continue
            child_result = next((block for block in event.get("message", {}).get("content", [])
                                 if isinstance(block, dict) and block.get("type") == "tool_result"
                                 and block.get("tool_use_id") == call["id"]), None)
            if child_result:
                break
    def text_content(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(text_content(item) for item in value)
        if isinstance(value, dict):
            return "\n".join(text_content(value.get(key, "")) for key in ("text", "content"))
        return ""
    child_return = text_content(child_result.get("content", "")) if child_result else ""
    child_prompt = str(request.get("prompt", ""))
    exact_scope = [str((directory / source).resolve()) for source in source_names]
    checks = {
        "exactly_one_named_child": len(calls) == 1 and request.get("subagent_type") == CHILD_AGENT_NAME,
        "coordinator_only_dispatched_child": coordinator_tools == ["Agent"],
        "foreground_child": request.get("run_in_background") is False,
        "task_and_exact_scope_sent": marker in child_prompt and all(path in child_prompt for path in exact_scope),
        "child_received_edit_tool": "Edit" in child_tools,
        "child_tools_within_allowlist": set(child_tools).issubset({"Read", "Grep", "Glob", "Edit", "Write"}),
        "marker_in_child_return": bool(child_result) and marker in child_return,
        "child_tool_succeeded": bool(child_result) and not child_result.get("is_error", False),
        "successful_terminal": final_success,
        "allowed_diff_only": bool(changed_files) and set(changed_files).issubset(scope)
            and not extra_files and not unauthorized_changes,
        "independent_after_check": tests_passed,
        "qwen_route_confirmed": qwen_route_calls > 0,
        "sift_route_confirmed": qwen_sift_requests > 0,
    }
    return {"checks": checks, "passed": all(checks.values()), "marker": marker,
            "child_return": child_return[:1200], "agent_calls": [item.get("input") for item in calls],
            "child_tool_names": child_tools, "qwen_route_calls": qwen_route_calls}


def cli_env(key: str, *, auto_compact_percent: int = QWEN_AUTO_COMPACT_PERCENT,
            client_context_tokens: int = QWEN_CLIENT_CONTEXT_TOKENS) -> dict[str, str]:
    if type(client_context_tokens) is not int or not QWEN_INPUT_TARGET <= client_context_tokens <= QWEN_RAW_INPUT_LIMIT:
        raise ValueError("Client history window must be32768..1000000tokens; native capacity remains40960")
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("ANTHROPIC_", "CLAUDE_"))}
    env.update({
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_API_KEY": key,
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(client_context_tokens),
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(client_context_tokens),
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(auto_compact_percent),
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "4096",
        "MAX_THINKING_TOKENS": "0",
    })
    return env


def redact_text(text: str, values: tuple[str, ...] = ()) -> str:
    """Redact known credentials and common token-bearing fields before persistence."""
    for value in values:
        if value:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", text)
    return re.sub(r'(?i)("(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"\s*:\s*")[^"]*',
                  r"\1[REDACTED]", text)


def unit_tests(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-q"], cwd=directory,
        capture_output=True, text=True, timeout=30,
    )


def checkpoint_receipt_complete(path: Path | None) -> bool:
    if path is None or not path.is_file():
        return False
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(receipt, dict) and receipt.get("status") in {"passed", "failed", "progress"}


def is_agent_dispatch_event(line: str) -> bool:
    """Start the first-edit watchdog when the coordinator actually delegates."""
    try:
        event = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return False
    message = event.get("message") if isinstance(event, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return (isinstance(content, list) and any(
        isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "Agent"
        for item in content))


def run_cli_bounded(command: list[str], *, task_input: str, cwd: Path,
                    env: dict[str, str], timeout: int,
                    idle_timeout: int = CHILD_IDLE_TIMEOUT,
                    transcript_path: Path | None = None,
                    redact_values: tuple[str, ...] = (),
                    checkpoint_receipt_path: Path | None = None,
                    checkpoint_deadline: int | None = None) -> tuple[str, str, int, str | None, bool | None]:
    """Run a child-mode CLI and terminate its process tree on a wall or idle timeout."""
    stdout_transcript = stderr_transcript = None
    if transcript_path is not None:
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_transcript = transcript_path.open("w", encoding="utf-8", newline="")
        stderr_transcript = transcript_path.with_suffix(".stderr.txt").open("w", encoding="utf-8", newline="")
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
        bufsize=1, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(task_input)
    process.stdin.close()
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def forward(stream_name: str, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                output_queue.put((stream_name, line))
        finally:
            output_queue.put((stream_name, None))
            stream.close()

    readers = [threading.Thread(target=forward, args=("stdout", process.stdout), daemon=True),
               threading.Thread(target=forward, args=("stderr", process.stderr), daemon=True)]
    for reader in readers:
        reader.start()
    output: list[str] = []
    errors: list[str] = []
    started = last_progress = time.monotonic()
    checkpoint_started: float | None = None
    closed_streams: set[str] = set()
    timeout_reason = None
    cleanup_verified = None
    try:
        while process.poll() is None or len(closed_streams) < 2:
            now = time.monotonic()
            if now - started >= timeout:
                timeout_reason = f"Claude Code exceeded the {timeout}-second total task limit"
            elif (checkpoint_deadline is not None and checkpoint_receipt_path is not None
                  and checkpoint_started is not None
                  and not checkpoint_receipt_complete(checkpoint_receipt_path)
                  and now - checkpoint_started >= checkpoint_deadline):
                timeout_reason = ("Qwen first-edit checkpoint was not reached within "
                                  f"{checkpoint_deadline} seconds after worker delegation")
            elif now - last_progress >= idle_timeout:
                timeout_reason = f"Claude Code produced no output for {idle_timeout} seconds"
            if timeout_reason:
                if os.name == "nt":
                    try:
                        killed = subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            capture_output=True, text=True, timeout=10,
                        )
                        cleanup_verified = killed.returncode == 0
                    except (OSError, subprocess.TimeoutExpired):
                        cleanup_verified = False
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                else:
                    process.kill()
                    cleanup_verified = True
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    cleanup_verified = False
                break
            try:
                stream_name, line = output_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                closed_streams.add(stream_name)
            else:
                (output if stream_name == "stdout" else errors).append(line)
                transcript = stdout_transcript if stream_name == "stdout" else stderr_transcript
                if transcript is not None:
                    transcript.write(redact_text(line, redact_values))
                    transcript.flush()
                if stream_name == "stdout" and checkpoint_started is None and is_agent_dispatch_event(line):
                    checkpoint_started = time.monotonic()
                last_progress = time.monotonic()
    except BaseException:
        if process.poll() is None:
            if os.name == "nt":
                try:
                    killed = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                            capture_output=True, text=True, timeout=10)
                    cleanup_verified = killed.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_verified = False
            else:
                process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            cleanup_verified = False
        raise
    finally:
        for reader in readers:
            reader.join(timeout=2)
        while True:
            try:
                stream_name, line = output_queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                continue
            (output if stream_name == "stdout" else errors).append(line)
            transcript = stdout_transcript if stream_name == "stdout" else stderr_transcript
            if transcript is not None:
                transcript.write(redact_text(line, redact_values))
                transcript.flush()
        for transcript in (stdout_transcript, stderr_transcript):
            if transcript is not None:
                transcript.flush()
                transcript.close()
    return "".join(output), "".join(errors), process.returncode or 0, timeout_reason, cleanup_verified


def fixture_files(directory: Path) -> dict[str, bytes]:
    files = {}
    for path in directory.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or ".pytest_cache" in path.parts:
            continue
        relative = path.relative_to(directory).as_posix()
        if relative not in {"claude.jsonl", "claude.stderr", "initial.claude.jsonl",
                            "initial.claude.stderr", "result.json"}:
            files[relative] = path.read_bytes()
    return files


def make_billing_dossiers(directory: Path) -> None:
    """Generate long, deterministic audit evidence in the disposable workspace."""
    findings = (
        ("REPLAY-41", "same normalized event_id appears twice; balance changes once"),
        ("CONFLICT-52", "same event_id changes amount_cents; reject the export"),
        ("ACCOUNT-63", "refund account_id differs from its charge; reject the export"),
        ("OVERREFUND-74", "cumulative refunds exceed the charge amount; reject the export"),
    )
    for dossier, (code, finding) in enumerate(findings, 1):
        lines = [f"# Payment audit dossier {dossier}; chronological export evidence"]
        for index in range(240):
            digest = hashlib.blake2b(f"{dossier}:{index}".encode(), digest_size=16).hexdigest()
            lines.append(
                f"{index:04d}|batch=2026Q4-{dossier}|event_id=evt_{dossier}_{index:04d}_{digest[:8]}|"
                f"account_id=acct_{index % 23:02d}|kind=charge|amount_cents={101 + index % 997}|"
                f"charge_id=|trace={digest}|source=processor_export|validation=pending|"
                f"received_utc=2026-10-15T12:{index % 60:02d}:{index % 60:02d}Z"
            )
            if index == 120:
                lines.append(f"INCIDENT {code}: {finding}")
        (directory / f"audit_dossier_{dossier}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def holdout_tests(name: str, directory: Path) -> subprocess.CompletedProcess[str] | None:
    hidden = FIXTURES / "holdouts" / ("billing_ledger" if name == "billing_ledger_stress" else name)
    if not hidden.is_dir():
        return None
    with tempfile.TemporaryDirectory(prefix=f"qwen-{name}-holdout-") as temporary:
        isolated = Path(temporary)
        shutil.copytree(directory, isolated, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("claude.jsonl", "claude.stderr", "result.json", "__pycache__"))
        for visible_test in isolated.rglob("test_*.py"):
            visible_test.unlink()
        shutil.copytree(hidden, isolated, dirs_exist_ok=True)
        return unit_tests(isolated)


def run_task(name: str, directory: Path, key: str, *, verify_in_cli: bool = False,
             timeout: int = 240, max_turns: int | None = None,
             auto_compact_percent: int = QWEN_AUTO_COMPACT_PERCENT,
             client_context_tokens: int = QWEN_CLIENT_CONTEXT_TOKENS,
             dispatch_mode: str = DIRECT_MODE) -> dict:
    source_names, prompt = TASKS[name]
    source_names = (source_names,) if isinstance(source_names, str) else source_names
    if dispatch_mode == CHILD_MODE:
        timeout = min(timeout, CHILD_TIMEOUT)
        max_turns = min(max_turns or CHILD_TURN_CAP, CHILD_TURN_CAP)
    marker = f"{CHILD_MARKER_PREFIX}{uuid.uuid4().hex[:12]}" if dispatch_mode == CHILD_MODE else None
    if verify_in_cli:
        prompt += (
            " First run `python -m unittest -q` in this workspace with PowerShell to see the failing tests. "
            "After editing, run that command again. Report the actual test result."
        )
    before = {source_name: (directory / source_name).read_bytes() for source_name in source_names}
    visible_tests_before = {str(path.relative_to(directory)): path.read_bytes()
                            for path in directory.rglob("test_*.py")}
    baseline = unit_tests(directory)
    assert baseline.returncode != 0, f"{name} fixture already passes"
    fixture_before = fixture_files(directory)
    start_utc = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    process_tree_cleanup_verified = None
    try:
        command = claude_command(
            allow_shell=verify_in_cli, max_turns=max_turns,
            dispatch_mode=dispatch_mode,
            agents=child_agent_spec(name, directory, marker) if marker else None,
        )
        task_input = coordinator_prompt(name, directory, marker) if marker else prompt
        env = cli_env(key, auto_compact_percent=auto_compact_percent,
                      client_context_tokens=client_context_tokens)
        if dispatch_mode == CHILD_MODE:
            stdout, stderr, returncode, stop_reason, process_tree_cleanup_verified = run_cli_bounded(
                command, task_input=task_input, cwd=directory, env=env, timeout=timeout,
            )
            if stop_reason:
                stderr += f"\n{stop_reason}"
                returncode = 124
        else:
            result = subprocess.run(
                command, input=task_input, cwd=directory, env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
            stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else error.stdout or ""
        stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else error.stderr or ""
        stderr += f"\nClaude Code exceeded the {timeout}-second task limit"
        returncode = 124
    initial_attempt = None
    final_repair_test_result = None
    if dispatch_mode == CHILD_MODE and (
        returncode == 0 or (returncode == 124 and process_tree_cleanup_verified is True)
    ):
        initial_events = []
        for line in stdout.splitlines():
            try:
                initial_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        initial_final = next((event for event in reversed(initial_events)
                              if event.get("type") == "result"), {})
        initial_tests = unit_tests(directory)
        final_repair_test_result = initial_tests
        if initial_tests.returncode != 0:
            initial_fixture_after = fixture_files(directory)
            initial_extra_files = sorted(initial_fixture_after.keys() - fixture_before.keys())
            initial_unauthorized_changes = sorted(
                name for name, contents in fixture_before.items()
                if name not in source_names and initial_fixture_after.get(name) != contents
            )
            initial_changed_files = [name for name, contents in before.items()
                                     if (directory / name).read_bytes() != contents]
            initial_tests_unchanged = all(
                (directory / name).read_bytes() == contents
                for name, contents in visible_tests_before.items()
            )
            initial_failure_output = (initial_tests.stdout + initial_tests.stderr)[-5000:]
            initial_stopped_cleanly = (
                returncode == 124 and process_tree_cleanup_verified is True
            )
            initial_attempt = {
                "marker": marker,
                "cli_exit": returncode,
                "cli_subtype": initial_final.get("subtype"),
                "child_marker_returned": child_returned_successfully(initial_events, marker),
                "changed_files": initial_changed_files,
                "extra_files": initial_extra_files,
                "unauthorized_changes": initial_unauthorized_changes,
                "visible_tests_unchanged": initial_tests_unchanged,
                "tests_passed": False,
                "test_output": initial_failure_output,
                "repair_attempted": False,
            }
            clean_initial_failure = (
                (initial_stopped_cleanly or (
                    initial_final.get("subtype") == "success"
                    and not initial_final.get("is_error")
                ))
                and bool(initial_changed_files)
                and set(initial_changed_files).issubset(set(source_names))
                and not initial_extra_files
                and not initial_unauthorized_changes
                and initial_tests_unchanged
            )
            if clean_initial_failure:
                repair_marker = f"{CHILD_MARKER_PREFIX}{uuid.uuid4().hex[:12]}"
                initial_attempt["repair_attempted"] = True
                repair_started = time.perf_counter()
                try:
                    repair_command = claude_command(
                        allow_shell=False, max_turns=max_turns, dispatch_mode=CHILD_MODE,
                        agents=child_agent_spec(name, directory, repair_marker,
                                                repair_feedback=initial_failure_output),
                    )
                    repair_stdout, repair_stderr, repair_returncode, repair_stop_reason, repair_cleanup = run_cli_bounded(
                        repair_command,
                        task_input=coordinator_repair_prompt(name, directory, repair_marker,
                                                             initial_failure_output),
                        cwd=directory, env=env, timeout=timeout,
                    )
                    (directory / "initial.claude.jsonl").write_text(
                        stdout.replace(key, "[REDACTED]"), encoding="utf-8")
                    (directory / "initial.claude.stderr").write_text(
                        stderr.replace(key, "[REDACTED]"), encoding="utf-8")
                    if repair_stop_reason:
                        repair_stderr += f"\n{repair_stop_reason}"
                        repair_returncode = 124
                    initial_attempt["repair_seconds"] = round(time.perf_counter() - repair_started, 2)
                    initial_attempt["repair_cli_exit"] = repair_returncode
                    initial_attempt["repair_process_tree_cleanup_verified"] = repair_cleanup
                    initial_attempt["repair_marker"] = repair_marker
                    process_tree_cleanup_verified = repair_cleanup
                    stdout, stderr, returncode = repair_stdout, repair_stderr, repair_returncode
                    marker = repair_marker
                    final_repair_test_result = unit_tests(directory)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    initial_attempt["repair_seconds"] = round(time.perf_counter() - repair_started, 2)
                    initial_attempt["repair_error"] = str(error)
                    stderr += f"\nAutomatic repair could not be started: {error}"
                    returncode = 125
    elapsed = round(time.perf_counter() - start, 2)
    end_utc = datetime.now(timezone.utc).isoformat()
    (directory / "claude.jsonl").write_text(stdout.replace(key, "[REDACTED]"), encoding="utf-8")
    (directory / "claude.stderr").write_text(stderr.replace(key, "[REDACTED]"), encoding="utf-8")
    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    final = next((event for event in reversed(events) if event.get("type") == "result"), {})
    ttft_ms = [event["ttft_ms"] for event in events
               if event.get("type") == "stream_event"
               and event.get("event", {}).get("type") == "message_start"
               and isinstance(event.get("ttft_ms"), (int, float))]
    logs = subprocess.run(
        ["docker", "logs", "--since", start_utc, "--until", end_utc, "subroute-qwen-local-1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    timings = [(float(ms), int(tokens)) for ms, tokens in re.findall(
        r"(?<!prompt )eval time\s*=\s*([\d.]+) ms /\s*(\d+) tokens", logs.stdout + logs.stderr
    )]
    gateway_logs = subprocess.run(
        ["docker", "logs", "--since", start_utc, "--until", end_utc, "subroute-gateway-staging-1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    audits = [{"protocol": protocol, "compacted": compacted == "true", "input_tokens": int(raw),
               "forwarded_tokens": int(forwarded), "target": int(target), "output_limit": int(output_limit)}
              for protocol, compacted, raw, forwarded, target, output_limit in re.findall(
                  r"sift_request protocol=(\w+) compacted=(true|false) [^\r\n]*?input_tokens=(\d+) forwarded_tokens=(\d+) target=(\d+) output_limit=(-?\d+)",
                  gateway_logs.stdout + gateway_logs.stderr) if int(target) in {24_576, QWEN_INPUT_TARGET}]
    qwen_route_calls = len(re.findall(r"completion\(\) model=\s*qwen3\.8-27b", gateway_logs.stdout + gateway_logs.stderr))
    qwen_sift_requests = sum(audit["protocol"] == "anthropic_messages" and audit["target"] == QWEN_INPUT_TARGET
                             for audit in audits)
    calls = [block for event in events if event.get("type") == "assistant"
             for block in event.get("message", {}).get("content", [])
             if isinstance(block, dict) and block.get("type") == "tool_use"]
    tools = [block.get("name") for block in calls]
    dossier_reads = sorted({number for block in calls if block.get("name") == "Read"
                            for number in range(1, 5)
                            if f"audit_dossier_{number}.txt" in str((block.get("input") or {}).get("file_path", ""))})
    client_snapshots = sum(
        "This session is being continued" in str(event.get("message", {}).get("content", ""))
        for event in events if event.get("type") == "user"
    )
    slot_tokens = [int(value) for value in re.findall(r"stop processing: n_tokens = (\d+)", logs.stdout + logs.stderr)]
    shell_commands = [block.get("input") for block in calls if block.get("name") in {"PowerShell", "Bash"}]
    edit_index = next((index for index, block in enumerate(calls) if block.get("name") == "Edit"), None)
    call_indices = {block.get("id"): index for index, block in enumerate(calls)}
    test_runs = []
    for event in events:
        if event.get("type") != "user":
            continue
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            index = call_indices.get(block.get("tool_use_id"))
            if index is None:
                continue
            call = calls[index]
            if call.get("name") not in {"PowerShell", "Bash"}:
                continue
            command = str((call.get("input") or {}).get("command", ""))
            if "python -m unittest" not in command:
                continue
            tool_result = event.get("tool_use_result")
            output = "\n".join(str(tool_result.get(key, "")) for key in ("stdout", "stderr")) \
                if isinstance(tool_result, dict) else str(block.get("content", ""))
            passed = bool(re.search(r"Ran \d+ tests?", output) and re.search(r"(?m)^OK\s*$", output))
            test_runs.append({"phase": "after_edit" if edit_index is not None and index > edit_index else "before_edit",
                              "passed": passed, "failed": "FAILED" in output})
    cli_test_loop = any(item["phase"] == "before_edit" and item["failed"] for item in test_runs) and any(
        item["phase"] == "after_edit" and item["passed"] for item in test_runs
    )
    after_tests = final_repair_test_result or unit_tests(directory)
    fixture_after = fixture_files(directory)
    extra_files = sorted(fixture_after.keys() - fixture_before.keys())
    unauthorized_changes = sorted(
        name for name, contents in fixture_before.items()
        if name not in source_names and fixture_after.get(name) != contents
    )
    changed_files = [name for name, contents in before.items() if (directory / name).read_bytes() != contents]
    visible_tests_unchanged = all((directory / name).read_bytes() == contents
                                  for name, contents in visible_tests_before.items())
    holdout = holdout_tests(name, directory)
    child_acceptance = None
    if marker:
        child_acceptance = qwen_child_checks(
            name, directory, events, marker,
            final_success=returncode == 0 and final.get("subtype") == "success" and not final.get("is_error"),
            changed_files=changed_files, extra_files=extra_files,
            unauthorized_changes=unauthorized_changes,
            tests_passed=after_tests.returncode == 0, qwen_route_calls=qwen_route_calls,
            qwen_sift_requests=qwen_sift_requests,
        )
    outcome = {
        "task": name, "seconds": elapsed, "cli_exit": returncode,
        "process_tree_cleanup_verified": process_tree_cleanup_verified,
        "dispatch_mode": dispatch_mode, "child_acceptance": child_acceptance,
        "initial_attempt": initial_attempt,
        "qwen_route_calls": qwen_route_calls, "qwen_sift_requests": qwen_sift_requests,
        "start_utc": start_utc, "end_utc": end_utc,
        "sift_requests": audits, "sift_compactions": [audit for audit in audits if audit["compacted"]],
        "cli_subtype": final.get("subtype"), "cli_error": final.get("is_error"),
        "tools": tools, "shell_commands": shell_commands, "cli_test_runs": test_runs,
        "dossier_reads": dossier_reads, "client_context_snapshots": client_snapshots,
        "max_server_slot_tokens": max(slot_tokens, default=None),
        "largest_server_slot_drop": max((left - right for left, right in zip(slot_tokens, slot_tokens[1:])), default=0),
        "cli_test_loop": cli_test_loop, "source_changed": bool(changed_files),
        "changed_files": changed_files, "visible_tests_unchanged": visible_tests_unchanged,
        "extra_files": extra_files, "unauthorized_changes": unauthorized_changes,
        "write_scope_clean": not extra_files and not unauthorized_changes,
        "baseline_failed": baseline.returncode != 0,
        "tests_passed": after_tests.returncode == 0,
        "test_output": (after_tests.stdout + after_tests.stderr)[-1500:],
        "holdout_passed": holdout.returncode == 0 if holdout else None,
        "holdout_output": (holdout.stdout + holdout.stderr)[-1500:] if holdout else None,
        "final_answer": final.get("result", "")[:1200],
        "stderr_tail": stderr[-1200:].replace(key, "[REDACTED]"),
        "usage": final.get("usage"),
        "ttft_ms": ttft_ms,
        "mean_ttft_ms": round(sum(ttft_ms) / len(ttft_ms), 1) if ttft_ms else None,
        "server_eval": [{"ms": ms, "tokens": tokens} for ms, tokens in timings],
        "mean_server_tokens_per_second": round(sum(tokens for ms, tokens in timings) /
                                               (sum(ms for ms, tokens in timings) / 1000), 2) if timings else None,
        "server_response_count": len(timings),
    }
    if name == "incident_reconstruction":
        reads = []
        for event in events:
            if event.get("type") == "assistant" and any(
                isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Edit"
                for block in event.get("message", {}).get("content", [])
            ):
                break
            if event.get("type") != "user":
                continue
            tool_result = event.get("tool_use_result")
            file = tool_result.get("file", {}) if isinstance(tool_result, dict) else {}
            if isinstance(file, dict) and isinstance(file.get("content"), str):
                blocks = event.get("message", {}).get("content", [])
                delivered = "\n".join(
                    block.get("content", "") for block in blocks
                    if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str)
                )
                reads.append((Path(file.get("filePath", "")).name, delivered))
        outcome["complete_incident_intake"] = complete_incident_intake(directory, reads)
    serialized = json.dumps(outcome, ensure_ascii=False, indent=2).replace(key, "[REDACTED]")
    (directory / "result.json").write_text(serialized, encoding="utf-8")
    return json.loads(serialized)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=[*TASKS, "all", "extended", "domains"], default="all")
    parser.add_argument("--execution-mode", choices=[DIRECT_MODE, CHILD_MODE], default=DIRECT_MODE,
                        help="run Qwen directly or use a named Qwen child under a coordinator")
    parser.add_argument("--verify-in-cli", action="store_true", help="let Claude Code run the fixture tests with PowerShell")
    parser.add_argument("--timeout", type=int, default=300, help="wall-clock limit per task in seconds")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--auto-compact-percent", type=int, default=QWEN_AUTO_COMPACT_PERCENT)
    parser.add_argument("--client-context-tokens", type=int, default=QWEN_CLIENT_CONTEXT_TOKENS)
    args = parser.parse_args()
    if not 1 <= args.auto_compact_percent <= 100:
        parser.error("--auto-compact-percent must be between 1 and 100")
    if not QWEN_INPUT_TARGET <= args.client_context_tokens <= QWEN_RAW_INPUT_LIMIT:
        parser.error("--client-context-tokens must be between32768and1000000")
    if args.execution_mode == CHILD_MODE and args.verify_in_cli:
        parser.error("--verify-in-cli is unavailable in qwen-child mode; the runner checks tests after the child returns")
    if args.execution_mode == CHILD_MODE:
        args.timeout = min(args.timeout, CHILD_TIMEOUT)
        args.max_turns = min(args.max_turns or CHILD_TURN_CAP, CHILD_TURN_CAP)
    groups = {"all": ORIGINAL_TASKS, "extended": EXTENDED_TASKS, "domains": DOMAIN_TASKS}
    selected = list(groups[args.task]) if args.task in groups else [args.task]
    run_root = FIXTURES / ".runs"
    run_root.mkdir(exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="subroute-qwen-claude-", dir=run_root))
    for name in selected:
        source = "billing_ledger" if name == "billing_ledger_stress" else name
        shutil.copytree(FIXTURES / source, run_dir / name)
        if name == "billing_ledger_stress":
            make_billing_dossiers(run_dir / name)
        elif name == "incident_reconstruction":
            make_incident_evidence(run_dir / name)
    key = gateway_key()
    headers = {"Authorization": "Bearer " + key}
    outcomes = []
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30) as client:
        client.get("/health/readiness").raise_for_status()
        before = client.get("/api/active-model").json()
        try:
            set_policy(client, before, test=True)
            for name in selected:
                outcome = run_task(name, run_dir / name, key, verify_in_cli=args.verify_in_cli,
                                   timeout=args.timeout, max_turns=args.max_turns,
                                   auto_compact_percent=args.auto_compact_percent,
                                   client_context_tokens=args.client_context_tokens,
                                   dispatch_mode=args.execution_mode)
                outcomes.append(outcome)
                print(json.dumps({key: outcome[key] for key in (
                    "task", "seconds", "cli_exit", "cli_subtype", "source_changed", "tests_passed",
                    "holdout_passed", "mean_ttft_ms", "mean_server_tokens_per_second", "server_response_count"
                )}), flush=True)
        finally:
            set_policy(client, before, test=False)
            after = client.get("/api/active-model").json()
            keys = ("active_model", "mode", "reasoning_effort", "advisor_model",
                    "advisor_reasoning_effort", "advisor_trigger", "advisor_sensitivity",
                    "advisor_classifier", "fast_mode")
            assert all(after[key] == before[key] for key in keys), "staging policy was not restored"
    summary = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "model": MODEL, "claude_code": subprocess.run([claude_command()[0], "--version"],
            capture_output=True, text=True, timeout=10).stdout.strip(),
        "execution_mode": args.execution_mode,
        "timeout_seconds": args.timeout,
        "parent_max_turns": args.max_turns,
        "child_max_turns": CHILD_TURN_CAP if args.execution_mode == CHILD_MODE else None,
        "original_policy_restored": True, "verify_in_cli": args.verify_in_cli,
        "auto_compact_percent": args.auto_compact_percent, "outcomes": outcomes,
        "client_context_tokens": args.client_context_tokens,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("evidence", run_dir, flush=True)
    return 0 if all(item["cli_exit"] == 0 and item["cli_subtype"] == "success"
                    and item["source_changed"] and item["tests_passed"]
                    and item["visible_tests_unchanged"] and item["write_scope_clean"]
                    and item["holdout_passed"] is not False
                    and item.get("complete_incident_intake", True)
                    and "Read" in item["tools"] and "Edit" in item["tools"]
                    and (args.execution_mode != CHILD_MODE or bool(item.get("child_acceptance", {}).get("passed")))
                    and (not args.verify_in_cli or item["cli_test_loop"]) for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
