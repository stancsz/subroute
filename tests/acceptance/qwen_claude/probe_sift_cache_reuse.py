"""Check that compacted tool turns append to llama.cpp's cached prompt."""

from __future__ import annotations

import argparse
import json
import time

import httpx

from run import BASE_URL, MODEL, gateway_key, set_policy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask", choices=("newest", "earliest"), default="newest")
    args = parser.parse_args()
    messages = [
        {"role": "system", "content": f"Reply with only the {args.ask} FAILED test name."},
        {"role": "user", "content":
         f"Audit the incremental cache probe and report the {args.ask} FAILED test."},
    ]
    for index in range(36):
        tool_id = f"cache_probe_{index}"
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": tool_id, "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": tool_id,
             "content": (f"Diagnostic cache probe {index} line. " * 280)
             + f"\nFAILED: test_cache_probe_{index}"},
        ])

    results = []
    with httpx.Client(base_url=BASE_URL, headers={
        "Authorization": "Bearer " + gateway_key(),
    }, timeout=180) as client:
        before = client.get("/api/active-model").json()
        try:
            set_policy(client, before, test=True)
            for cycles in (34, 35, 36):
                start = time.perf_counter()
                response = client.post("/v1/chat/completions", json={
                    "model": MODEL, "messages": messages[:2 + 2 * cycles],
                    "max_tokens": 768, "stream": False,
                })
                body = response.json()
                usage = body.get("usage") or {}
                result = {
                    "cycles": cycles, "status": response.status_code,
                    "seconds": round(time.perf_counter() - start, 2),
                    "estimated_forwarded": int(response.headers.get(
                        "x-sift-estimated-forwarded-tokens", "0")),
                    "provider_input": usage.get("prompt_tokens", 0),
                    "cached_tokens": (usage.get("prompt_tokens_details") or {}).get(
                        "cached_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason"),
                    "answer": (body.get("choices") or [{}])[0].get(
                        "message", {}).get("content") or "",
                }
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            set_policy(client, before, test=False)
            after = client.get("/api/active-model").json()
            keys = ("active_model", "mode", "reasoning_effort", "advisor_model",
                    "advisor_reasoning_effort", "advisor_trigger", "advisor_sensitivity",
                    "advisor_classifier")
            assert all(after[key] == before[key] for key in keys)

    return 0 if len(results) == 3 and all(
        item["status"] == 200
        and f"test_cache_probe_{item['cycles'] - 1 if args.ask == 'newest' else 0}" in item["answer"]
        and 0 < item["estimated_forwarded"] <= 24_576
        and 0 < item["provider_input"] <= 28_672
        and (index == 0 or item["cached_tokens"] >= 10_000)
        for index, item in enumerate(results)
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
