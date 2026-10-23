"""Bounded CPU-only 4M intake / 1M checkpoint benchmark. No inference calls."""

import json
import time

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
    for label, messages, target in (
        ("4m_exact_fixture", exact, 32768),
        ("dense_checkpoint", [{"role": "user", "content": dense}], 1000000),
    ):
        raw = count(messages)
        assert raw <= RAW_INTAKE_LIMIT, raw
        original_text = messages[0]["content"]
        started = time.perf_counter()
        result = compact_messages(messages, count, target=target, raw_limit=RAW_INTAKE_LIMIT)
        elapsed = time.perf_counter() - started
        assert messages[0]["content"] == original_text, "Caller input was mutated"
        assert result.audit["forwarded_tokens"] <= target
        print(json.dumps({"case": label, "target": target,
                          "elapsed_seconds": round(elapsed, 3), **result.audit}), flush=True)


if __name__ == "__main__":
    main()
