"""Opt-in, real Claude Code acceptance against the local Subroute gateway."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

import pytest


pytestmark = pytest.mark.live


HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "qwen_claude"
RUNS = HERE / ".runs"
MIN_SECONDS = 180
MIN_TURNS = 20
MAX_SECONDS = 900
IDLE_SECONDS = 150


def _claude_executable() -> str:
    native = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    if native.is_file():
        return str(native)
    return shutil.which("claude.exe") or ""

CASES = {
    "build_planner": {
        "model": "default",
        "sources": ("build_manifest.py", "incremental.py"),
        "prompt": "Implement the incremental build planning contract in README.md. Read the source and tests, run the visible tests, edit only build_manifest.py and incremental.py, then run the tests again.",
        "reviews": ("duplicate keys and malformed paths", "deterministic topological order", "current dependency fingerprints", "canonical hash encoding", "unchanged rebuild plans"),
    },
    "billing_ledger": {
        "model": "claude-sonnet-4-6",
        "sources": ("csv_ingest.py", "ledger.py"),
        "prompt": "Implement the payment CSV and reconciliation contract in README.md. Read source and tests, run the visible tests, edit only the two source files, then run the tests again.",
        "reviews": ("CSV quoting and BOM", "strict amount parsing", "replay versus conflicting duplicate", "refund account matching", "aggregate refund limit"),
    },
    "sse_relay": {
        "model": "subroute-acceptance-unknown-model",
        "sources": ("sse_parser.py", "relay.py"),
        "prompt": "Implement the SSE streaming relay contract in README.md. Read source and tests, run the visible tests, edit only the two source files, then run the tests again.",
        "reviews": ("split UTF-8 and CRLF", "multiline data", "oversize event bounds", "error and terminal events", "upstream cleanup on generator close"),
    },
}


def _events(path: Path) -> list[dict]:
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            result.append(event)
    return result


def _check_unknown_model_fallback(key: str, model: str) -> dict:
    base = "http://127.0.0.1:4000"

    def get(path: str) -> dict:
        req = Request(base + path, headers={"Authorization": "Bearer " + key})
        with urlopen(req, timeout=15) as response:
            return json.load(response)

    policy = get("/api/active-model")
    saved_default = policy["active_model"]
    catalog = get("/model/info")["data"]
    default_ids = {item.get("model_info", {}).get("id") for item in catalog
                   if item.get("model_name") == saved_default}
    body = {"model": model, "max_tokens": 512,
            "messages": [{"role": "user", "content": "Reply exactly FALLBACK_OK."}]}
    req = Request(base + "/v1/messages", data=json.dumps(body).encode("utf-8"),
                  headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                           "content-type": "application/json"}, method="POST")
    with urlopen(req, timeout=90) as response:
        result = json.load(response)
        deployment_id = response.headers.get("x-litellm-model-id")
        status = response.status
    answer = "".join(block.get("text", "") for block in result.get("content", [])
                     if block.get("type") == "text")
    checks = {"http_200": status == 200,
              "terminal": result.get("stop_reason") == "end_turn",
              "marker": "FALLBACK_OK" in answer,
              "requested_identity": result.get("model") == model,
              "resolved_saved_default": bool(deployment_id) and deployment_id in default_ids}
    return {"saved_default": saved_default, "policy_mode": policy.get("mode"),
            "checks": checks, "passed": all(checks.values())}


def _invoke(work: Path, audit: Path, config: Path, model: str, prompt: str,
            index: int, key: str, timeout: float) -> dict:
    command = [_claude_executable(), "-p", "--bare", "--setting-sources", "",
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--disable-slash-commands", "--tools", "Read,Edit,PowerShell",
               "--allowedTools", "Read,Edit,PowerShell", "--permission-mode", "dontAsk",
               "--model", model, "--max-turns", "35", "--output-format", "stream-json",
               "--include-partial-messages", "--verbose", "--settings",
               '{"alwaysThinkingEnabled":true}',
               "--system-prompt", "Complete the coding task using the permitted file tools. "
               "Keep each response concise. Edit only the named source files. "
               "Work only inside the current directory; do not inspect parent directories "
               "or acceptance logs. Use bounded checks on supplied examples; do not run "
               "broad scans, disassemble Python bytecode, or create scratch files. Run the supplied tests "
               "and report actual results."]
    command += ["--no-session-persistence", "--session-id", str(uuid.uuid4()), prompt]
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("ANTHROPIC_", "CLAUDE_")) and name != "GATEWAY_MASTER_KEY"}
    env.update(ANTHROPIC_BASE_URL="http://127.0.0.1:4000",
               ANTHROPIC_API_KEY=key, ANTHROPIC_MODEL=model,
               ANTHROPIC_CUSTOM_HEADERS="x-litellm-stream-timeout: 60",
               ANTHROPIC_DEFAULT_OPUS_MODEL=model,
               ANTHROPIC_DEFAULT_SONNET_MODEL=model,
               ANTHROPIC_DEFAULT_HAIKU_MODEL=model,
               CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_1M_CONTEXT="1",
               CLAUDE_CODE_MAX_CONTEXT_TOKENS="256000",
               MAX_THINKING_TOKENS="1024")
    started = time.monotonic()
    last_output = [started]
    output_path = audit / f"claude-{index:02}.jsonl"
    error_path = audit / f"claude-{index:02}.stderr"
    stop_reason = None
    with output_path.open("w", encoding="utf-8") as output, error_path.open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=work, env=env, text=True,
                                   encoding="utf-8", errors="replace", bufsize=1)

        def copy_lines(source, target) -> None:
            for line in source:
                target.write(line.replace(key, "[REDACTED]"))
                target.flush()
                last_output[0] = time.monotonic()

        workers = [threading.Thread(target=copy_lines, args=(process.stdout, output), daemon=True),
                   threading.Thread(target=copy_lines, args=(process.stderr, errors), daemon=True)]
        for worker in workers:
            worker.start()
        while process.poll() is None:
            now = time.monotonic()
            if now - started > timeout or now - last_output[0] > IDLE_SECONDS:
                stop_reason = "call_timeout" if now - started > timeout else "idle_timeout"
                process.kill()
                break
            time.sleep(2)
        process.wait()
        for worker in workers:
            worker.join(timeout=5)
        exit_code = 124 if stop_reason else process.returncode
        if stop_reason:
            errors.write(f"\nacceptance runner: {stop_reason}\n")
    events = _events(output_path)
    final = next((event for event in reversed(events) if event.get("type") == "result"), {})
    init = next((event for event in events if event.get("type") == "system"
                 and event.get("subtype") == "init"), {})
    assistant = [event for event in events if event.get("type") == "assistant"]
    tool_use = sum(block.get("type") == "tool_use" for event in assistant
                   for block in event.get("message", {}).get("content", [])
                   if isinstance(block, dict))
    result_blocks = [block for event in events if event.get("type") == "user"
                     for block in event.get("message", {}).get("content", [])
                     if isinstance(block, dict) and block.get("type") == "tool_result"]
    tool_result = len(result_blocks)
    tool_errors = sum(bool(block.get("is_error")) for block in result_blocks)
    unavailable_tools = sum("No such tool available" in str(block.get("content", ""))
                            for block in result_blocks)
    last_stream = next((event.get("event", {}).get("type") for event in reversed(events)
                        if event.get("type") == "stream_event"
                        and isinstance(event.get("event"), dict)), None)
    return {"seconds": round(time.monotonic() - started, 2), "exit_code": exit_code,
            "effective_model": init.get("model"), "tool_use": tool_use,
            "tool_result": tool_result, "tool_errors": tool_errors,
            "unavailable_tool_attempts": unavailable_tools,
            "last_event_type": events[-1].get("type") if events else None,
            "last_stream_event_type": last_stream,
            "api_retries": sum(event.get("type") == "system" and event.get("subtype") == "api_retry"
                               for event in events),
            "subtype": final.get("subtype"), "terminal_reason": final.get("terminal_reason"),
            "api_error_status": final.get("api_error_status"), "is_error": final.get("is_error"),
            "stop_reason": stop_reason,
            "num_turns": final.get("num_turns"),
            "assistant_events": len(assistant), "errors": [event.get("error") for event in events
            if event.get("type") == "error"], "stderr_file": f"claude-{index:02}.stderr"}


def _run_case(name: str) -> None:
    if os.getenv("RUN_SUBROUTE_CLAUDE_ACCEPTANCE") != "1":
        pytest.skip("set RUN_SUBROUTE_CLAUDE_ACCEPTANCE=1 for live CLI acceptance")
    key = os.getenv("SUBROUTE_ACCEPTANCE_API_KEY", "")
    if not key:
        pytest.fail("SUBROUTE_ACCEPTANCE_API_KEY must be a gateway inference key, never the master key")
    if not _claude_executable():
        pytest.fail("Claude Code CLI is missing")
    case = CASES[name]
    RUNS.mkdir(exist_ok=True)
    run_dir = RUNS / f"{name}-{uuid.uuid4().hex[:10]}"
    run_dir.mkdir()
    work = run_dir / "workspace"
    shutil.copytree(FIXTURES / name, work)
    audit = run_dir / "audit"
    audit.mkdir()
    config = run_dir / "config"
    config.mkdir()
    before = {source: (work / source).read_bytes() for source in case["sources"]}
    fixture_before = {p.name: p.read_bytes() for p in work.iterdir() if p.is_file()}
    baseline = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=work,
                              capture_output=True, text=True, timeout=30)
    assert baseline.returncode != 0, "fixture unexpectedly passes before Claude works"
    fallback = (_check_unknown_model_fallback(key, case["model"])
                if name == "sse_relay" else None)
    if fallback is not None:
        (run_dir / "fallback.json").write_text(json.dumps(fallback, indent=2), encoding="utf-8")
        assert fallback["passed"], fallback
    started = time.monotonic()
    attempts = []
    for index in range(10):
        remaining = MAX_SECONDS - (time.monotonic() - started)
        if remaining <= 5:
            break
        if index == 0:
            prompt = case["prompt"]
        else:
            concern = case["reviews"][(index - 1) % len(case["reviews"])]
            sources = ", ".join(case["sources"])
            prompt = (f"Review the current implementation of {sources} for {concern}. "
                      "Use README.md and the visible tests in this directory, with at most "
                      "three focused examples. Fix a concrete defect in those source files "
                      "if found, run the supplied tests once, and report exactly what you "
                      "checked. Do not edit tests or create other files. Complete promptly.")
        attempt = _invoke(work, audit, config, case["model"], prompt, index, key,
                          timeout=min(450, remaining))
        attempts.append(attempt)
        elapsed = time.monotonic() - started
        turns = sum(item["num_turns"] for item in attempts if isinstance(item["num_turns"], int))
        if attempt["exit_code"] or attempt["is_error"] or attempt["subtype"] != "success":
            break
        if elapsed >= MIN_SECONDS and turns >= MIN_TURNS:
            break
        if elapsed >= MAX_SECONDS:
            break
    after = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=work,
                           capture_output=True, text=True, timeout=30)
    elapsed = round(time.monotonic() - started, 2)
    turns = sum(item["num_turns"] for item in attempts if isinstance(item["num_turns"], int))
    changed = [source for source, old in before.items()
               if not (work / source).is_file() or (work / source).read_bytes() != old]
    sources_present = all((work / source).is_file() for source in case["sources"])
    other_files_unchanged = all((work / filename).is_file()
                                and (work / filename).read_bytes() == old
                                for filename, old in fixture_before.items()
                                if filename not in case["sources"])
    unexpected_files = [p.relative_to(work).as_posix() for p in work.rglob("*")
                        if p.is_file() and p.relative_to(work).parts[0]
                        not in {"__pycache__", ".pytest_cache"}
                        and p.relative_to(work).as_posix() not in fixture_before]
    receipt = {"case": name, "model_requested": case["model"], "gateway": "http://127.0.0.1:4000",
               "fallback_preflight": fallback,
               "elapsed_seconds": elapsed, "cli_turns": turns, "attempts": attempts,
               "source_changed": changed, "other_files_unchanged": other_files_unchanged,
               "sources_present": sources_present,
               "unexpected_files": unexpected_files,
               "unit_tests_passed": after.returncode == 0,
               "unit_test_output": (after.stdout + after.stderr)[-3000:]}
    (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    assert all(item["exit_code"] == 0 and item["subtype"] == "success"
               and item["terminal_reason"] == "completed" and not item["is_error"]
               for item in attempts), receipt
    assert elapsed >= MIN_SECONDS, receipt
    assert turns >= MIN_TURNS, receipt
    assert all(item["effective_model"] == case["model"] for item in attempts), receipt
    assert sum(item["tool_use"] for item in attempts) > 0, receipt
    assert sum(item["tool_result"] - item["tool_errors"] for item in attempts) > 0, receipt
    assert changed and sources_present and other_files_unchanged and not unexpected_files \
        and after.returncode == 0, receipt


def test_default_model_build_planner_coding() -> None:
    _run_case("build_planner")


def test_claude_model_card_billing_coding() -> None:
    _run_case("billing_ledger")


def test_unknown_model_falls_back_sse_coding() -> None:
    _run_case("sse_relay")
