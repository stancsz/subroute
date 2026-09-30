"""Opt-in, bounded live MCP matrix; optional one-time refusal recovery experiment."""

import argparse
import asyncio
from datetime import timedelta
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def observe(session, case):
    started = time.monotonic()
    try:
        result = await session.call_tool(case["tool"], case["arguments"],
            read_timeout_seconds=timedelta(seconds=175))
        row = {"is_error": result.isError, "structured_content": result.structuredContent,
               "text": [item.text for item in result.content if item.type == "text"]}
    except Exception as exc:
        row = {"is_error": True, "client_error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    row["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return row


async def observe_case(session, case, refusal_recheck_once=False, on_attempt=None):
    first = await observe(session, case)
    if on_attempt:
        on_attempt(first)
    content = first.get("structured_content") or {}
    attempts = [first]
    if (refusal_recheck_once and not case.get("expected_error") and first["is_error"]
            and content.get("status") == "refused"
            and content.get("error", {}).get("code") == "provider_content_filter"):
        # Explicit diagnostic experiment, not a production retry policy. Never
        # change the task or suppress the first refusal to obtain a passing result.
        print(json.dumps({"case": case["case"], "event": "explicit_refusal_recheck", "max_attempts": 2}), flush=True)
        await asyncio.sleep(1)
        attempts.append(await observe(session, case))
        if on_attempt:
            on_attempt(attempts[-1])
    final = dict(attempts[-1])
    if refusal_recheck_once:
        final["attempts"] = attempts
        final["elapsed_seconds"] = round(sum(row["elapsed_seconds"] for row in attempts) + (1 if len(attempts) == 2 else 0), 3)
        if len(attempts) == 2:
            final["recheck_inputs_match"] = bool(content.get("inputs")) and content["inputs"] == (attempts[-1].get("structured_content") or {}).get("inputs")
            if not final["recheck_inputs_match"]:
                final["is_error"] = True
                final["client_error"] = "Audio input receipts changed between attempts; recovery is not established."
    return final


def summarize(rows):
    attempts = [attempt for row in rows for attempt in row.get("attempts", [row])]
    complete = lambda row: not row["is_error"] and (row.get("structured_content") or {}).get("status") == "complete"
    eligible = [row for row in rows if not row.get("expected_error")]
    first = [row.get("attempts", [row])[0] for row in eligible]
    initial_failures = sum(not complete(row) for row in first)
    recovered = sum(not complete(initial) and complete(final) for initial, final in zip(first, eligible))
    return {"calls": len(attempts), "tasks": len(rows), "eligible_tasks": len(eligible),
            "errors": sum(row["is_error"] for row in attempts),
            "unexpected_outcomes": sum(not row["expected_outcome_matches"] for row in rows),
            "first_attempt_complete": sum(complete(row) for row in first),
            "final_complete": sum(complete(row) for row in rows), "recovered_tasks": recovered,
            "initial_failures": initial_failures,
            "observed_final_success_rate": sum(complete(row) for row in eligible) / len(eligible) if eligible else None,
            "conditional_recovery_rate": recovered / initial_failures if initial_failures else None,
            "known_provider_tokens": sum((row.get("structured_content") or {}).get("usage", {}).get("total_tokens", 0)
                for row in attempts if (row.get("structured_content") or {}).get("usage")),
            "provider_calls_with_unknown_usage": sum(
                not (row.get("structured_content") or {}).get("usage")
                and (row.get("structured_content") or {}).get("error", {}).get("code") != "invalid_input"
                for row in attempts),
            "failed_usage": "Includes all observed attempts when available; unknown is not zero. CLI internal calls are not counted.",
            "latency_seconds": sorted(row["elapsed_seconds"] for row in rows)}


async def run(args):
    cases = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
        raise ValueError("manifest must contain 1-64 explicit cases")
    report = {"gateway": args.gateway_url, "root": str(args.root.resolve()),
              "mcp_version": importlib.metadata.version("mcp"), "cases": [],
              "scope": "Live transport and refusal observations; not calibrated music-quality certification.",
              "automatic_retry": args.refusal_recheck_once, "production_retry_policy_changed": False,
              "concurrency": args.concurrency,
              "explicit_refusal_recheck_once": args.refusal_recheck_once,
              "max_tool_calls": len(cases) * (2 if args.refusal_recheck_once else 1)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.refusal_recheck_once:
        report["attempt_ledger"] = []
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    async with httpx.AsyncClient(timeout=10) as client:
        report["policy_before"] = (await client.get(args.gateway_url + "/api/active-model")).json()
    repo = Path(__file__).resolve().parents[1]
    env = {"PYTHONPATH": str(repo / "src")}
    if os.getenv("SUBROUTE_API_KEY"):
        env["SUBROUTE_API_KEY"] = os.environ["SUBROUTE_API_KEY"]
    params = StdioServerParameters(command=sys.executable, args=["-m", "subroute.music_mcp",
        "--root", str(args.root.resolve()), "--gateway-url", args.gateway_url], cwd=str(repo), env=env)
    slots = asyncio.Semaphore(args.concurrency)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                report["tools"] = [tool.name for tool in (await session.list_tools()).tools]
                async def call(index, case):
                    async with slots:
                        row = {"index": index, **case}
                        def record_attempt(attempt):
                            content = attempt.get("structured_content") or {}
                            report["attempt_ledger"].append({"index": index, "case": case["case"],
                                "is_error": attempt["is_error"], "elapsed_seconds": attempt["elapsed_seconds"],
                                "status": content.get("status"), "error": content.get("error"),
                                "inputs": content.get("inputs"), "usage": content.get("usage"),
                                "client_error": attempt.get("client_error")})
                            save()
                        row.update(await observe_case(session, case, args.refusal_recheck_once,
                            record_attempt if args.refusal_recheck_once else None))
                        content = row.get("structured_content") or {}
                        expected_error = case.get("expected_error")
                        row["expected_outcome_matches"] = (
                            row["is_error"] and content.get("error", {}).get("code") == expected_error
                            if expected_error else not row["is_error"] and content.get("status") == "complete")
                        report["cases"].append(row)
                        save()
                        print(json.dumps({"index": index, "case": case["case"], "seconds": row["elapsed_seconds"],
                            "status": content.get("status"), "code": content.get("error", {}).get("code"),
                            "is_error": row["is_error"]}, ensure_ascii=True), flush=True)
                await asyncio.gather(*(call(index, case) for index, case in enumerate(cases)))
    finally:
        async with httpx.AsyncClient(timeout=10) as client:
            report["policy_after"] = (await client.get(args.gateway_url + "/api/active-model")).json()
            report["readiness"] = (await client.get(args.gateway_url + "/health/readiness")).status_code
        report["policy_unchanged"] = report["policy_after"] == report["policy_before"]
        rows = report["cases"]
        report["summary"] = summarize(rows)
        report["cases"].sort(key=lambda row: row["index"])
        save()
    print(str(args.output), flush=True)
    return 1 if report["summary"]["unexpected_outcomes"] or not report["policy_unchanged"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly authorize the listed Subscription calls")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:4000")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument("--refusal-recheck-once", action="store_true",
                        help="Explicit experiment: one unchanged recheck after a filter refusal, retain both attempts; does not change production retries")
    args = parser.parse_args()
    if not args.live:
        parser.error("Live provider requests require --live")
    sys.exit(asyncio.run(run(args)))
