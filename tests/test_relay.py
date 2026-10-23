"""Behavioral acceptance for the optional LAN workboard and local executor."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest

pytest.importorskip("mcp")
from starlette.testclient import TestClient

from subroute.relay.store import Conflict, GoalSpec, Store, TaskSpec
from subroute.relay.server import Settings, create_app
from subroute.relay.process import execute, terminal_result
from subroute.relay import runner
from subroute.relay.runner import FileLock, inference_env, load_config
from subroute.relay.supervisor import should_run


@pytest.fixture
def board(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / "relay.db", lease_seconds=30, clock=lambda: now[0])
    goal = store.create_goal(GoalSpec(objective="Fix a real bug", acceptance="Tests pass and independent review", authority="Local edits/tests only"))
    store.node_heartbeat("pc", "instance", ["repo", "other"])
    return store, goal, now


def task(board, **kwargs):
    store, goal, _now = board
    spec = dict(goal_id=goal["id"], node="pc", workspace="repo", instruction="Fix the bug and test")
    spec.update(kwargs)
    return store.create_task(TaskSpec(**spec))


def claim(store, workspace="repo"):
    return store.claim("pc", "instance", [workspace])


def owned(attempt):
    return (attempt["id"], "pc", "instance", attempt["attempt"], attempt["token"])


def test_claim_is_atomic_and_persistent(board):
    store, _goal, _now = board
    task(board)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: claim(store), range(6)))
    assert sum(r is not None for r in results) == 1
    restored = Store(store.path, lease_seconds=30, clock=store.clock)
    assert restored.snapshot()["tasks"][0]["status"] == "running"
    assert "token" not in json.dumps(restored.snapshot())
    assert claim(restored) is None


def test_separate_workspaces_can_run_concurrently(board):
    store, goal, _now = board
    task(board)
    store.create_task(TaskSpec(goal_id=goal["id"], node="pc", workspace="other", instruction="Inspect independent repo"))
    assert claim(store)["workspace"] == "repo"
    assert claim(store, "other")["workspace"] == "other"


def test_expired_work_quarantines_workspace_and_rejects_late_results(board):
    store, _goal, now = board
    task(board)
    first = claim(store)
    task(board)
    now[0] += 31
    assert store.snapshot()["tasks"][0]["status"] == "reconcile"
    store.node_heartbeat("pc", "instance", ["repo"])
    assert claim(store) is None
    with pytest.raises(Conflict, match="Stale"):
        store.report(*owned(first), "success", "late success")
    with pytest.raises(Conflict):
        store.retry(first["id"])
    store.reconcile(first["id"], "Operator killed old process tree and inspected git diff")
    assert claim(store) is not None


def test_cancel_fences_success_until_process_stop_acknowledged(board):
    store, _goal, _now = board
    task(board)
    attempt = claim(store)
    assert store.cancel(attempt["id"])["status"] == "cancelling"
    assert store.pulse(*owned(attempt))["stop"]
    with pytest.raises(Conflict, match="Cancelled"):
        store.report(*owned(attempt), "success", "too late")
    assert store.report(*owned(attempt), "cancelled", "Stopped process tree")["status"] == "cancelled"


def test_pause_cancels_current_work_and_blocks_new_claims(board):
    store, goal, _now = board
    task(board)
    attempt = claim(store)
    store.goal_state(goal["id"], "paused")
    assert store.pulse(*owned(attempt))["stop"]
    assert claim(store) is None


def test_result_delivery_idempotent_and_attempt_budget_preserved(board):
    store, _goal, _now = board
    task(board, max_attempts=1)
    attempt = claim(store)
    first = store.report(*owned(attempt), "failed", "Observed missing executable")
    assert store.report(*owned(attempt), "failed", "Observed missing executable") == first
    with pytest.raises(Conflict):
        store.report(*owned(attempt), "success", "different report")
    with pytest.raises(Conflict, match="budget"):
        store.retry(attempt["id"])


def test_goal_acceptance_requires_separate_review(board):
    store, goal, now = board
    original = task(board)
    attempt = claim(store)
    store.report(*owned(attempt), "success", "Implemented regression; pytest passes")
    with pytest.raises(Conflict):
        store.accept(original["id"], "Trusting worker is insufficient")
    with pytest.raises(Conflict):
        store.goal_state(goal["id"], "completed", "No actual independent acceptance")
    review = task(board, kind="review", reviews_task=original["id"])
    now[0] += 1
    reviewer = claim(store)
    assert reviewer["id"] == review["id"]
    store.report(*owned(reviewer), "success", "Independent checks: bug reproduced before fix, targeted tests pass after")
    store.accept(review["id"], "Review evidence inspected; no unresolved findings")
    store.accept(original["id"], "Separate review reran targeted tests and checked diff", review["id"])
    assert store.goal_state(goal["id"], "completed", "Observed intended behavior and accepted review")["status"] == "completed"


def test_review_reserves_workspace_until_original_acceptance(board):
    store, _goal, _now = board
    original = task(board)
    work_attempt = claim(store)
    store.report(*owned(work_attempt), "success", "Work result awaiting review")
    competing = task(board, instruction="Do not write during review")
    independent = task(board, workspace="other", instruction="Independent workspace")
    assert claim(store) is None
    review = task(board, kind="review", reviews_task=original["id"])

    reviewer = claim(store)
    assert reviewer["id"] == review["id"]
    store.report(*owned(reviewer), "success", "Independent review completed")
    store.accept(review["id"], "Inspected independent review evidence")

    assert claim(store) is None
    assert claim(store, "other")["id"] == independent["id"]
    store.accept(original["id"], "Accepted exact reviewed work result", review["id"])
    assert claim(store)["id"] == competing["id"]


def test_cancelling_work_awaiting_review_releases_workspace(board):
    store, _goal, _now = board
    original = task(board)
    attempt = claim(store)
    store.report(*owned(attempt), "success", "Work result awaiting review")
    competing = task(board, instruction="Run after cancellation")

    assert store.cancel(original["id"])["status"] == "cancelled"
    assert claim(store)["id"] == competing["id"]


def test_old_review_cannot_certify_retried_result(board):
    store, _goal, now = board
    original = task(board)
    attempt = claim(store)
    store.report(*owned(attempt), "success", "first result")
    review = task(board, kind="review", reviews_task=original["id"])
    reviewer = claim(store)
    store.report(*owned(reviewer), "success", "review result")
    store.accept(review["id"], "Reviewed initial result with tests")
    store.cancel(original["id"])
    store.retry(original["id"])
    # A wall-clock correction must not let an old review certify another attempt.
    now[0] -= 5
    later = claim(store)
    store.report(*owned(later), "success", "new changed result")
    with pytest.raises(Conflict, match="separate review"):
        store.accept(original["id"], "Cannot reuse old review evidence", review["id"])


def test_heartbeat_does_not_extend_hard_deadline(board):
    store, _goal, now = board
    task(board, timeout_seconds=10)
    attempt = claim(store)
    now[0] += 9
    store.pulse(*owned(attempt), "tool:Read")
    now[0] += 2
    assert store.snapshot()["tasks"][0]["status"] == "reconcile"


def test_live_node_identity_cannot_be_taken_over(board):
    store, _goal, now = board
    with pytest.raises(Conflict, match="Another live runner"):
        store.node_heartbeat("pc", "second", ["repo"])
    now[0] += 31
    store.node_heartbeat("pc", "second", ["repo"])
    with pytest.raises(Conflict):
        claim(store)


def test_advisor_reservation_counts_failed_calls_and_persists(board):
    store, _goal, now = board
    with pytest.raises(Conflict):
        store.reserve_advice(0)
    store.reserve_advice(1)
    with pytest.raises(Conflict):
        Store(store.path, clock=store.clock).reserve_advice(1)
    now[0] += 86400
    store.reserve_advice(1)


@pytest.fixture
def client(board):
    settings = Settings(operator_key="o" * 40, node_keys={"pc": "n" * 40}, hosts=("testserver:*",))
    with TestClient(create_app(board[0], settings)) as client:
        yield client


def test_operator_node_auth_separation_host_origin_and_size(client):
    operator = {"Authorization": "Bearer " + "o" * 40}
    node = {"Authorization": "Bearer " + "n" * 40}
    assert client.get("/health").status_code == 200
    assert client.get("/snapshot").status_code == 401
    assert client.get("/snapshot", headers=node).status_code == 401
    assert client.post("/nodes/pc/claim", headers=operator, json={}).status_code == 401
    assert client.post("/nodes/other/claim", headers=node, json={}).status_code == 401
    assert client.get("/snapshot", headers=dict(operator, Origin="https://evil.example")).status_code == 403
    assert client.get("/snapshot", headers=dict(operator, Host="evil.example")).status_code == 421
    assert client.post("/nodes/pc/heartbeat", headers=node, content=b"x" * 65537).status_code == 413
    assert client.post("/nodes/pc/heartbeat", headers=node, json={"instance": "bad", "workspaces": [], "extra": "secret"}).json() == {"error": "Invalid request"}


def test_real_mcp_initialize_and_tool_discovery(client):
    headers = {"Authorization": "Bearer " + "o" * 40, "Accept": "application/json, text/event-stream"}
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "acceptance", "version": "1"}}})
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "Subroute Relay"
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert {"goal_create", "task_create", "task_get", "task_cancel", "consult_advisor"} <= names
    response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fleet_status", "arguments": {}}})
    assert not response.json()["result"].get("isError")


def test_host_boundary_accepts_configured_ipv6_and_rejects_other_hosts(board):
    settings = Settings(operator_key="o" * 40, node_keys={"pc": "n" * 40}, hosts=("[::1]:*",))
    with TestClient(create_app(board[0], settings)) as client:
        assert client.get("/health", headers={"Host": "[::1]:8765"}).status_code == 200
        assert client.get("/health", headers={"Host": "evil.example:8765"}).status_code == 421


@pytest.mark.parametrize("event,code,expected", [
    ({"type": "result", "subtype": "success", "is_error": False, "result": "Observed evidence"}, 0, "success"),
    ({"type": "result", "subtype": "error_max_turns", "is_error": True}, 0, "failed"),
    ({"type": "result", "subtype": "success", "is_error": False, "result": ""}, 0, "failed"),
    ({"type": "result", "subtype": "success", "is_error": False, "result": "evidence"}, 1, "failed"),
    ({"type": "assistant", "message": "done"}, 0, "failed"),
])
def test_terminal_outcome_not_process_exit(event, code, expected):
    assert terminal_result([event], code)[0] == expected
    assert terminal_result([event, event], code)[0] == "failed"


def test_actual_subprocess_valid_result_and_malformed_result(tmp_path):
    script = "import json,sys; sys.stdin.read(); print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'read actual fixture'}))"
    outcome, result = asyncio.run(execute([sys.executable, "-c", script], "assignment", tmp_path, os.environ.copy(), 5))
    assert outcome == "success", result
    assert "read actual fixture" in result
    assert asyncio.run(execute([sys.executable, "-c", "print('not JSON')"], "", tmp_path, os.environ.copy(), 5))[0] == "failed"


def test_failed_cli_receipt_retains_usage_and_session_for_recovery():
    event = {"type": "result", "subtype": "error_max_turns", "is_error": True,
             "session_id": "failed-session", "usage": {"input_tokens": 120}, "num_turns": 12,
             "total_cost_usd": 0.1}
    outcome, result = terminal_result([event], 1)
    data = json.loads(result)
    assert outcome == "failed" and data["error_type"] == "error_max_turns"
    assert data["session_id"] == "failed-session" and data["usage"]["input_tokens"] == 120
    assert data["num_turns"] == 12


def test_terminal_success_receipt_truncates_escaped_text_before_encoding():
    event = {"type": "result", "subtype": "success", "is_error": False,
             "session_id": "11111111-1111-4111-8111-111111111111",
             "usage": {"input_tokens": 123}, "result": ('"\\\n' * 5000)}
    outcome, result = terminal_result([event], 0)
    receipt = json.loads(result)
    assert outcome == "success"
    assert len(result.encode("utf-8")) <= 16000
    assert receipt["session_id"] == event["session_id"]
    assert receipt["usage"] == event["usage"]
    assert event["result"].startswith(receipt["result"])
    assert receipt["result"] and len(receipt["result"]) < len(event["result"])


def test_terminal_success_receipt_respects_utf8_byte_limit_for_astral_text():
    event = {"type": "result", "subtype": "success", "is_error": False,
             "session_id": "astral-session", "usage": {"output_tokens": 4},
             "result": "😀" * 10000}
    outcome, result = terminal_result([event], 0)
    receipt = json.loads(result)
    assert outcome == "success"
    assert len(result.encode("utf-8")) <= 16000
    assert receipt["session_id"] == event["session_id"]
    assert receipt["usage"] == event["usage"]
    assert event["result"].startswith(receipt["result"])
    assert receipt["result"] and len(receipt["result"]) < len(event["result"])


def test_failed_cli_receipt_truncates_escaped_result_and_preserves_failure():
    event = {"type": "result", "subtype": "error_max_turns", "is_error": True,
             "session_id": "failed-session", "usage": {"input_tokens": 120},
             "result": ('"\\\n' * 5000)}
    outcome, result = terminal_result([event], 1)
    receipt = json.loads(result)
    assert outcome == "failed"
    assert len(result.encode("utf-8")) <= 16000
    assert receipt["error_type"] == "error_max_turns"
    assert receipt["session_id"] == event["session_id"]
    assert receipt["usage"] == event["usage"]
    assert event["result"].startswith(receipt["result"])


def test_terminal_receipt_fails_closed_when_metadata_cannot_fit():
    event = {"type": "result", "subtype": "success", "is_error": False,
             "session_id": "recoverable-session", "usage": {"payload": "x" * 20000},
             "result": "evidence"}
    outcome, result = terminal_result([event], 0)
    receipt = json.loads(result)
    assert outcome == "failed"
    assert len(result.encode("utf-8")) <= 16000
    assert receipt["error_type"] == "metadata_too_large"
    assert receipt["session_id"] == event["session_id"]


@pytest.mark.parametrize("failure", ["timeout", "cancel", "network"])
def test_process_tree_stops_on_timeout_cancel_and_network_loss(tmp_path, failure):
    marker = tmp_path / "descendant.txt"
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).write_text('orphan')"
    script = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(20)"
    async def pulse(_activity):
        if failure == "network":
            raise ConnectionError("offline")
        return failure == "cancel"
    outcome, _result = asyncio.run(execute([sys.executable, "-c", script], "", tmp_path, os.environ.copy(),
        0.5 if failure == "timeout" else 5, None if failure == "timeout" else pulse, pulse_interval=0.2))
    assert outcome == ("cancelled" if failure == "cancel" else "failed")
    time.sleep(2.1)
    assert not marker.exists(), "Descendant survived cancellation"


def test_success_also_cleans_lingering_descendant(tmp_path):
    marker = tmp_path / "descendant.txt"
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).write_text('orphan')"
    script = f"import subprocess,sys,json; subprocess.Popen([sys.executable,'-c',{child!r}]); print(json.dumps({{'type':'result','subtype':'success','is_error':False,'result':'done'}}), flush=True)"
    outcome, result = asyncio.run(execute([sys.executable, "-c", script], "", tmp_path, os.environ.copy(), 5))
    assert outcome == "success", result
    time.sleep(2.1)
    assert not marker.exists()


def test_local_workspace_and_instance_lock(tmp_path):
    with FileLock(tmp_path / "runner.lock"):
        with pytest.raises(RuntimeError):
            with FileLock(tmp_path / "runner.lock"):
                pass


def test_local_config_rejects_duplicate_and_nested_workspaces(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    config = {"node": "pc", "url": "http://127.0.0.1:8765", "gateway_url": "http://127.0.0.1:4000",
              "workspaces": {"root": str(tmp_path), "nested": str(nested)}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="overlap"):
        load_config(path)


def test_worker_does_not_inherit_operator_or_expert_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_OPERATOR_KEY", "operator-secret")
    monkeypatch.setenv("EXPERTS_API_KEY", "expert-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unrelated-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "authorized-inference-key")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0")
    env = inference_env({"state": tmp_path, "gateway_url": "http://127.0.0.1:4000"})
    assert env["ANTHROPIC_API_KEY"] == "authorized-inference-key"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert not {"RELAY_OPERATOR_KEY", "EXPERTS_API_KEY", "ANTHROPIC_AUTH_TOKEN"} & env.keys()


@pytest.mark.parametrize("explicit_guidance", [False, True])
def test_runner_executes_bounded_assignment_with_optional_local_guidance(tmp_path, monkeypatch, explicit_guidance):
    import httpx

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    guidance = tmp_path / "LOCAL.md"
    guidance.write_text("LOCAL FIXTURE GUIDANCE", encoding="utf-8")
    monkeypatch.setattr(runner.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("RELAY_NODE_KEY", "n" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inference-fixture")

    task = {"id": "task-1", "attempt": 1, "token": "t" * 40, "workspace": "project",
            "kind": "build", "max_turns": 4, "timeout_seconds": 30,
            "goal": {"objective": "Fix the bounded bug", "acceptance": "Regression test passes",
                     "authority": "Local edits and tests only"},
            "instruction": "Implement and test the fix", "reviews_task": None}
    posted = []

    def respond(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "heartbeat":
            return httpx.Response(200, json={"lease_seconds": 30})
        if action == "claim":
            return httpx.Response(200, json=task)
        if action == "pulse":
            return httpx.Response(200, json={"stop": False})
        if action == "report":
            posted.append(request)
            return httpx.Response(200, json={"status": "completed"})
        raise AssertionError(f"Unexpected coordinator action: {action}")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    captured = []

    async def fake_execute(_command, prompt, _root, _env, _timeout, _pulse, **_kwargs):
        captured.append(prompt)
        return "success", "Verified result submitted"

    monkeypatch.setattr(runner, "execute", fake_execute)
    if not explicit_guidance:
        def unexpected_discovery(_config):
            raise AssertionError("Default runner must not discover or read an installed Northstar file")
        monkeypatch.setattr(runner, "northstar_instructions", unexpected_discovery)

    config = {"node": "worker", "url": "http://relay-fixture.test:8765",
              "gateway_url": "http://gateway-fixture.test:4000", "state": state,
              "roots": {"project": workspace}, "workspaces": {"project": str(workspace)},
              "slots": 1, "lease_seconds": 30}
    if explicit_guidance:
        config["northstar_path"] = str(guidance)

    asyncio.run(runner.run(config, once=True))

    assert len(captured) == 1
    prompt = captured[0]
    assert "The coordinator owns Northstar policy, separate review, and continuation" in prompt
    assert "Fix the bounded bug" in prompt and "Regression test passes" in prompt
    assert "Implement and test the fix" in prompt
    assert ("LOCAL FIXTURE GUIDANCE" in prompt) is explicit_guidance
    assert "Central coordinator of the six-skill" not in prompt
    assert len(posted) == 1
    report = json.loads(posted[0].content)
    assert report["outcome"] == "success"
    assert "Verified result submitted" in report["result"]


def test_explicit_missing_local_instruction_file_fails_clearly(tmp_path):
    with pytest.raises(ValueError, match="Configured local instruction file is missing"):
        runner.northstar_instructions({"northstar_path": str(tmp_path / "missing.md")})


def test_supervision_is_event_driven_with_durable_daily_budget():
    snapshot = {"revision": 8, "goals": [{"status": "active"}]}
    assert should_run(snapshot, {}, 1000, 4)
    assert not should_run(snapshot, {"revision": 8}, 1000, 4)
    assert not should_run(snapshot, {"day": 0, "calls": 4}, 1000, 4)
    assert should_run(snapshot, {"day": 0, "calls": 4}, 90000, 4)
    assert not should_run(snapshot, {"failed_revision": 8, "failures": 2}, 1000, 4)


def test_supervisor_restart_respects_reserved_budget_and_cursor(tmp_path, monkeypatch):
    import httpx
    from subroute.relay import process, supervisor
    monkeypatch.setenv("RELAY_OPERATOR_KEY", "operator-" + "o" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inference-fixture")
    monkeypatch.setattr(supervisor.Path, "home", classmethod(lambda cls: tmp_path))
    installed = tmp_path / ".agents/skills/northstar/SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("SUPERVISOR FULL GUIDANCE", encoding="utf-8")
    monkeypatch.setattr(process, "claude_command", lambda: ["fixture-claude"])
    snapshot = {"revision": 10, "goals": [{"status": "active"}]}
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=snapshot))
    monkeypatch.setattr(supervisor.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    calls = []
    async def fake_execute(command, _prompt, _cwd, env, _timeout):
        assert "SUPERVISOR FULL GUIDANCE" in _prompt
        reserved = json.loads((tmp_path / "supervisor/supervisor.json").read_text())
        assert reserved["failures"] == 1 and reserved["calls"] == 1
        assert command[command.index("--tools") + 1] == ""
        assert "${RELAY_OPERATOR_KEY}" in command[command.index("--mcp-config") + 1]
        assert env["RELAY_OPERATOR_KEY"] == "operator-" + "o" * 40
        calls.append(command)
        return "success", "Observed and recorded useful next action"
    monkeypatch.setattr(supervisor, "execute", fake_execute)
    config = {"url": "http://relay-fixture.test:8765", "gateway_url": "http://127.0.0.1:4000",
              "state_dir": str(tmp_path / "supervisor"), "daily_cycles": 1}
    asyncio.run(supervisor.run(config, once=True))
    asyncio.run(supervisor.run(config, once=True))
    assert len(calls) == 1
    snapshot["revision"] = 11
    asyncio.run(supervisor.run(config, once=True))
    assert len(calls) == 1
    receipt = json.loads((tmp_path / "supervisor/supervisor.json").read_text())
    assert receipt["calls"] == 1 and receipt["revision"] == 10


def live_verifier():
    spec = importlib.util.spec_from_file_location("relay_acceptance_verifier", Path(__file__).resolve().parents[1] / "scripts/verify_relay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name,linked,error,count,passed", [
    ("Bash", True, False, 2, True),
    ("Read", True, False, 2, False),
    ("Bash", False, False, 2, False),
    ("Bash", True, True, 2, False),
    ("Bash", True, False, 0, False),
])
def test_live_acceptance_requires_matching_real_test_result(tmp_path, name, linked, error, count, passed):
    verifier = live_verifier()
    session = "11111111-1111-4111-8111-111111111111"
    trace = tmp_path / "claude/projects/fixture"
    trace.mkdir(parents=True)
    events = [
        {"message": {"content": [{"type": "tool_use", "name": name, "id": "actual-call", "input": {"command": "python -m unittest -v"}}]}},
        {"message": {"content": [{"type": "tool_result", "tool_use_id": "actual-call" if linked else "other-call",
            "is_error": error, "content": f"Ran {count} tests in 0.01s\n\nOK\n"}]}},
    ]
    (trace / (session + ".jsonl")).write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    task = {"outcome": "success", "result": json.dumps({"session_id": session, "usage": {}, "result": "I claim everything passed"})}
    if passed:
        assert verifier._test_trace(tmp_path, task)["test_count"] == count
    else:
        with pytest.raises(RuntimeError, match="No successful actual unittest"):
            verifier._test_trace(tmp_path, task)


def test_live_parent_holdout_rejects_self_confirming_worker_tests(tmp_path):
    verifier = live_verifier()
    (tmp_path / "labels.py").write_text("def normalize_labels(values):\n    return []\n", encoding="utf-8")
    (tmp_path / "test_labels.py").write_text("import unittest\nfrom labels import normalize_labels\nclass TestLabels(unittest.TestCase):\n    def test_empty(self):\n        self.assertEqual(normalize_labels([]), [])\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="semantic holdout failed"):
        verifier._parent_tests(tmp_path)


def test_live_failure_capture_preserves_full_receipts_from_both_nodes():
    verifier = live_verifier()
    receipt = json.dumps({"result": "Evidence " * 400, "usage": {"input_tokens": 123}, "session_id": "session-a"})
    observed = {"offline": {"id": "offline", "result": "earlier full observation"}}

    async def call(name, **arguments):
        assert name == "task_get"  # fleet_status intentionally truncates long receipts.
        ident = arguments["task_id"]
        if ident == "offline":
            raise RuntimeError("hub unavailable")
        return {"id": ident, "node": "node-" + ident, "result": receipt}

    asyncio.run(verifier._capture_live_tasks(call, ["a", "b", "offline"], observed))
    assert {observed[k]["node"] for k in ("a", "b")} == {"node-a", "node-b"}
    assert all(json.loads(observed[k]["result"])["usage"] == {"input_tokens": 123} for k in ("a", "b"))
    assert observed["offline"]["result"] == "earlier full observation"
