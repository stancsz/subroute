"""Check a buried fact through both staging protocol surfaces."""

from __future__ import annotations

import argparse
import json
import time

import httpx

from run import BASE_URL, MODEL, gateway_key, set_policy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lines", type=int, default=6000)
    parser.add_argument("--protocol", choices=("anthropic", "openai", "both"), default="both")
    parser.add_argument("--tool-array-without-items", action="store_true")
    parser.add_argument("--paired-diagnostic", action="store_true")
    parser.add_argument("--identifier-join", action="store_true")
    args = parser.parse_args()
    if args.lines < 4:
        parser.error("--lines must be at least 4")
    if args.identifier_join and args.lines < 12:
        parser.error("--identifier-join needs at least 12 lines")
    if args.tool_array_without_items and args.protocol != "openai":
        parser.error("--tool-array-without-items requires --protocol openai")
    if args.paired_diagnostic and args.identifier_join:
        parser.error("choose one evidence scenario")
    lines = ([f"webhook d-42 heartbeat seq={index} normal" for index in range(args.lines)]
             if args.identifier_join else
             [f"Build trace worker={index} status=ok" for index in range(args.lines)])
    if args.identifier_join:
        lines[args.lines // 6] = "webhook d-42 assigned policy P7 (superseded)"
        lines[args.lines // 3] = "policy P7 retry_after 2026-10-15T18:00Z"
        lines[2 * args.lines // 3] = "webhook d-42 assigned policy P8 (current)"
        lines[5 * args.lines // 6] = "policy P8 retry_after 2026-10-16T18:00Z"
        question = "According to the current policy, when may webhook d-42 retry? Return the timestamp only."
        expected_answer = "2026-10-16T18:00Z"
    elif args.paired_diagnostic:
        lines[len(lines) // 2] = "FAILED test_invoice_total"
        lines[len(lines) // 2 + 1] = "rounding_mode=HALF_EVEN; observed +0.01 versus Decimal expected"
        question = "What setting appears immediately after FAILED test_invoice_total? Reply as name=value."
        expected_answer = "HALF_EVEN"
    else:
        lines[len(lines) // 2] = "DEPLOY_KEY_73 = cobalt"
        question = "What is the exact value of DEPLOY_KEY_73? Answer with one word."
        expected_answer = "cobalt"
    history = "\n".join(lines)
    key = gateway_key()
    with httpx.Client(base_url=BASE_URL, headers={"Authorization": "Bearer " + key}, timeout=180) as client:
        before = client.get("/api/active-model").json()
        results = []
        try:
            set_policy(client, before, test=True)
            requests = (
                ("anthropic", "/v1/messages", {
                    "model": MODEL, "system": "Answer using the conversation evidence.",
                    "messages": [
                        {"role": "user", "content": history},
                        {"role": "assistant", "content": "I received the trace."},
                        {"role": "user", "content": question},
                    ],
                    "max_tokens": 512, "stream": False,
                }),
                ("openai", "/v1/chat/completions", {
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": "Answer using the conversation evidence."},
                        {"role": "user", "content": history},
                        {"role": "assistant", "content": "I received the trace."},
                        {"role": "user", "content": question},
                    ],
                    "max_tokens": 512, "stream": False,
                }),
            )
            for protocol, path, payload in requests:
                if args.protocol not in ("both", protocol):
                    continue
                if args.tool_array_without_items:
                    payload["tools"] = [{"type": "function", "function": {
                        "name": "Run", "parameters": {"type": "object", "properties": {
                            "commands": {"type": "array"},
                        }},
                    }}]
                start = time.perf_counter()
                response = client.post(path, json=payload)
                elapsed = round(time.perf_counter() - start, 2)
                body = response.json()
                if protocol == "anthropic":
                    answer = "".join(block.get("text", "") for block in body.get("content", [])
                                     if block.get("type") == "text")
                    usage = body.get("usage", {})
                    input_tokens = sum(usage.get(name, 0) or 0 for name in (
                        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                else:
                    answer = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                    usage = body.get("usage", {})
                    input_tokens = usage.get("prompt_tokens", 0)
                results.append({
                    "protocol": protocol, "status": response.status_code, "seconds": elapsed,
                    "input_tokens": input_tokens, "answer": answer,
                    "sift_headers": {name: value for name, value in response.headers.items()
                                     if name.lower().startswith("x-sift-")},
                    "stop_reason": body.get("stop_reason") if protocol == "anthropic" else
                        (body.get("choices") or [{}])[0].get("finish_reason"),
                    "error": body.get("error") if response.status_code != 200 else None,
                })
        finally:
            set_policy(client, before, test=False)
            after = client.get("/api/active-model").json()
            keys = ("active_model", "mode", "reasoning_effort", "advisor_model", "advisor_reasoning_effort",
                    "advisor_trigger", "advisor_sensitivity", "advisor_classifier")
            assert all(after[name] == before[name] for name in keys), "staging policy was not restored"
    print(json.dumps({"history_characters": len(history), "results": results}, ensure_ascii=False))
    expected = 2 if args.protocol == "both" else 1
    return 0 if len(results) == expected and all(
        item["status"] == 200 and expected_answer.lower() in item["answer"].lower()
        and 0 < item["input_tokens"] <= 28_672
        and item["stop_reason"] in {"end_turn", "stop"}
        for item in results
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
