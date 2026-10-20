"""Live Responses compaction, terminal-outcome and prefix-cache probe."""

from __future__ import annotations

import argparse
import json
import time

import httpx

from run import BASE_URL, MODEL, gateway_key, set_policy


def request(client: httpx.Client, payload: dict) -> dict:
    start = time.perf_counter()
    first_token = first_text = None
    text = ""
    terminal = None
    body = {}
    with client.stream("POST", "/v1/responses", json=payload) as response:
        response.raise_for_status()
        headers = {key: value for key, value in response.headers.items() if key.startswith("x-sift-")}
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                continue
            event = json.loads(raw)
            kind = event.get("type", "")
            if kind in {"response.output_text.delta", "response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                now = round((time.perf_counter() - start) * 1000, 1)
                if first_token is None:
                    first_token = now
                if kind == "response.output_text.delta":
                    if first_text is None:
                        first_text = now
                    text += event.get("delta", "")
            if kind in {"response.completed", "response.failed", "response.incomplete", "error"}:
                terminal = kind
                body = event.get("response") or event
    usage = body.get("usage") or {}
    return {"seconds": round(time.perf_counter() - start, 2), "first_token_ms": first_token,
            "first_text_ms": first_text, "terminal": terminal, "status": body.get("status"),
            "answer": text, "usage": usage, "sift": headers}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask", choices=("newest", "earliest"), default="newest")
    parser.add_argument("--short", action="store_true")
    args = parser.parse_args()
    items = [{"role": "user", "content": f"Audit the incremental cache probe and report the {args.ask} FAILED test."}]
    for index in range(36):
        items.extend([
            {"type": "function_call", "call_id": f"cache_probe_{index}", "name": "Read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"cache_probe_{index}",
             "output": (f"Diagnostic cache probe {index} line. " * 280) + f"\nFAILED: test_cache_probe_{index}"},
        ])
    rows = []
    with httpx.Client(base_url=BASE_URL, headers={"Authorization": "Bearer " + gateway_key()}, timeout=180) as client:
        before = client.get("/api/active-model").json()
        try:
            set_policy(client, before, test=True)
            for cycles in ((0,) if args.short else (34, 35, 36)):
                payload = {"model": MODEL, "instructions": f"Reply with only the {args.ask} FAILED test name.",
                           "input": items[:1 + 2 * cycles], "max_output_tokens": 768, "store": False, "stream": True}
                if args.short:
                    payload["input"] = "Reply with exactly SIFT_RESPONSES_READY"
                    payload["instructions"] = "Follow the user request."
                row = {"cycles": cycles, **request(client, payload)}
                rows.append(row)
                print(json.dumps(row), flush=True)
        finally:
            set_policy(client, before, test=False)
            after = client.get("/api/active-model").json()
            fields = ("active_model", "mode", "reasoning_effort", "fast_mode", "advisor_model",
                      "advisor_reasoning_effort", "advisor_trigger", "advisor_sensitivity", "advisor_classifier")
            assert all(before.get(field) == after.get(field) for field in fields)
    return 0 if all(
        row["terminal"] == "response.completed" and row["status"] == "completed"
        and ("SIFT_RESPONSES_READY" if args.short else f"test_cache_probe_{row['cycles'] - 1 if args.ask == 'newest' else 0}") in row["answer"]
        and int(row["sift"].get("x-sift-estimated-forwarded-tokens", "999999")) <= 24_576
        for row in rows
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
