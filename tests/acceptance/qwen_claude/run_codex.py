"""Run native Codex against the same isolated fixtures as Claude Code."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import httpx

from run import (BASE_URL, FIXTURES, MODEL, ROOT, TASKS, fixture_files, gateway_key,
                 holdout_tests, set_policy, unit_tests)
from incident_fixture import complete_incident_intake, make_incident_evidence


def command() -> list[str]:
    executable = Path(os.environ["APPDATA"]) / "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    if not executable.is_file():
        raise RuntimeError(f"Codex executable missing: {executable}")
    settings = {
        "model_provider": "sift_eval", "model_context_window": 1_000_000,
        "model_auto_compact_token_limit": 950_000, "approval_policy": "never",
        "web_search": "disabled", "model_reasoning_effort": "low",
        "features.apps": False, "features.plugins": False, "features.multi_agent": False,
        "skills.max_context_tokens": 512,
        # Match this host's configured native sandbox backend while ignoring
        # unrelated user provider/plugin settings. This retains workspace scope.
        "windows.sandbox": "elevated",
        "shell_environment_policy.exclude": ["SIFT_EVAL_GATEWAY_KEY"],
        "model_providers.sift_eval.name": "Subroute local Qwen staging",
        "model_providers.sift_eval.base_url": BASE_URL + "/v1",
        "model_providers.sift_eval.env_key": "SIFT_EVAL_GATEWAY_KEY",
        "model_providers.sift_eval.wire_api": "responses",
        "model_providers.sift_eval.requires_openai_auth": False,
        "model_providers.sift_eval.supports_websockets": False,
        "model_providers.sift_eval.request_max_retries": 0,
        "model_providers.sift_eval.stream_max_retries": 0,
    }
    output = [str(executable), "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
              "--json", "--color", "never", "--skip-git-repo-check", "--sandbox", "workspace-write", "--model", MODEL]
    for key, value in settings.items():
        output.extend(["-c", f"{key}={json.dumps(value)}"])
    return [*output, "-"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=[name for name in TASKS if name != "billing_ledger_stress"], default="build_planner")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--smoke-tools", action="store_true")
    args = parser.parse_args()
    args.smoke = args.smoke or args.smoke_tools
    run_root = FIXTURES / ".runs"
    run_root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="subroute-qwen-codex-", dir=run_root))
    if args.smoke:
        prompt = ("Run exactly one exec_command PowerShell command: Write-Output 'SIFT_CODEX_READY'. "
                  "If the command succeeds, reply exactly SIFT_CODEX_READY. If it is denied, report that once and stop.") \
                 if args.smoke_tools else "Reply with exactly SIFT_CODEX_READY. Do not run tools."
        before_files = {}
    else:
        shutil.copytree(FIXTURES / args.task, directory, dirs_exist_ok=True)
        if args.task == "incident_reconstruction":
            make_incident_evidence(directory)
        prompt = TASKS[args.task][1]
        assert unit_tests(directory).returncode != 0, "fixture already passes"
        before_files = fixture_files(directory)
    key = gateway_key()
    client_version = subprocess.run([command()[0], "--version"], capture_output=True,
                                    text=True, timeout=10, check=True).stdout.strip()
    env = {name: value for name, value in os.environ.items() if name not in {"OPENAI_API_KEY", "OPENAI_BASE_URL"}}
    env["SIFT_EVAL_GATEWAY_KEY"] = key
    with httpx.Client(base_url=BASE_URL, headers={"Authorization": "Bearer " + key}, timeout=30) as client:
        client.get("/health/readiness").raise_for_status()
        before = client.get("/api/active-model").json()
        start_utc = datetime.now(timezone.utc).isoformat()
        start = time.perf_counter()
        try:
            set_policy(client, before, test=True)
            try:
                result = subprocess.run(command(), input=prompt, cwd=directory, env=env,
                                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=args.timeout)
                stdout, stderr, exit_code = result.stdout, result.stderr, result.returncode
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else error.stdout or ""
                stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else error.stderr or ""
                exit_code = 124
        finally:
            set_policy(client, before, test=False)
            after = client.get("/api/active-model").json()
            fields = ("active_model", "mode", "reasoning_effort", "fast_mode", "advisor_model",
                      "advisor_reasoning_effort", "advisor_trigger", "advisor_sensitivity", "advisor_classifier")
            assert all(before.get(field) == after.get(field) for field in fields), "staging policy not restored"
    seconds = round(time.perf_counter() - start, 2)
    end_utc = datetime.now(timezone.utc).isoformat()
    events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    completed = any(event.get("type") == "turn.completed" for event in events)
    commands = [event.get("item", {}) for event in events if event.get("type") == "item.completed"
                and event.get("item", {}).get("type") == "command_execution"]
    final = "\n".join(event.get("item", {}).get("text", "") for event in events
                      if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message")
    logs = subprocess.run(["docker", "logs", "--since", start_utc, "--until", end_utc, "subroute-qwen-local-1"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    timings = [(float(ms), int(tokens)) for ms, tokens in re.findall(
        r"(?<!prompt )eval time\s*=\s*([\d.]+) ms /\s*(\d+) tokens", logs.stdout + logs.stderr)]
    gateway_logs = subprocess.run(["docker", "logs", "--since", start_utc, "--until", end_utc, "subroute-gateway-staging-1"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    audits = [{"protocol": protocol, "compacted": compacted == "true", "input_tokens": int(raw), "forwarded_tokens": int(forwarded), "target": int(target), "output_limit": int(output_limit)}
              for protocol, compacted, raw, forwarded, target, output_limit in re.findall(
                  r"sift_request protocol=(\w+) compacted=(true|false) [^\r\n]*?input_tokens=(\d+) forwarded_tokens=(\d+) target=(\d+) output_limit=(-?\d+)",
                  gateway_logs.stdout + gateway_logs.stderr) if int(target) == 24_576]
    outcome = {"client": "codex", "model": MODEL, "task": args.task, "smoke": args.smoke,
               "client_version": client_version,
               "seconds": seconds, "exit_code": exit_code, "turn_completed": completed,
               "final_answer": final[-2000:], "commands": commands, "sift_requests": audits,
               "sift_compactions": [audit for audit in audits if audit["compacted"]],
               "start_utc": start_utc, "end_utc": end_utc,
               "mean_ttft_ms": None, "ttft_note": "codex exec JSONL does not expose per-response first-token timing",
               "server_response_count": len(timings),
               "mean_server_tokens_per_second": round(sum(tokens for ms, tokens in timings) / (sum(ms for ms, tokens in timings) / 1000), 2) if timings else None,
               "usage": [event.get("usage") for event in events if event.get("type") == "turn.completed"],
               "original_policy_restored": True}
    if args.smoke:
        accepted = exit_code == 0 and completed and "SIFT_CODEX_READY" in final
        if args.smoke_tools:
            accepted = accepted and bool(commands) and all(item.get("exit_code") == 0 for item in commands)
    else:
        visible = unit_tests(directory)
        holdout = holdout_tests(args.task, directory)
        after_files = fixture_files(directory)
        source_names = TASKS[args.task][0]
        source_names = (source_names,) if isinstance(source_names, str) else source_names
        changed = [name for name in source_names if before_files[name] != after_files.get(name)]
        unauthorized = sorted(name for name, value in before_files.items() if name not in source_names and after_files.get(name) != value)
        extra = sorted(after_files.keys() - before_files.keys())
        tests = [item for item in commands if "python -m unittest" in item.get("command", "")]
        loop = any("FAILED" in item.get("aggregated_output", "") for item in tests) and any(
            re.search(r"(?m)^OK\s*$", item.get("aggregated_output", "")) and item.get("exit_code") == 0 for item in tests)
        outcome.update({"source_changed": changed, "unauthorized_changes": unauthorized, "extra_files": extra,
                        "visible_passed": visible.returncode == 0, "holdout_passed": holdout.returncode == 0 if holdout else None,
                        "visible_output": (visible.stdout + visible.stderr)[-2000:],
                        "holdout_output": (holdout.stdout + holdout.stderr)[-2000:] if holdout else None, "test_loop": loop})
        accepted = exit_code == 0 and completed and bool(changed) and not unauthorized and not extra and loop and visible.returncode == 0 and (holdout is None or holdout.returncode == 0)
        if args.task == "incident_reconstruction":
            reads = []
            for event in events:
                if event.get("type") != "item.completed":
                    continue
                item = event.get("item", {})
                if item.get("type") == "file_change":
                    break
                if item.get("type") != "command_execution":
                    continue
                if "findings.json" in item.get("command", "") and re.search(
                    r"Set-Content|Out-File|WriteAllText|write_text|write_bytes|>\s*['\"]?findings", item["command"], re.I
                ):
                    break
                command_text = item.get("command", "")
                # Accept ordinary PowerShell path flags and absolute paths;
                # the complete returned source lines establish the intake.
                matches = re.findall(r"incident_[1-4]\.jsonl", command_text, re.I) \
                    if re.search(r"\bGet-Content\b", command_text, re.I) else []
                if len(matches) == 1 and item.get("exit_code") == 0:
                    reads.append((matches[0].lower(), item.get("aggregated_output", "")))
            outcome["complete_incident_intake"] = complete_incident_intake(directory, reads)
            accepted = accepted and outcome["complete_incident_intake"]
    outcome["accepted"] = accepted
    (directory / "codex.jsonl").write_text(stdout.replace(key, "[REDACTED]"), encoding="utf-8")
    (directory / "codex.stderr").write_text(stderr.replace(key, "[REDACTED]"), encoding="utf-8")
    serialized = json.dumps(outcome, indent=2).replace(key, "[REDACTED]")
    (directory / "result.json").write_text(serialized, encoding="utf-8")
    print(serialized, flush=True)
    print("evidence", directory, flush=True)
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
