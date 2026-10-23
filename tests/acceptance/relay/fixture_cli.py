#!/usr/bin/env python3
"""Deterministic Claude CLI stand-in used only by the isolated Docker suite."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def assignment(prompt: str) -> dict:
    marker = '\n{"objective":'
    start = prompt.rfind(marker)
    if start < 0:
        raise ValueError("assignment wrapper missing")
    wrapper, _end = json.JSONDecoder().raw_decode(prompt[start + 1:])
    value = json.loads(wrapper["assignment"])
    if not isinstance(value, dict) or set(value) != {"scenario", "expected"}:
        raise ValueError("unsupported fixture assignment")
    return value


def counter(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = int(path.read_text(encoding="ascii"))
    except FileNotFoundError:
        value = 0
    path.write_text(str(value + 1), encoding="ascii")


def event(result: str, subtype: str = "success", error: bool = False) -> None:
    print(json.dumps({"type": "result", "subtype": subtype, "is_error": error,
                      "result": result, "session_id": "fixture-session",
                      "usage": {"input_tokens": 0, "output_tokens": 0},
                      "total_cost_usd": 0, "num_turns": 1}), flush=True)


def main() -> int:
    prompt = sys.stdin.read()
    spec = assignment(prompt)
    counter(Path(os.environ["FIXTURE_COUNTER_PATH"]))
    workspace = Path.cwd()
    local_count = workspace / ".fixture-cli-count"
    try:
        count = int(local_count.read_text(encoding="ascii"))
    except FileNotFoundError:
        count = 0
    local_count.write_text(str(count + 1), encoding="ascii")

    scenario, expected = spec["scenario"], spec["expected"]
    (workspace / "launch.json").write_text(json.dumps({
        "model": sys.argv[sys.argv.index("--model") + 1],
        "max_turns": int(sys.argv[sys.argv.index("--max-turns") + 1]),
        "budget": float(sys.argv[sys.argv.index("--max-budget-usd") + 1]),
        "operator_key_present": "RELAY_OPERATOR_KEY" in os.environ,
        "node_key_present": "RELAY_NODE_KEY" in os.environ,
        "gateway_master_key_present": "GATEWAY_MASTER_KEY" in os.environ,
        "dummy_inference_key": os.environ.get("ANTHROPIC_API_KEY") == "fixture-inference-not-a-key",
        "local_guidance_present": "Relay acceptance fixture contract" in prompt,
    }), encoding="utf-8")
    if scenario == "descendant":
        code = ("import pathlib,sys,time; p=pathlib.Path('descendant.heartbeat'); "
                "pathlib.Path('descendant.pid').write_text(str(__import__('os').getpid())); "
                "exec('while True:\\n p.write_text(str(time.time_ns()))\\n time.sleep(.1)')")
        child = subprocess.Popen([sys.executable, "-c", code], cwd=workspace,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        (workspace / "descendant.pid").write_text(str(child.pid), encoding="ascii")
        while True:
            time.sleep(1)
    if scenario == "crash":
        marker = workspace / ".crash-started"
        if not marker.exists():
            marker.write_text("first invocation started", encoding="ascii")
            while True:
                time.sleep(1)
        scenario = "retry_work"
    if scenario == "bad_terminal":
        print("not-json", flush=True)
        return 0
    if scenario == "large_terminal":
        event(('"\\\n😀' * 10000))
        return 0
    if scenario == "negative_review":
        content = (workspace / "artifact.txt").read_text(encoding="utf-8")
        assert content != expected
        event("REVIEW_REJECTED: actual artifact differs from the required contents")
        return 0  # A successful review session can still report a failing verdict.

    artifact = workspace / "artifact.txt"
    if scenario in {"work", "retry_work"} and "Independent review only." not in prompt:
        if scenario == "work":
            (workspace / "work.started").write_text(str(time.monotonic_ns()), encoding="ascii")
            while not (workspace / "work.release").exists():
                time.sleep(.05)
        artifact.write_text(expected, encoding="utf-8")
        check = subprocess.run([sys.executable, "-c", "from pathlib import Path; import sys; assert Path('artifact.txt').read_text() == sys.argv[1]", expected],
                               cwd=workspace, capture_output=True, timeout=5)
        if check.returncode:
            event("ARTIFACT_CHECK_FAILED", subtype="error", error=True)
            return 1
        if scenario == "retry_work" and local_count.exists() and int(local_count.read_text()) >= 2:
            event("RETRY_ARTIFACT_OK")
        else:
            event("ARTIFACT_OK")
        return 0
    if scenario in {"work", "retry_work", "artifact_mismatch"}:
        content = artifact.read_text(encoding="utf-8") if artifact.is_file() else ""
        if scenario == "artifact_mismatch" and "Independent review only." not in prompt:
            artifact.write_text("wrong fixture content", encoding="utf-8")
            event("ARTIFACT_WRITTEN")
            return 0
        if content != expected:
            event("REVIEW_REJECTED: artifact mismatch", subtype="error", error=True)
            return 0
        marker = "RELAY_REVIEW_PASS" if "Independent review only." in prompt else "ARTIFACT_OK"
        event(marker)
        return 0
    event("FIXTURE_SCENARIO_UNSUPPORTED", subtype="error", error=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Never echo prompts, assignments, credentials, or environment values.
        print(json.dumps({"type": "result", "subtype": "fixture_error", "is_error": True,
                          "result": "fixture rejected its bounded assignment"}), flush=True)
        raise SystemExit(1)
