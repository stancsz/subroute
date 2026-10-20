"""Ask Qwen about buried failures from locally generated Claude task logs."""

from __future__ import annotations

import json
import time

import httpx

from eval_sift_trace import TRACES, ROOT, replay
from run import BASE_URL, MODEL, gateway_key, set_policy


def failed_log(task: str, folder: str, required: str) -> str:
    history = replay(ROOT / "tests/acceptance/qwen_claude/.runs" / folder / task / "claude.jsonl", task)
    for message in history:
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            value = block.get("content")
            if isinstance(value, str) and "Exit code 1" in value and required in value:
                return value
    raise RuntimeError(f"no matching failed run in {task}")


def main() -> int:
    noise = "\n".join(f"Routine worker record {index} completed normally" for index in range(4500))
    cases = [(task, folder, focus, expected, "ERROR")
             for task, (folder, focus, expected) in TRACES.items()]
    cases.append((
        "context_compaction_followup", TRACES["context_compaction"][0],
        "test_rejects_late_instruction_and_media_when_compacting",
        "FAIL: test_rejects_late_instruction_and_media_when_compacting", "FAIL",
    ))
    key = gateway_key()
    results = []
    with httpx.Client(base_url=BASE_URL, headers={"Authorization": "Bearer " + key}, timeout=180) as client:
        before = client.get("/api/active-model").json()
        try:
            set_policy(client, before, test=True)
            for task, folder, focus, expected, correct_label in cases:
                source_task = "context_compaction" if task == "context_compaction_followup" else task
                log = failed_log(source_task, folder, expected)
                assert expected in log
                history = noise + "\n\nINITIAL BASELINE TEST OUTPUT\n" + log + "\n\n" + noise
                payload = {
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": "Answer only from the test output in the conversation."},
                        {"role": "user", "content": history},
                        {"role": "assistant", "content": "I received the log."},
                        {"role": "user", "content":
                         f"For {focus}, did this test output list it under ERROR or FAIL? Reply one word."},
                    ],
                    "max_tokens": 256, "stream": False,
                }
                start = time.perf_counter()
                response = client.post("/v1/chat/completions", json=payload)
                elapsed = round(time.perf_counter() - start, 2)
                body = response.json()
                choice = (body.get("choices") or [{}])[0]
                answer = choice.get("message", {}).get("content") or ""
                results.append({
                    "task": task, "status": response.status_code, "seconds": elapsed,
                    "raw_characters": len(history),
                    "input_tokens": body.get("usage", {}).get("prompt_tokens", 0),
                    "answer": answer, "correct": answer.strip().upper() == correct_label,
                    "finish_reason": choice.get("finish_reason"),
                    "error": body.get("error") if response.status_code != 200 else None,
                })
        finally:
            set_policy(client, before, test=False)
            after = client.get("/api/active-model").json()
            keys = ("active_model", "mode", "reasoning_effort", "advisor_model", "advisor_reasoning_effort",
                    "advisor_trigger", "advisor_sensitivity", "advisor_classifier")
            assert all(after[name] == before[name] for name in keys), "staging policy was not restored"
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if len(results) == len(cases) and all(
        result["status"] == 200 and result["correct"]
        and 0 < result["input_tokens"] <= 28_672 and result["finish_reason"] == "stop"
        for result in results
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
