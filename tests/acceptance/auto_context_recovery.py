"""Opt-in live probes through the 11435 entry point; credentials never printed.

Run from Subroute: .venv/Scripts/python tests/acceptance/auto_context_recovery.py
Uses the existing local gateway configuration and live provider inference.
The configured advisor policy may also perform its usual consultation.
"""

import json
import argparse
from pathlib import Path
import time

import httpx


def payload(protocol, cycles=12):
    messages = [{"role": "user", "content":
                 "Report the earliest and newest failed test identifiers. Use exactly two identifiers; do not call more tools."}]
    for index in range(cycles):
        tool_id = f"read_{index}"
        text = "routine output " * (40_000 if index < 8 else 100)
        text += f"\nFAILED: test_recovery_{index}"
        if protocol == "chat":
            messages.extend([
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": tool_id, "type": "function",
                    "function": {"name": "Read", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": tool_id, "content": text},
            ])
        else:
            messages.extend([
                {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id,
                                                     "name": "Read", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                "content": text}]},
            ])
    data = {"model": "auto", "max_tokens": 512, "stream": True, "messages": messages}
    instruction = "Use only the supplied test output. No more tool calls are needed."
    schema = {"type": "object", "properties": {}}
    if protocol == "chat":
        data["messages"] = [{"role": "system", "content": instruction}, *messages]
        data["tools"] = [{"type": "function", "function": {"name": "Read", "parameters": schema}}]
    else:
        data["system"] = instruction
        data["tools"] = [{"name": "Read", "input_schema": schema}]
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=12, choices=(12, 13))
    cycles = parser.parse_args().cycles
    config = json.loads((Path.home() / "local-api-gateway/config.11435.json").read_text())
    secret = next(iter(config["keys"].values()))["secret"]
    with httpx.Client(base_url="http://127.0.0.1:11435", timeout=180, headers={
        "Authorization": "Bearer " + secret, "anthropic-version": "2023-06-01",
    }) as client:
        for protocol, endpoint in (("chat", "/v1/chat/completions"), ("anthropic", "/v1/messages")):
            start = time.monotonic()
            response = client.post(endpoint, json=payload(protocol, cycles))
            events = []
            for line in response.text.splitlines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    events.append(json.loads(line[6:]))
            answer = "".join(
                (item.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                if protocol == "chat" else item.get("delta", {}).get("text", "")
                for item in events
            )
            terminal = (any((item.get("choices") or [{}])[0].get("finish_reason") == "stop" for item in events)
                        if protocol == "chat" else any(item.get("type") == "message_stop" for item in events))
            audit = {key: response.headers.get(key) for key in (
                "x-sift-compacted", "x-sift-estimated-input-tokens", "x-sift-estimated-forwarded-tokens",
            )}
            print(json.dumps({"protocol": protocol, "status": response.status_code,
                              "seconds": round(time.monotonic() - start, 2),
                              "audit": audit, "answer": answer, "terminal": terminal}))
            assert response.status_code == 200 and terminal
            assert audit["x-sift-compacted"] == "true"
            assert int(audit["x-sift-estimated-input-tokens"]) > 256_000
            assert int(audit["x-sift-estimated-forwarded-tokens"]) + 512 + 8192 <= 256_000
            assert "test_recovery_0" in answer and f"test_recovery_{cycles - 1}" in answer


if __name__ == "__main__":
    main()
