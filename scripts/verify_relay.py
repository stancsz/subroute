"""Isolated Docker/MCP/two-runner acceptance. Default mode is fixture-only; --live uses bounded real sessions.

Run with this checkout's Python environment after building subroute-relay:0.1.0.
Creates and removes only its uniquely named Compose project and volume.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from subroute.relay import runner
from subroute.relay.process import WindowsJob, claude_command

LIVE_DEADLINE_SECONDS = 420
LIVE_TASK_TIMEOUT_SECONDS = 180
LIVE_MAX_TURNS = 12
LIVE_MAX_COST_USD = 1


def _safe_receipt(task):
    try:
        receipt = json.loads(task.get("result") or "{}")
    except (TypeError, json.JSONDecodeError):
        receipt = {}
    return {"task_id": task.get("id"), "kind": task.get("kind"), "status": task.get("status"),
            "outcome": task.get("outcome"), "session_id": receipt.get("session_id"),
            "usage": receipt.get("usage"), "total_cost_usd_estimate": receipt.get("total_cost_usd"),
            "num_turns": receipt.get("num_turns")}


def _require_live_receipt(task):
    if task.get("outcome") != "success":
        raise RuntimeError("Live task lacks successful terminal outcome")
    try:
        receipt = json.loads(task.get("result") or "{}")
    except (TypeError, json.JSONDecodeError):
        receipt = {}
    if not receipt.get("session_id") or not isinstance(receipt.get("usage"), dict):
        raise RuntimeError("Live task lacks actual Claude terminal session and usage receipt")
    return receipt


def _checked_test_output(output):
    match = re.search(r"Ran (\d+) tests?", output)
    if not match or int(match[1]) < 1 or not re.search(r"^OK\s*$", output, re.MULTILINE):
        raise RuntimeError("Live acceptance requires actually discovered, passing tests")
    return int(match[1])


def _test_trace(state, task):
    receipt = _require_live_receipt(task)
    session_id = str(UUID(receipt["session_id"]))
    paths = list((state / "claude/projects").rglob(session_id + ".jsonl"))
    commands = {}
    for path in paths:
        if path.stat().st_size > 5_000_000:
            raise RuntimeError("Session trace exceeds bounded inspection size")
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            content = event.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use" and part.get("name") == "Bash":
                    command = part.get("input", {}).get("command", "")
                    if "python" in command and "-m unittest" in command:
                        commands[part["id"]] = command
                if (part.get("type") == "tool_result" and part.get("tool_use_id") in commands
                        and not part.get("is_error", False)):
                    output = part.get("content", "")
                    if isinstance(output, list):
                        output = "\n".join(p.get("text", "") for p in output if isinstance(p, dict))
                    try:
                        count = _checked_test_output(output)
                    except RuntimeError:
                        continue
                    return {"session_id": session_id, "unittest_tool_result_verified": True, "test_count": count}
    raise RuntimeError("No successful actual unittest tool result in this session's trace")


def _parent_tests(workspace):
    safe_env = {k: v for k, v in os.environ.items() if k in {
        "PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "LANG", "LC_ALL"}}
    test = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=workspace, env=safe_env,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
    output = (test.stdout + test.stderr).decode("utf-8", errors="replace")
    if test.returncode:
        raise RuntimeError("Parent unittest verification failed")
    count = _checked_test_output(output)
    holdout = """from labels import normalize_labels
assert normalize_labels([]) == []
assert normalize_labels(['  Alpha ', 'ALPHA', '', ' beta', 'Beta ', 'Γ', 'γ', '  ']) == ['Alpha', 'beta', 'Γ']
assert normalize_labels([' a', 'B', 'a', ' C ', ' b']) == ['a', 'B', 'C']
print('holdout passed')
"""
    check = subprocess.run([sys.executable, "-c", holdout], cwd=workspace, env=safe_env,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    if check.returncode or b"holdout passed" not in check.stdout:
        raise RuntimeError("Independent semantic holdout failed")
    return {"workspace": workspace.name, "command": "python -m unittest -v", "returncode": 0,
            "test_count": count, "holdout_passed": True}


async def _stop_runners(processes, jobs):
    for job in jobs:
        job.close()
    for process in processes:
        if process.poll() is None:
            if os.name == "nt":
                await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                        capture_output=True, timeout=5)
            else:
                process.send_signal(signal.SIGINT)
        try:
            await asyncio.to_thread(process.wait, 12)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                rows = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, timeout=3)
                parents = [tuple(map(int, row.split())) for row in rows.stdout.splitlines() if len(row.split()) == 2]
                descendants = {process.pid}
                while True:
                    found = descendants | {pid for pid, ppid in parents if ppid in descendants}
                    if found == descendants:
                        break
                    descendants = found
                for pid in descendants - {process.pid}:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            process.kill()
            await asyncio.to_thread(process.wait, 3)


def failure_causes(exc):
    """Keep actionable causes when SDK task groups wrap a live-check failure."""
    if isinstance(exc, BaseExceptionGroup):
        return [cause for child in exc.exceptions for cause in failure_causes(child)]
    return [type(exc).__name__ + ": " + str(exc)]


async def verify_live(url, operator_key, machine_keys, scratch, compose, start_runner=None):
    processes, jobs, observed = [], [], {}
    try:
        async with asyncio.timeout(LIVE_DEADLINE_SECONDS):
            return await _verify_live(url, operator_key, machine_keys, scratch, compose, processes, jobs, observed,
                                      start_runner)
    except BaseException as exc:
        tool_history = {}
        for path in scratch.rglob("*.jsonl"):
            if "/claude/projects/" not in path.as_posix() or path.stat().st_size > 5_000_000:
                continue
            events = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                    content = event.get("message", {}).get("content", [])
                    for part in content if isinstance(content, list) else []:
                        if isinstance(part, dict) and part.get("type") == "tool_use":
                            file = Path(part.get("input", {}).get("file_path", ""))
                            events.append({"tool": part.get("name"), "file": "/".join(file.parts[-2:])})
                except (ValueError, TypeError, AttributeError):
                    continue
            tool_history[path.stem] = events[-80:]
        data = json.dumps({"mode": "live", "error": type(exc).__name__ + ": " + str(exc),
                           "errors": failure_causes(exc),
                           "observed_tasks": list(observed.values()), "tool_history": tool_history, "accepted": False}, ensure_ascii=False)
        for key in [operator_key, *machine_keys.values(), os.environ.get("GATEWAY_MASTER_KEY", "")]:
            if key:
                data = data.replace(key, "[redacted]")
        (scratch.parent / (scratch.name + "-failure.json")).write_text(data, encoding="utf-8")
        raise
    finally:
        await _stop_runners(processes, jobs)


async def _capture_live_tasks(call, task_ids, observed):
    for ident in task_ids:
        try:
            # Fleet snapshots deliberately truncate results. Preserve full task receipts.
            observed[ident] = await call("task_get", task_id=ident)
        except Exception:
            pass  # Keep any earlier full observation if the hub became unreachable.


async def _verify_live(url, operator_key, machine_keys, scratch, compose, processes, jobs, observed,
                       start_runner=None):
    inference_key = os.environ.get("GATEWAY_MASTER_KEY")
    if not inference_key:
        raise RuntimeError("GATEWAY_MASTER_KEY is required in the process environment")
    if len(machine_keys) != 2:
        raise RuntimeError("Live acceptance requires exactly two same-host machine identities")
    deadline = time.monotonic() + LIVE_DEADLINE_SECONDS
    headers = {"Authorization": "Bearer " + operator_key}
    task_ids, tests, accepted = [], [], []
    async with httpx.AsyncClient(headers=headers, timeout=10, trust_env=False) as transport:
        async with streamable_http_client(url + "/mcp", http_client=transport) as (receive, send, _):
            async with ClientSession(receive, send) as session:
                await session.initialize()
                async def call(name, **arguments):
                    result = await session.call_tool(name, arguments)
                    if result.isError:
                        raise RuntimeError("Live MCP operation failed: " + name)
                    return result.structuredContent or json.loads(result.content[0].text)

                goal = await call("goal_create", objective="Same-host live Subroute Relay acceptance with real Claude Code sessions",
                    acceptance="Two isolated Python modules pass unittest; separate real review sessions inspect and run tests; parent independently reruns tests; all four task receipts are successful",
                    authority="Only isolated scratch workspaces; create one tiny Python module and unittest file per workspace. Relay and configured LiteLLM inference transport are permitted. Agent tools must not make network requests. No publishing, global settings, or writes outside assigned workspace.", max_tasks=4)
                nodes, work_tasks, review_tasks, workspaces = list(machine_keys), [], [], []
                for node in nodes:
                    workspace = scratch / ("workspace-" + node)
                    workspace.mkdir()
                    (workspace / "GOAL.md").write_text("# Live Relay task\nImplement labels.py and test_labels.py. normalize_labels strips whitespace, casefolds for deduplication, preserves first spelling and order, and ignores empty labels. Run python -m unittest -v. Authority: these two files and local tests only. No publishing or other project work. The orchestrator owns separate review.\n", encoding="utf-8")
                    (workspace / "AGENTS.md").write_text("The active outcome is in the GOAL.md in this directory and in the assignment. This is an isolated validation workspace. Do not adopt the gateway repository's unrelated parent goals. The coordinator owns Northstar policy and separate review. Read the local goal, implement the two requested files, and run their tests. Review sessions inspect and test without repairing. At terminal handoff, leave a concise evidence checkpoint.\n", encoding="utf-8")
                    workspaces.append(workspace)
                    work = await call("task_create", goal_id=goal["id"], node=node, workspace="live", timeout_seconds=LIVE_TASK_TIMEOUT_SECONDS, max_turns=LIVE_MAX_TURNS, max_attempts=1,
                        instruction="Implement labels.py with normalize_labels(values): strip surrounding whitespace, casefold for deduplication, preserve first spelling and order, and ignore empty labels. Add test_labels.py with unittest cases for whitespace, case-insensitive duplicates, preserved spelling/order, and empty values. Run python -m unittest -v. Report files and actual test output. No other files.")
                    task_ids.append(work["id"])
                    work_tasks.append(work)
                    config = {"node": node, "url": url, "gateway_url": "http://127.0.0.1:4000", "gateway_key_env": "GATEWAY_MASTER_KEY",
                        "model": "default", "max_turns": LIVE_MAX_TURNS, "max_cost_usd": LIVE_MAX_COST_USD, "timeout_seconds": LIVE_TASK_TIMEOUT_SECONDS,
                        "tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash(python*)"],
                        "review_tools": ["Read", "Glob", "Grep", "Bash(python*)"],
                        "workspaces": {"live": str(workspace)}, "state_dir": str(scratch / (node + "-state")), "slots": 1}
                    (scratch / (node + ".json")).write_text(json.dumps(config), encoding="utf-8")
                for node, key in machine_keys.items():
                    runner_env = dict(os.environ, RELAY_NODE_KEY=key, GATEWAY_MASTER_KEY=inference_key)
                    runner_env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + runner_env.get("PATH", "")
                    runner_env.pop("CLAUDECODE", None)
                    command = [sys.executable, "-m", "subroute.relay.runner", "--config",
                               str(scratch / (node + ".json"))]
                    kwargs = {"cwd": Path(__file__).resolve().parents[1], "env": runner_env,
                              "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL,
                              "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                              "start_new_session": os.name != "nt"}
                    process = (start_runner or subprocess.Popen)(command, **kwargs)
                    processes.append(process)
                    if os.name == "nt":
                        jobs.append(WindowsJob(process.pid))

                async def wait_status(ident, wanted):
                    while time.monotonic() < deadline:
                        task = await call("task_get", task_id=ident)
                        observed[ident] = task
                        if task.get("status") in {"failed", "cancelled", "reconcile"}:
                            raise RuntimeError("Live task terminated: " + task.get("status", "unknown"))
                        if task.get("status") == wanted:
                            return task
                        if any(proc.poll() is not None for proc in processes):
                            raise RuntimeError("Live runner process exited")
                        await asyncio.sleep(1)
                    raise TimeoutError("Live acceptance exceeded bounded parent deadline")

                try:
                    receipts_by_id = {}
                    for index, work in enumerate(work_tasks):
                        task = await wait_status(work["id"], "review")
                        _require_live_receipt(task)
                        workspace = workspaces[index]
                        if not (workspace / "labels.py").is_file() or not (workspace / "test_labels.py").is_file():
                            raise RuntimeError("Live work did not produce both required test artifacts")
                        tests.append(await asyncio.to_thread(_parent_tests, workspace))
                        work_trace = _test_trace(scratch / (nodes[index] + "-state"), task)
                        review_spec = await call("task_create", goal_id=goal["id"], node=nodes[index], workspace="live",
                            kind="review", reviews_task=work["id"], timeout_seconds=LIVE_TASK_TIMEOUT_SECONDS,
                            max_turns=LIVE_MAX_TURNS, max_attempts=1,
                            instruction="Review labels.py and test_labels.py without changes. Inspect semantics (strip, casefold deduplication, preserve first spelling/order, ignore empty), and actually run python -m unittest -v. If all checks pass and no findings remain, include the exact marker RELAY_REVIEW_PASS. Otherwise report findings without that marker. Do not claim a pass after a denied test command.")
                        task_ids.append(review_spec["id"])
                        review_tasks.append(review_spec)
                        review = await wait_status(review_spec["id"], "review")
                        receipt = _require_live_receipt(review)
                        if "RELAY_REVIEW_PASS" not in receipt.get("result", ""):
                            raise RuntimeError("Independent reviewer did not return a passing verdict")
                        review_trace = _test_trace(scratch / (nodes[index] + "-state"), review)
                        tests.append({"work_trace": work_trace, "review_trace": review_trace})
                        await call("task_accept", task_id=review["id"], evidence="Separate session's actual successful unittest tool result verified in native trace; independent semantic holdout and parent unittest passed.")
                        await call("task_accept", task_id=work["id"], review_task_id=review["id"], evidence="Accepted after independent review acceptance and successful parent unittest rerun.")
                        receipts_by_id[work["id"]] = _safe_receipt(task)
                        receipts_by_id[review["id"]] = _safe_receipt(review)
                        accepted.extend((work["id"], review["id"]))
                    completed = await call("goal_set_state", goal_id=goal["id"], state="completed", evidence="Two same-host scratch workspaces passed parent-run unittest after independently accepted real Claude reviews; no cross-device or sustained claim.")
                    if completed.get("status") != "completed":
                        raise RuntimeError("Live goal did not reach completed state")
                except BaseException:
                    # Preserve every task's last observed state, including the other node,
                    # before pausing/cancelling and removing this disposable coordinator.
                    await _capture_live_tasks(call, task_ids, observed)
                    try:
                        await call("goal_set_state", goal_id=goal["id"], state="paused", evidence="Live acceptance stopped on failure; cleanup requested and task outcomes remain unaccepted.")
                    except Exception:
                        pass
                    for ident in task_ids:
                        try:
                            await call("task_cancel", task_id=ident)
                        except Exception:
                            pass
                    stop_deadline = time.monotonic() + 15
                    while time.monotonic() < stop_deadline:
                        try:
                            board = await call("fleet_status")
                            if not any(t["status"] in {"running", "cancelling"} for t in board["tasks"]):
                                break
                        except Exception:
                            break
                        await asyncio.sleep(1)
                    raise
    return {"mode": "live", "machine_identities": 2, "identity_scope": "two identities on one host; not two physical devices",
        "tasks": [receipts_by_id.get(ident) for ident in task_ids], "tests": tests, "accepted_tasks": len(accepted),
        "cross_device": False, "sustained": False}


async def verify(url, operator_key, machine_keys, scratch, compose):
    headers = {"Authorization": "Bearer " + operator_key}
    # Verify Claude Code's actual HTTP MCP discovery without sending inference.
    discovery_env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "RELAY_", "CLAUDE_CODE_"))}
    discovery_env.update(CLAUDE_CONFIG_DIR=str(scratch / "claude-discovery"), RELAY_OPERATOR_KEY=operator_key,
                         CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    discovery_env.pop("CLAUDECODE", None)
    discovery_config = json.dumps({"type": "http", "url": url + "/mcp",
                                 "headers": {"Authorization": "Bearer ${RELAY_OPERATOR_KEY}"}})
    added = await asyncio.to_thread(subprocess.run, claude_command() + ["mcp", "add-json", "relay", discovery_config, "--scope", "user"],
        env=discovery_env, cwd=scratch, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    assert added.returncode == 0, "Claude Code temporary MCP registration failed"
    discovery = await asyncio.to_thread(subprocess.run, claude_command() + ["mcp", "list"], env=discovery_env, cwd=scratch,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    if discovery.returncode or b"Connected" not in discovery.stdout:
        detail = (discovery.stdout + discovery.stderr).decode("utf-8", errors="replace").replace(operator_key, "[redacted]")[-1200:]
        raise AssertionError("Claude Code MCP discovery failed: " + detail)
    async with httpx.AsyncClient(headers=headers, timeout=15, trust_env=False) as transport:
        async with streamable_http_client(url + "/mcp", http_client=transport) as (receive, send, _session_id):
            async with ClientSession(receive, send) as session:
                await session.initialize()
                async def call(name, **arguments):
                    result = await session.call_tool(name, arguments)
                    if result.isError:
                        raise RuntimeError("MCP acceptance operation failed: " + name)
                    return result.structuredContent or json.loads(result.content[0].text)

                tools = {tool.name for tool in (await session.list_tools()).tools}
                assert "consult_advisor" in tools
                goal = await call("goal_create", objective="Verify two outbound runners", acceptance="Both fixture artifacts exist and separate reviews pass",
                                  authority="Write disposable fixture artifacts only", max_tasks=8)
                tasks = []
                for node in machine_keys:
                    tasks.append(await call("task_create", goal_id=goal["id"], node=node, workspace="fixture",
                                            instruction="Create the disposable acceptance artifact and report evidence", timeout_seconds=30))
                fake = scratch / "fixture_cli.py"
                fake.write_text("""import json,sys
from pathlib import Path
prompt=sys.stdin.read()
if 'Independent review only.' in prompt:
    assert Path('artifact.txt').read_text() == 'fixture artifact'
    evidence='Independent fixture review read the artifact and checked exact contents'
else:
    Path('artifact.txt').write_text('fixture artifact')
    evidence='Wrote disposable fixture artifact.txt and verified its exact contents'
print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':evidence}))
""", encoding="utf-8")
                original_args = runner.cli_args
                runner.cli_args = lambda *_args, **_kwargs: [sys.executable, str(fake)]
                os.environ["RELAY_ACCEPTANCE_INFERENCE"] = "fixture-not-a-provider-key"
                original_node_key = os.environ.get("RELAY_NODE_KEY")
                try:
                    # Run two distinct machine identities sequentially because RELAY_NODE_KEY
                    # is process-local; each task runs through real HTTP and subprocess entry points.
                    for node, key in machine_keys.items():
                        workspace = scratch / node
                        workspace.mkdir()
                        config_path = scratch / (node + ".json")
                        config_path.write_text(json.dumps({"node": node, "url": url, "gateway_url": "http://127.0.0.1:4000",
                            "gateway_key_env": "RELAY_ACCEPTANCE_INFERENCE", "workspaces": {"fixture": str(workspace)},
                            "state_dir": str(scratch / (node + "-state"))}), encoding="utf-8")
                        config = runner.load_config(config_path)
                        os.environ["RELAY_NODE_KEY"] = key
                        await runner.run(config, once=True)
                    board = await call("fleet_status")
                    assert all(t["status"] == "review" for t in board["tasks"])
                    for spec in tasks:
                        await call("task_create", goal_id=goal["id"], node=spec["node"], workspace="fixture", kind="review",
                                   reviews_task=spec["id"], instruction="Read and verify fixture artifact; do not modify it", timeout_seconds=30)
                    # Let the original runner identities age out before new instances start.
                    # Persist/restore runner instance IDs is deliberately not used by the product.
                    # We reuse live identities via a direct authenticated machine roundtrip below.
                    board = await call("fleet_status")
                    for review in [t for t in board["tasks"] if t["kind"] == "review"]:
                        node = review["node"]
                        machine = next(n for n in board["nodes"] if n["id"] == node)
                        async with httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer " + machine_keys[node]}, trust_env=False) as client:
                            response = await client.post(f"/nodes/{node}/claim", json={"instance": machine["instance"], "workspaces": ["fixture"]})
                            response.raise_for_status()
                            attempt = response.json()
                            assert attempt["id"] == review["id"]
                            command = [sys.executable, str(fake)]
                            from subroute.relay.process import execute
                            outcome, evidence = await execute(command, runner.assignment(attempt), scratch / node, os.environ.copy(), 10)
                            assert outcome == "success"
                            response = await client.post(f"/nodes/{node}/tasks/{review['id']}/report", json={
                                "instance": machine["instance"], "attempt": attempt["attempt"], "token": attempt["token"],
                                "outcome": outcome, "result": evidence})
                            response.raise_for_status()
                        await call("task_accept", task_id=review["id"], evidence="Separate fixture process inspected the actual artifact; exact-content check passed")
                        await call("task_accept", task_id=review["reviews_task"], review_task_id=review["id"],
                                   evidence="Accepted the separate fixture review and inspected its terminal evidence")
                    completed = await call("goal_set_state", goal_id=goal["id"], state="completed", evidence="Both exact-content fixture checks passed in separate review processes")
                    assert completed["status"] == "completed"
                    async with httpx.AsyncClient(trust_env=False) as unauthorized:
                        assert (await unauthorized.get(url + "/snapshot")).status_code == 401
                        assert (await unauthorized.get(url + "/snapshot", headers={"Authorization": "Bearer " + next(iter(machine_keys.values()))})).status_code == 401
                    before = (await call("fleet_status"))["revision"]
                finally:
                    runner.cli_args = original_args
                    os.environ.pop("RELAY_ACCEPTANCE_INFERENCE", None)
                    if original_node_key is None:
                        os.environ.pop("RELAY_NODE_KEY", None)
                    else:
                        os.environ["RELAY_NODE_KEY"] = original_node_key
    # Restart the actual container with the same isolated SQLite volume.
    await asyncio.to_thread(compose, "restart", "relay")
    async with httpx.AsyncClient(headers=headers, trust_env=False) as client:
        deadline = time.monotonic() + 30
        while True:
            try:
                response = await client.get(url + "/snapshot")
                response.raise_for_status()
                snapshot = response.json()
                break
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.5)
        assert snapshot["goals"][0]["status"] == "completed"
        assert snapshot["revision"] == before
    return {"mcp_tools": len(tools), "machine_identities": len(machine_keys), "accepted_tasks": 4,
            "docker_restart_persistence": True, "operator_machine_auth_separation": True,
            "claude_code_mcp_discovery": True,
            "inference": "fixture CLI only; no live provider request", "second_physical_machine": "not exercised"}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use real Claude Code sessions and bounded gateway inference")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    project = "relay-acceptance-" + uuid4().hex[:10]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    operator_key = secrets.token_urlsafe(36)
    machine_keys = {"fixture-a": secrets.token_urlsafe(36), "fixture-b": secrets.token_urlsafe(36)}
    env = dict(os.environ, RELAY_OPERATOR_KEY=operator_key, RELAY_NODE_KEYS=json.dumps(machine_keys),
        RELAY_PORT=str(port), RELAY_PUBLISH_IP="127.0.0.1", RELAY_ALLOWED_HOSTS="127.0.0.1:*",
        RELAY_ADVISOR_KEY="", RELAY_ADVISOR_DAILY_LIMIT="0")
    def compose(*args):
        result = subprocess.run(["docker", "compose", "-f", str(repo / "docker-compose.relay.yml"), "-p", project, *args],
                                cwd=repo, env=env, capture_output=True, text=True, timeout=60)
        if result.returncode:
            # These selected commands do not print expanded secret environment variables.
            raise RuntimeError(result.stderr[-3000:])
    try:
        compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "40")
        scratch_root = repo / "tmp" / "lan-relay" / "live-builder" if args.live else None
        if scratch_root:
            scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="relay-acceptance-", dir=scratch_root) as scratch:
            scratch = Path(scratch)
            if args.live:
                result = asyncio.run(verify_live(f"http://127.0.0.1:{port}", operator_key, machine_keys, scratch, compose))
            else:
                result = asyncio.run(verify(f"http://127.0.0.1:{port}", operator_key, machine_keys, scratch, compose))
        if args.live:
            summary = repo / "tmp" / "lan-relay" / "live-builder" / ("subroute-relay-live-" + project + ".json")
            summary.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
