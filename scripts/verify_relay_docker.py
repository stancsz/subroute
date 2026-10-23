"""Provider-free acceptance using the shipped Compose services in an internal Docker network.

Builds current Relay images, shadows Claude with an explicit scripted fixture, and
removes only its uniquely named project and volumes. Requires Docker Compose 2.24.4+
for the fixture overlay's !override tag. Does not establish native model quality.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/acceptance/relay"


class Acceptance:
    def __init__(self):
        self.project = "relay-docker-acceptance-" + uuid4().hex[:10]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.keys = {n: secrets.token_urlsafe(36) for n in ("fixture-a", "fixture-b")}
        self.operator = secrets.token_urlsafe(36)
        # Only these explicit values enter containers. Never pass real inference keys.
        self.env = dict(os.environ, RELAY_OPERATOR_KEY=self.operator, RELAY_NODE_KEYS=json.dumps(self.keys),
            RELAY_NODE_KEY=self.keys["fixture-a"], RELAY_NODE_KEY_B=self.keys["fixture-b"],
            GATEWAY_MASTER_KEY="fixture-inference-not-a-key", RELAY_PORT=str(port), RELAY_PUBLISH_IP="127.0.0.1",
            RELAY_ALLOWED_HOSTS="127.0.0.1:*,relay:*", RELAY_ADVISOR_KEY="", RELAY_ADVISOR_DAILY_LIMIT="0",
            RELAY_ADVISOR_URL="http://127.0.0.1:1/v1", RELAY_WORKSPACE=str(FIXTURES),
            RELAY_RUNNER_CONFIG=str(FIXTURES / "fixtures/runner-a.json"))
        self.started = time.monotonic()
        self.deadline = self.started + 240
        self.checks = []
        self.receipt = {"project": self.project, "mode": "scripted CLI fixture in isolated Docker",
            "native_model_inference": False, "physical_devices": 1, "sustained": False, "checks": self.checks}

    def command(self, args, *, timeout=60, cleanup=False):
        limit = timeout if cleanup else min(timeout, max(.1, self.deadline - time.monotonic()))
        result = subprocess.run(args, cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=limit)
        if result.returncode:
            raise RuntimeError(self.redact((result.stdout + result.stderr)[-1800:]))
        return result.stdout.strip()

    def compose(self, *args, cleanup=False):
        return self.command(["docker", "compose", "-f", str(ROOT / "docker-compose.relay.yml"),
            "-f", str(ROOT / "docker-compose.relay-worker.yml"), "-f", str(FIXTURES / "compose.fixture.yml"),
            "-p", self.project, *args], cleanup=cleanup)

    def redact(self, text):
        for secret in [self.operator, *self.keys.values(), self.env["GATEWAY_MASTER_KEY"]]:
            text = text.replace(secret, "[redacted]")
        return text

    def checked(self, name, **evidence):
        self.checks.append({"name": name, "passed": True, **evidence})
        print("PASS " + name, flush=True)

    def setup(self):
        for file, tag in [("sidecars/relay/Dockerfile", "subroute-relay:0.1.0"),
                          ("sidecars/relay/Dockerfile.runner", "subroute-relay-runner:0.1.0"),
                          ("tests/acceptance/relay/Dockerfile.fixture", "subroute-relay-runner-fixture:0.1.0")]:
            self.command(["docker", "build", "-f", file, "-t", tag, "."], timeout=120)
        config = json.loads(self.compose("config", "--format", "json"))
        assert config["networks"]["default"]["internal"] is True
        for service in ("worker", "worker-b"):
            spec = config["services"][service]
            assert spec["read_only"] and spec["init"] and "ALL" in spec["cap_drop"]
            assert not spec.get("ports") and not spec.get("extra_hosts")
            assert "RELAY_OPERATOR_KEY" not in spec["environment"]
            assert spec["environment"]["GATEWAY_MASTER_KEY"] == "fixture-inference-not-a-key"
            assert all(v["type"] == "volume" for v in spec["volumes"])
        self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "40")
        for service in ("worker", "worker-b"):
            networks = json.loads(self.command(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", self.cid(service)]))
            assert set(networks) == {self.project + "_default"}
            mounts = json.loads(self.command(["docker", "inspect", "--format", "{{json .Mounts}}", self.cid(service)]))
            for mount in mounts:
                if mount["Type"] == "bind":
                    # Compose configs are the only host files mounted, all public fixtures.
                    normalized = mount["Source"].replace("\\", "/").lower()
                    assert "/tests/acceptance/relay/" in normalized and not mount["RW"]
                else:
                    assert mount["Type"] == "volume" and mount["Name"].startswith(self.project + "_")
        network = self.command(["docker", "network", "inspect", self.project + "_default", "--format", "{{.Internal}}"])
        assert network == "true"
        self.checked("isolated_shipped_compose", internal_network=True, real_provider_keys=False,
                     private_named_volumes=True, read_only_fixture_configs=True)

    def cid(self, service):
        return self.compose("ps", "-a", "-q", service)

    def python(self, service, code, *args):
        return self.compose("exec", "-T", service, "python", "-c", code, *map(str, args))

    def read(self, service, path):
        return json.loads(self.python(service, "from pathlib import Path; import json,sys; p=Path(sys.argv[1]); print(json.dumps(p.read_text() if p.is_file() else None))", path))

    def write(self, service, path, text):
        self.python(service, "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])", path, text)

    async def ready(self):
        end = min(self.deadline, time.monotonic() + 40)
        observation = "not observed"
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + self.operator}, trust_env=False, timeout=3) as client:
            while time.monotonic() < end:
                try:
                    board = (await client.get(self.url + "/snapshot")).raise_for_status().json()
                    online = {n["id"] for n in board["nodes"] if n["online"]}
                    observation = "online identities: " + ",".join(sorted(online))
                    if online == set(self.keys):
                        return board
                except httpx.HTTPError as exc:
                    observation = type(exc).__name__
                await asyncio.sleep(.5)
        raise TimeoutError("Client readiness deadline: " + observation)

    @asynccontextmanager
    async def mcp(self):
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + self.operator}, trust_env=False, timeout=8) as client:
            async with streamable_http_client(self.url + "/mcp", http_client=client) as (receive, send, _):
                async with ClientSession(receive, send) as session:
                    await session.initialize()

                    async def call(name, *, reject=False, **arguments):
                        result = await session.call_tool(name, arguments)
                        if reject:
                            assert result.isError, "Forbidden transition unexpectedly succeeded: " + name
                            return None
                        if result.isError:
                            detail = " ".join(c.text for c in result.content if hasattr(c, "text"))
                            raise AssertionError("MCP operation failed: " + name + ": " + self.redact(detail[:1000]))
                        return result.structuredContent or json.loads(result.content[0].text)
                    yield call

    async def status(self, call, ident, wanted, timeout=35):
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            task = await call("task_get", task_id=ident)
            if task["status"] == wanted:
                return task
            if task["status"] in {"failed", "cancelled", "reconcile"}:
                raise AssertionError(f"Expected {wanted}, observed {task['status']}: " + str(task.get("result"))[:200])
            await asyncio.sleep(.2)
        raise TimeoutError("Task failed to reach " + wanted)

    async def goal(self, call, objective, max_tasks):
        return await call("goal_create", objective=objective, max_tasks=max_tasks,
            acceptance="Exact fixture artifacts, separate passing review and explicit acceptance; reject unknown or failed execution",
            authority="Only the disposable internal-network fixture containers and their private volumes; no providers or external actions")

    async def task(self, call, goal, node, scenario, expected, **overrides):
        arguments = dict(goal_id=goal["id"], node=node, workspace="private",
            instruction=json.dumps({"scenario": scenario, "expected": expected}), timeout_seconds=180,
            max_turns=16, max_attempts=2)
        arguments.update(overrides)
        return await call("task_create", **arguments)

    async def review_and_accept(self, call, goal, work, scenario, expected):
        task = await self.status(call, work["id"], "review")
        await call("task_accept", task_id=task["id"], evidence="Premature acceptance must fail without a separate review", reject=True)
        review = await self.task(call, goal, task["node"], scenario, expected, kind="review", reviews_task=task["id"])
        reviewed = await self.status(call, review["id"], "review")
        assert json.loads(reviewed["result"])["result"] == "RELAY_REVIEW_PASS"
        await call("task_accept", task_id=review["id"], evidence="Separate fixture process inspected the actual artifact and its exact bytes matched")
        await call("task_accept", task_id=task["id"], review_task_id=review["id"], evidence="Parent artifact oracle and accepted separate fixture review both passed")
        return task

    async def main_cases(self):
        board = await self.ready()
        assert all(n["workspaces"] == ["private"] for n in board["nodes"])
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            assert (await client.get(self.url + "/snapshot")).status_code == 401
            headers = {"Authorization": "Bearer " + self.keys["fixture-a"]}
            assert (await client.get(self.url + "/snapshot", headers=headers)).status_code == 401
            denied = await client.post(self.url + "/nodes/fixture-b/heartbeat", headers=headers,
                                      json={"instance": "foreign", "workspaces": ["private"]})
            assert denied.status_code == 401
        self.checked("operator_and_node_auth_separation")
        async with self.mcp() as call:
            goal = await self.goal(call, "Concurrent independent fixture work", 4)
            a = await self.task(call, goal, "fixture-a", "work", "alpha")
            b = await self.task(call, goal, "fixture-b", "work", "beta")
            await self.status(call, a["id"], "running")
            await self.status(call, b["id"], "running")
            end = time.monotonic() + 10
            while not (self.read("worker", "/workspace/work.started") and self.read("worker-b", "/workspace/work.started")):
                assert time.monotonic() < end, "Fixture work did not reach its concurrency barrier"
                await asyncio.sleep(.1)
            for service in ("worker", "worker-b"):
                launch = json.loads(self.read(service, "/workspace/launch.json"))
                assert launch == dict(model="default", max_turns=2, budget=.01, operator_key_present=False,
                                     node_key_present=False, gateway_master_key_present=False, dummy_inference_key=True,
                                     local_guidance_present=service == "worker-b")
                self.write(service, "/workspace/work.release", "release")
            await self.review_and_accept(call, goal, a, "work", "alpha")
            await self.review_and_accept(call, goal, b, "work", "beta")
            assert self.read("worker", "/workspace/artifact.txt") == "alpha"
            assert self.read("worker-b", "/workspace/artifact.txt") == "beta"
            completed = await call("goal_set_state", goal_id=goal["id"], state="completed", evidence="Both exact artifacts and separate reviews passed with private workspaces and bounded CLI flags")
            assert completed["status"] == "completed"
            self.checked("concurrent_work_review_acceptance", accepted_tasks=4, overlap_barrier=True,
                         local_caps_override_remote=True, inference_credentials_separated=True,
                         default_without_skills=True, explicit_local_guidance=True)

            goal = await self.goal(call, "Pause cancels the owned process tree", 1)
            task = await self.task(call, goal, "fixture-a", "descendant", "unused")
            await self.status(call, task["id"], "running")
            end = time.monotonic() + 10
            while not self.read("worker", "/workspace/descendant.heartbeat"):
                assert time.monotonic() < end
                await asyncio.sleep(.1)
            pid = int(self.read("worker", "/workspace/descendant.pid"))
            await call("goal_set_state", goal_id=goal["id"], state="paused", evidence="Actual descendant heartbeat observed; stop this goal and its owned execution")
            await self.status(call, task["id"], "cancelled", timeout=18)
            heartbeat = self.read("worker", "/workspace/descendant.heartbeat")
            await asyncio.sleep(.6)
            assert self.read("worker", "/workspace/descendant.heartbeat") == heartbeat
            self.python("worker", "from pathlib import Path; import sys; p=Path('/proc')/sys.argv[1]/'stat'; assert not p.exists() or p.read_text().split(') ')[1][0] == 'Z'", pid)
            await self.task(call, goal, "fixture-b", "work", "forbidden", reject=True)
            self.checked("pause_cancels_descendants_and_blocks_new_work", stopped_descendant=True)

            goal = await self.goal(call, "Reject malformed and incorrect fixture outcomes", 4)
            long_task = await self.task(call, goal, "fixture-b", "large_terminal", "unused")
            long_result = await self.status(call, long_task["id"], "review")
            receipt = json.loads(long_result["result"])
            assert receipt["result"] and ('"\\\n😀' * 10000).startswith(receipt["result"])
            assert len(long_result["result"].encode("utf-8")) <= 16000
            assert receipt["session_id"] == "fixture-session" and receipt["usage"]["input_tokens"] == 0
            await call("task_cancel", task_id=long_task["id"])
            self.checked("escaped_utf8_receipt_survives_http_report", utf8_bytes=len(long_result["result"].encode("utf-8")))
            malformed = await self.task(call, goal, "fixture-b", "bad_terminal", "unused")
            await self.status(call, malformed["id"], "failed")
            await call("task_accept", task_id=malformed["id"], evidence="Malformed terminal must never be accepted", reject=True)
            await call("task_cancel", task_id=malformed["id"])
            wrong = await self.task(call, goal, "fixture-b", "artifact_mismatch", "right fixture content")
            await self.status(call, wrong["id"], "review")
            review = await self.task(call, goal, "fixture-b", "artifact_mismatch", "right fixture content", kind="review", reviews_task=wrong["id"])
            await self.status(call, review["id"], "failed")
            await call("task_accept", task_id=wrong["id"], review_task_id=review["id"], evidence="Failed review must block work acceptance", reject=True)
            await call("goal_set_state", goal_id=goal["id"], state="completed", evidence="Incorrect artifact must block completion", reject=True)
            await call("task_cancel", task_id=wrong["id"])
            await call("goal_set_state", goal_id=goal["id"], state="paused", evidence="Negative cases intentionally failed and remain unaccepted")
            self.checked("bad_terminal_and_wrong_artifact_rejected")

            verdict_goal = await self.goal(call, "A successful CLI is not a passing review", 2)
            work = await self.task(call, verdict_goal, "fixture-b", "artifact_mismatch", "correct contents")
            await self.status(call, work["id"], "review")
            review = await self.task(call, verdict_goal, "fixture-b", "negative_review", "correct contents",
                                     kind="review", reviews_task=work["id"])
            reviewed = await self.status(call, review["id"], "review")
            verdict = json.loads(reviewed["result"])["result"]
            assert reviewed["outcome"] == "success" and verdict.startswith("REVIEW_REJECTED:")
            assert "RELAY_REVIEW_PASS" not in verdict
            assert self.read("worker-b", "/workspace/artifact.txt") != "correct contents"
            await call("task_accept", task_id=work["id"], review_task_id=review["id"], evidence="Successful execution with negative review is not acceptance", reject=True)
            await call("goal_set_state", goal_id=verdict_goal["id"], state="completed", evidence="A negative review must not be treated as goal completion", reject=True)
            await call("task_cancel", task_id=review["id"])
            await call("task_cancel", task_id=work["id"])
            await call("goal_set_state", goal_id=verdict_goal["id"], state="paused", evidence="Parent inspected the actual wrong artifact and negative review; neither was accepted")
            self.checked("successful_cli_negative_review_remains_unaccepted", semantic_decision_owner="operator/test oracle")

            board = await call("fleet_status")
            await call("goal_create", objective="Invalid budget", acceptance="must reject", authority="fixture only", max_tasks=0, reject=True)
            assert len((await call("fleet_status"))["goals"]) == len(board["goals"])
            budget_goal = await self.goal(call, "Enforce task and request budgets", 1)
            for limit in ({"max_turns": 0}, {"timeout_seconds": 9}):
                await self.task(call, budget_goal, "fixture-b", "work", "never", workspace="not-announced", reject=True, **limit)
            queued = await self.task(call, budget_goal, "fixture-b", "work", "never", workspace="not-announced")
            await self.task(call, budget_goal, "fixture-b", "work", "never", workspace="not-announced", reject=True)
            pending = await call("task_get", task_id=queued["id"])
            assert pending["status"] == "queued" and pending["attempt"] == 0
            await call("task_cancel", task_id=queued["id"])
            await call("goal_set_state", goal_id=budget_goal["id"], state="paused", evidence="Invalid limits and exhausted task budget were rejected without dispatch")
            self.checked("goal_task_budgets_and_invalid_limits_rejected")

            goal = await self.goal(call, "Unknown execution requires explicit recovery", 2)
            task = await self.task(call, goal, "fixture-a", "crash", "recovered artifact")
            await self.status(call, task["id"], "running")
            end = time.monotonic() + 10
            while not self.read("worker", "/workspace/.crash-started"):
                assert time.monotonic() < end
                await asyncio.sleep(.1)
            before = self.read("worker", "/home/relay/fixture-cli-count")
            old_cid = self.cid("worker")
            self.compose("stop", "--timeout", "0", "worker")
            assert self.command(["docker", "inspect", "--format", "{{.State.Running}}", old_cid]) == "false"
            quarantined = await self.status(call, task["id"], "reconcile", timeout=60)
            assert quarantined["attempt"] == 1
            await call("task_retry", task_id=task["id"], reject=True)
            self.checked("hard_crash_quarantined_without_retry", attempt=1, container_observed_stopped=True)
        return goal, task, before

    async def recovery(self, goal, task, before):
        self.compose("restart", "relay")
        self.compose("start", "worker")
        await self.ready()
        async with self.mcp() as call:
            state = await call("task_get", task_id=task["id"])
            assert state["status"] == "reconcile" and state["attempt"] == 1
            await asyncio.sleep(3)
            assert self.read("worker", "/home/relay/fixture-cli-count") == before
            assert self.read("worker", "/workspace/.crash-started") == "first invocation started"
            await call("task_reconcile", task_id=task["id"], stopped_and_inspected_evidence="Parent observed the old container stopped, then inspected the preserved workspace marker and unchanged invocation counter")
            await call("task_retry", task_id=task["id"])
            recovered = await self.review_and_accept(call, goal, task, "retry_work", "recovered artifact")
            assert recovered["attempt"] == 2 and self.read("worker", "/workspace/artifact.txt") == "recovered artifact"
            await call("goal_set_state", goal_id=goal["id"], state="completed", evidence="Explicit stopped-process/workspace reconciliation preceded retry; second attempt and fresh separate review passed")
            self.checked("quarantine_persists_restart_and_explicit_recovery", attempt=2, no_automatic_retry=True)
            counts = [self.read(s, "/home/relay/fixture-cli-count") for s in ("worker", "worker-b")]
            await asyncio.sleep(3)
            assert counts == [self.read(s, "/home/relay/fixture-cli-count") for s in ("worker", "worker-b")]
            self.checked("idle_polling_launches_no_cli", invocation_counters=counts)

    def cleanup(self):
        self.compose("down", "--volumes", "--remove-orphans", cleanup=True)
        for kind in ("container", "volume", "network"):
            args = ["docker", "ps", "-aq"] if kind == "container" else ["docker", kind, "ls", "-q"]
            assert not self.command(args + ["--filter", "label=com.docker.compose.project=" + self.project], cleanup=True)
        self.receipt["cleanup_verified"] = True

    def failure_context(self):
        try:
            board = httpx.get(self.url + "/snapshot", headers={"Authorization": "Bearer " + self.operator},
                              trust_env=False, timeout=4).raise_for_status().json()
            self.receipt["observed_tasks"] = [{k: t.get(k) for k in ("id", "kind", "node", "status", "attempt", "outcome", "reason")}
                                               for t in board["tasks"]]
        except Exception as exc:
            self.receipt["failure_context_error"] = type(exc).__name__


def error_leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for child in exc.exceptions for leaf in error_leaves(child)]
    return [type(exc).__name__ + ": " + str(exc)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the sanitized JSON receipt here")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Acceptance assertions require Python without -O/PYTHONOPTIMIZE")
    suite = Acceptance()
    output = args.output or ROOT / "tmp/lan-relay" / (suite.project + ".json")
    try:
        suite.setup()
        goal, task, before = asyncio.run(suite.main_cases())
        asyncio.run(suite.recovery(goal, task, before))
        suite.receipt["passed"] = True
    except BaseException as exc:
        suite.receipt.update(passed=False, errors=[suite.redact(text) for text in error_leaves(exc)])
        suite.failure_context()
    finally:
        try:
            suite.cleanup()
        except Exception as exc:
            suite.receipt.update(passed=False, cleanup_verified=False, cleanup_error=suite.redact(str(exc)))
        suite.receipt["elapsed_seconds"] = round(time.monotonic() - suite.started, 2)
        payload = suite.redact(json.dumps(suite.receipt, indent=2))
        archive = ROOT / "tmp/lan-relay" / (suite.project + ".json")
        for destination in {output.resolve(), archive.resolve()}:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(payload, encoding="utf-8")
        print(payload)
    return 0 if suite.receipt.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
