"""Bounded CPU-only 4M intake / 1M checkpoint benchmark. No inference calls."""

import json
import time
from copy import deepcopy

import litellm
import tiktoken

from sift.core import compact_messages
from subroute.sift_plugin import RAW_INTAKE_LIMIT


def main():
    router = litellm.Router(model_list=[{
        "model_name": "fixture", "model_info": {"max_input_tokens": 256000},
        "litellm_params": {"model": "openai/gpt-4o", "api_key": "offline-only"},
    }], enable_pre_call_checks=True)

    def count(messages):
        return router._count_pre_call_check_tokens(
            messages=messages, input=None, request_kwargs={"model": "fixture"},
        )

    exact = [{"role": "user", "content": " neutral" * 3999993}]
    # Seven framing tokens for this one-message Chat fixture. Large-input
    # LiteLLM estimates differ from the full cl100k tokenization checked here.
    assert len(tiktoken.get_encoding("cl100k_base").encode(exact[0]["content"])) + 7 == 4000000
    dense = "".join(
        f"record {i:06d} status=observed path=src/module_{i % 97}.py value={i % 997} "
        "evidence=stable diagnostic context retained for review\n"
        for i in range(147500)
    )
    cycles = [{"role": "user", "content": "Find the latest diagnostic CTX875."}]
    for index in range(26):
        call_id = f"old_{index}"
        cycles.extend([
            {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
                "function": {"name": "lookup", "arguments": json.dumps({"query": " old evidence" * 20000})}}]},
            {"role": "tool", "tool_call_id": call_id, "content": "Previous result."},
        ])
    latest = {"id": "latest_call", "type": "function",
              "function": {"name": "lookup", "arguments": '{"query":"latest"}'}}
    cycles.extend([
        {"role": "assistant", "tool_calls": [latest]},
        {"role": "tool", "tool_call_id": "latest_call",
         "content": "current diagnostic record\n" * 80000 + "diagnostic CTX875 is resolved\n"},
    ])
    for label, messages, target in (
        ("4m_exact_fixture", exact, 32768),
        ("dense_checkpoint", [{"role": "user", "content": dense}], 1000000),
        ("large_active_tool_cycles", cycles, 32768),
    ):
        raw = count(messages)
        assert raw <= RAW_INTAKE_LIMIT, raw
        original = deepcopy(messages)
        started = time.perf_counter()
        result = compact_messages(messages, count, target=target, raw_limit=RAW_INTAKE_LIMIT)
        elapsed = time.perf_counter() - started
        assert messages == original, "Caller input was mutated"
        assert result.audit["forwarded_tokens"] <= target
        if label == "large_active_tool_cycles":
            assert result.messages[-2]["tool_calls"] == [latest]
            assert result.messages[-1]["tool_call_id"] == "latest_call"
            assert "diagnostic CTX875 is resolved" in result.messages[-1]["content"]
        print(json.dumps({"case": label, "target": target,
                          "elapsed_seconds": round(elapsed, 3), **result.audit}), flush=True)


if __name__ == "__main__":
    main()
