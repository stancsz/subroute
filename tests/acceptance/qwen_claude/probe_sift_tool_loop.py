"""Exercise in-turn Anthropic tool-cycle compaction through staging."""

from __future__ import annotations

import json
import time

import httpx

from run import BASE_URL, MODEL, gateway_key, set_policy


def main() -> int:
    messages = [{"role": "user", "content": "After the tool work, identify the newest failing test by name only."}]
    for index in range(36):
        tool_id = f"read_{index}"
        messages.extend([
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": tool_id, "name": "Read",
                "input": {"file_path": f"test_{index}.txt"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tool_id,
                "content": ("Routine diagnostic entry " + str(index) + ". ") * 330
                + f"\nFAILED: test_sift_cycle_{index}"
                + ("\nFINAL TOOL RESULT. There are no more files to read. Give the user the newest failing test name now."
                   if index == 35 else ""),
            }]},
        ])
    payload = {
        "model": MODEL,
        "system": "Respond to the user's original task using the latest tool result.",
        "messages": messages,
        "max_tokens": 512, "stream": False,
    }
    key = gateway_key()
    with httpx.Client(base_url=BASE_URL, headers={"Authorization": "Bearer " + key}, timeout=180) as client:
        before = client.get("/api/active-model").json()
        try:
            set_policy(client, before, test=True)
            start = time.perf_counter()
            response = client.post("/v1/messages", json=payload)
            elapsed = round(time.perf_counter() - start, 2)
            body = response.json()
            answer = "".join(block.get("text", "") for block in body.get("content", [])
                             if block.get("type") == "text")
            usage = body.get("usage", {})
            input_tokens = sum(usage.get(name, 0) or 0 for name in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            result = {"status": response.status_code, "seconds": elapsed,
                      "raw_characters": sum(len(str(msg["content"])) for msg in messages),
                      "input_tokens": input_tokens, "answer": answer,
                      "stop_reason": body.get("stop_reason"),
                      "error": body.get("error") if response.status_code != 200 else None}
            print(json.dumps(result, ensure_ascii=False))
            return 0 if response.status_code == 200 and "test_sift_cycle_35" in answer \
                and 0 < input_tokens <= 28_672 and body.get("stop_reason") == "end_turn" else 1
        finally:
            set_policy(client, before, test=False)
            after = client.get("/api/active-model").json()
            keys = ("active_model", "mode", "reasoning_effort", "advisor_model", "advisor_reasoning_effort",
                    "advisor_trigger", "advisor_sensitivity", "advisor_classifier")
            assert all(after[name] == before[name] for name in keys), "staging policy was not restored"


if __name__ == "__main__":
    raise SystemExit(main())
