"""Exercise local Qwen's Anthropic Messages compaction through staging."""

from __future__ import annotations

import json
import time

import httpx

from run import BASE_URL, MODEL, gateway_key, set_policy


def main() -> int:
    key = gateway_key()
    headers = {"Authorization": "Bearer " + key}
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=120) as client:
        before = client.get("/api/active-model").json()
        try:
            set_policy(client, before, test=True)
            payload = {
                "model": MODEL,
                "system": "Answer the latest request only.",
                "messages": [
                    {"role": "user", "content": "Prior log: " + "old evidence " * 45_000},
                    {"role": "system", "content": "Keep the answer short."},
                    {"role": "assistant", "content": "I noted the old log."},
                    {"role": "user", "content": "Reply exactly PONG."},
                ],
                "max_tokens": 64,
                "stream": False,
            }
            start = time.perf_counter()
            response = client.post("/v1/messages", json=payload)
            elapsed = round(time.perf_counter() - start, 2)
            body = response.json()
            answer = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
            usage = body.get("usage", {})
            total_input = sum(usage.get(name, 0) or 0 for name in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
            ))
            result = {
                "status": response.status_code,
                "seconds": elapsed,
                "raw_input_characters": len(payload["messages"][0]["content"]),
                "prompt_tokens_including_cache": total_input,
                "usage": usage,
                "answer": answer,
                "error": body.get("error") if response.status_code != 200 else None,
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0 if response.status_code == 200 and "PONG" in answer and (
                total_input > 0 and total_input <= 28_672
            ) else 1
        finally:
            set_policy(client, before, test=False)
            after = client.get("/api/active-model").json()
            keys = ("active_model", "mode", "reasoning_effort", "advisor_model", "advisor_reasoning_effort",
                    "advisor_trigger", "advisor_sensitivity", "advisor_classifier")
            assert all(after[key] == before[key] for key in keys), "staging policy was not restored"


if __name__ == "__main__":
    raise SystemExit(main())
