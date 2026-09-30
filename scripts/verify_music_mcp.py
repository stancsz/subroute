"""Opt-in, bounded live MCP listening matrix. Each case is one explicit tool call."""

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


async def run(args):
    cases = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
        raise ValueError("manifest must contain 1-64 explicit cases")
    report = {"gateway": args.gateway_url, "root": str(args.root.resolve()),
              "mcp_version": importlib.metadata.version("mcp"), "cases": [],
              "scope": "Live transport and refusal observations; not calibrated music-quality certification.",
              "automatic_retry": False, "concurrency": args.concurrency}
    args.output.parent.mkdir(parents=True, exist_ok=True)
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
                        started = time.monotonic()
                        row = {"index": index, **case}
                        try:
                            result = await session.call_tool(case["tool"], case["arguments"],
                                read_timeout_seconds=timedelta(seconds=175))
                            row.update(is_error=result.isError, structured_content=result.structuredContent,
                                       text=[item.text for item in result.content if item.type == "text"])
                        except Exception as exc:
                            row.update(is_error=True, client_error=f"{type(exc).__name__}: {str(exc)[:500]}")
                        row["elapsed_seconds"] = round(time.monotonic() - started, 3)
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
        report["summary"] = {"calls": len(rows), "errors": sum(row["is_error"] for row in rows),
            "unexpected_outcomes": sum(not row["expected_outcome_matches"] for row in rows),
            "known_provider_tokens": sum((row.get("structured_content") or {}).get("usage", {}).get("total_tokens", 0)
                for row in rows if (row.get("structured_content") or {}).get("usage")),
            "provider_calls_with_unknown_usage": sum(
                not (row.get("structured_content") or {}).get("usage")
                and (row.get("structured_content") or {}).get("error", {}).get("code") != "invalid_input"
                for row in rows),
            "failed_usage": "Included when the bridge reported valid usage; missing usage is unknown, not zero.",
            "latency_seconds": sorted(row["elapsed_seconds"] for row in rows)}
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
    args = parser.parse_args()
    if not args.live:
        parser.error("Live provider requests require --live")
    sys.exit(asyncio.run(run(args)))
