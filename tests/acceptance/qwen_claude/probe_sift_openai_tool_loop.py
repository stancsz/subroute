"""Exercise an oversized OpenAI tool loop through staging."""

from __future__ import annotations

import json
import time

import httpx

from run import BASE_URL, MODEL, gateway_key, set_policy


def main() -> int:
    messages = [
        {"role": "system", "content": "Answer the original user task from the latest tool result."},
        {"role": "user", "content": "After the tool work, identify the newest failing test by name only."},
    ]
    for index in range(36):
        tool_id = f"call_{index}"
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": tool_id, "type": "function", "function": {
                    "name": "Read", "arguments": json.dumps({"file_path": f"test_{index}.txt"}),
                },
            }]},
            {"role": "tool", "tool_call_id": tool_id,
             "content": ("Routine diagnostic entry " + str(index) + ". ") * 330
             + f"\nFAILED: test_sift_openai_{index}"
             + ("\nFINAL TOOL RESULT. There are no more files to read. Give the user the newest failing test name now."
                if index == 35 else "")},
        ])
    payload = {"model": MODEL, "messages": messages, "max_tokens": 512, "stream": False}
    key = gateway_key()
    with httpx.Client(base_url=BASE_URL, headers={"Authorization": "Bearer " + key}, timeout=180) as client:
        before = client.get("/api/active-model").json()
        try:
            set_policy(client, before, test=True)
            start = time.perf_counter()
            response = client.post("/v1/chat/completions", json=payload)
            elapsed = round(time.perf_counter() - start, 2)
            body = response.json()
            choice = (body.get("choices") or [{}])[0]
            answer = choice.get("message", {}).get("content") or ""
            result = {"status": response.status_code, "seconds": elapsed,
                      "raw_characters": sum(len(str(msg.get("content"))) for msg in messages),
                      "input_tokens": body.get("usage", {}).get("prompt_tokens", 0),
                      "answer": answer, "finish_reason": choice.get("finish_reason"),
                      "error": body.get("error") if response.status_code != 200 else None}
            print(json.dumps(result, ensure_ascii=False))
            return 0 if response.status_code == 200 and "test_sift_openai_35" in answer \
                and 0 < result["input_tokens"] <= 28_672 and choice.get("finish_reason") == "stop" else 1
        finally:
            set_policy(client, before, test=False)
            after = client.get("/api/active-model").json()
            keys = ("active_model", "mode", "reasoning_effort", "advisor_model", "advisor_reasoning_effort",
                    "advisor_trigger", "advisor_sensitivity", "advisor_classifier")
            assert all(after[name] == before[name] for name in keys), "staging policy was not restored"


if __name__ == "__main__":
    raise SystemExit(main())
