"""Run bounded real-Claude Relay acceptance in isolated native runner containers."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
VERIFY_SPEC = importlib.util.spec_from_file_location("relay_acceptance_verifier", ROOT / "scripts/verify_relay.py")
verify = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verify)
IMAGE = "subroute-relay-runner:0.1.0"


def _docker(args, *, env=None, timeout=20, check=True):
    result = subprocess.run(["docker", *args], cwd=ROOT, env=env, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("Docker command failed: " + " ".join(args[:3]))
    return result


def _trace_sizes(states):
    return {(str(path.parent), path.name): path.stat().st_size
            for state in states for path in state.rglob("*.jsonl")}


def _validate_persisted(url, operator, project, state_dirs):
    import httpx

    headers = {"Authorization": "Bearer " + operator}
    def snapshot():
        response = httpx.get(url + "/snapshot", headers=headers, timeout=10, trust_env=False)
        response.raise_for_status()
        return response.json()

    deadline = time.monotonic() + 30
    while True:
        try:
            first = snapshot()
            break
        except httpx.HTTPError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.5)
    goals = first.get("goals", [])
    accepted = [task for task in first.get("tasks", []) if task.get("status") == "accepted"]
    if len(goals) != 1 or goals[0].get("status") != "completed" or len(accepted) != 4:
        raise RuntimeError("Restarted hub did not retain one completed goal and four accepted tasks")
    sizes = _trace_sizes(state_dirs)
    for _ in range(5):
        for node in ("docker-a", "docker-b"):
            running = _docker(["inspect", "--format", "{{.State.Running}}", project + "-" + node])
            if running.stdout.strip() != b"true":
                raise RuntimeError("Idle observation requires both real runners still running")
        time.sleep(1)
        current = snapshot()
        if current.get("revision") != first.get("revision"):
            raise RuntimeError("Idle hub polling changed the completed task snapshot")
    if _trace_sizes(state_dirs) != sizes:
        raise RuntimeError("Idle polling created or changed a native Claude session trace")
    return {"restart_snapshot_completed": True, "accepted_tasks": len(accepted),
            "idle_poll_seconds": 5, "idle_trace_sizes_stable": True}


def _owned_cleanup(project, label, env, volumes):
    errors = []
    containers = _docker(["ps", "-aq", "--filter", "label=" + label], timeout=15, check=False)
    if containers.returncode == 0:
        for container in containers.stdout.decode().splitlines():
            inspected = _docker(["inspect", "--format", "{{ index .Config.Labels \"" + label.split("=", 1)[0] + "\" }}", container], timeout=10, check=False)
            if inspected.returncode == 0 and inspected.stdout.decode().strip() == label.split("=", 1)[1]:
                removed = _docker(["rm", "-f", container], timeout=15, check=False)
                if removed.returncode:
                    errors.append("owned container removal failed")
    else:
        errors.append("owned container enumeration failed")
    for volume in volumes:
        inspected = _docker(["volume", "inspect", "--format", "{{ index .Labels \"" + label.split("=", 1)[0] + "\" }}", volume], timeout=10, check=False)
        if inspected.returncode == 0 and inspected.stdout.decode().strip() == label.split("=", 1)[1]:
            removed = _docker(["volume", "rm", volume], timeout=15, check=False)
            if removed.returncode:
                errors.append("owned volume removal failed")
    compose = _docker(["compose", "-f", str(ROOT / "docker-compose.relay.yml"), "-p", project,
                       "down", "--volumes", "--remove-orphans"], env=env, timeout=30, check=False)
    if compose.returncode:
        errors.append("owned Compose project cleanup failed")
    remaining = _docker(["ps", "-aq", "--filter", "label=" + label], timeout=15, check=False)
    if remaining.returncode or remaining.stdout.strip():
        errors.append("owned containers remain")
    for volume in volumes:
        still_exists = _docker(["volume", "inspect", volume], timeout=10, check=False)
        if still_exists.returncode == 0:
            errors.append("owned volume remains")
    for kind in ("container", "volume", "network"):
        args = ["ps", "-aq"] if kind == "container" else [kind, "ls", "-q"]
        remaining = _docker(args + ["--filter", "label=com.docker.compose.project=" + project],
                            timeout=15, check=False)
        if remaining.returncode or remaining.stdout.strip():
            errors.append("owned Compose " + kind + " resources remain or could not be checked")
    return {"complete": not errors, "errors": errors}


def _round(index):
    project = "relay-live-docker-" + uuid4().hex[:12]
    label = "org.subroute.relay-live-round=" + project
    scratch = ROOT / "tmp" / "lan-relay" / project
    scratch.mkdir(parents=True)
    operator = secrets.token_urlsafe(36)
    keys = {"docker-a": secrets.token_urlsafe(36), "docker-b": secrets.token_urlsafe(36)}
    volumes = [project + "-" + node + "-home" for node in keys]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, RELAY_OPERATOR_KEY=operator, RELAY_NODE_KEYS=json.dumps(keys),
               RELAY_PORT=str(port), RELAY_PUBLISH_IP="127.0.0.1",
               RELAY_ALLOWED_HOSTS="127.0.0.1:*,relay:*", RELAY_ADVISOR_KEY="", RELAY_ADVISOR_DAILY_LIMIT="0")
    logs = []
    state_dirs = []
    result = {"round": index, "project": project, "mode": "real Claude in isolated Docker runners",
              "cross_device": False, "sustained": False, "physical_devices": 1}

    def compose(*args):
        completed = _docker(["compose", "-f", str(ROOT / "docker-compose.relay.yml"), "-p", project,
                             *args], env=env, timeout=60)
        if completed.returncode:
            raise RuntimeError("Owned Relay Compose operation failed")

    def start_runner(_command, **kwargs):
        config_path = Path(_command[-1])
        config = json.loads(config_path.read_text(encoding="utf-8"))
        node = config["node"]
        workspace = Path(config["workspaces"]["live"])
        state = scratch / (node + "-state")
        home_volume = project + "-" + node + "-home"
        state.mkdir(parents=True, exist_ok=True)
        state_dirs.append(state)
        config.update(url="http://relay:8765", gateway_url="http://host.docker.internal:4000",
                      state_dir="/state", workspaces={"live": "/workspace"})
        config_path.write_text(json.dumps(config), encoding="utf-8")
        volume_result = _docker(["volume", "create", "--label", label, home_volume], timeout=15)
        if volume_result.returncode == 0 and volume_result.stdout.decode().strip() != home_volume:
            raise RuntimeError("Docker returned an unexpected private home volume")
        name = project + "-" + node
        log = (scratch / (node + ".container.log")).open("wb")
        logs.append(log)
        docker_args = ["run", "--name", name, "--label", label, "--network", project + "_default",
            "--add-host", "host.docker.internal:host-gateway", "--init", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:size=64m,nosuid",
            "--memory", "1g", "--cpus", "1", "--env", "RELAY_NODE_KEY", "--env", "GATEWAY_MASTER_KEY",
            "--mount", f"type=volume,source={home_volume},target=/home/relay",
            "--mount", f"type=bind,source={state},target=/state",
            "--mount", f"type=bind,source={workspace},target=/workspace",
            "--mount", f"type=bind,source={config_path},target=/config/runner.json,readonly", IMAGE]
        runner_env = kwargs["env"]
        return subprocess.Popen(["docker", *docker_args], cwd=ROOT, env=runner_env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

    try:
        compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "40")
        url = f"http://127.0.0.1:{port}"
        started = time.monotonic()
        receipt = asyncio.run(verify.verify_live(url, operator, keys, scratch, compose, start_runner))
        receipt["wall_time_seconds"] = round(time.monotonic() - started, 2)
        compose("restart", "relay")
        receipt["persistence_and_idle"] = _validate_persisted(url, operator, project, state_dirs)
        receipt["provider_usage"] = [task.get("usage") for task in receipt["tasks"]]
        result.update(receipt)
    except BaseException as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
        result["errors"] = verify.failure_causes(exc)
        failure = scratch.parent / (scratch.name + "-failure.json")
        result["failure_evidence"] = str(failure) if failure.exists() else None
        for node in keys:
            log_path = scratch / (node + ".container.log")
            container_logs = _docker(["logs", project + "-" + node], timeout=10, check=False)
            if container_logs.stdout or container_logs.stderr:
                log_path.write_bytes(container_logs.stdout + container_logs.stderr)
    finally:
        for log in logs:
            log.close()
        try:
            result["cleanup"] = _owned_cleanup(project, label, env, volumes)
        except Exception as exc:
            result["cleanup"] = {"complete": False, "errors": ["cleanup raised " + type(exc).__name__]}
    for secret in (operator, *keys.values(), os.environ.get("GATEWAY_MASTER_KEY", "")):
        if secret:
            result = json.loads(json.dumps(result).replace(secret, "[redacted]"))
    (scratch / "receipt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, choices=(1, 2), default=1)
    parser.add_argument("--output", type=Path, help="Optional aggregate JSON output path")
    args = parser.parse_args()
    if not os.environ.get("GATEWAY_MASTER_KEY"):
        parser.error("GATEWAY_MASTER_KEY must be present in the process environment")
    receipts = []
    for index in range(1, args.rounds + 1):
        receipt = _round(index)
        receipts.append(receipt)
        if receipt.get("error") or not receipt.get("cleanup", {}).get("complete"):
            break
    summary = {"rounds_requested": args.rounds, "rounds_run": len(receipts), "rounds": receipts,
               "all_passed": len(receipts) == args.rounds and all(
                   not item.get("error") and item.get("accepted_tasks") == 4 and item.get("cleanup", {}).get("complete")
                   for item in receipts)}
    encoded = json.dumps(summary, indent=2)
    for secret in (os.environ.get("GATEWAY_MASTER_KEY", ""),):
        if secret:
            encoded = encoded.replace(secret, "[redacted]")
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
