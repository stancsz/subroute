# Extended Claude Code benchmark on local Qwen, 2026-10-15

## Setup and measurement

Native Claude Code 2.1.251 used the staging Anthropic Messages route to `qwen3.8-27b`, backed by Unsloth's IQ3_S GGUF on llama.cpp CUDA. The server was configured for a 32,768-token context and Q8 KV cache. Each task used a disposable fixture copy, allowed Read, Edit, and PowerShell, and asked Claude Code to run failing tests, edit the source, and rerun the tests. The harness allowed up to 28 turns and 720 seconds per task. Its independent after-run tests checked the edited copy. The staging routing and advisor policy was restored after each task.

Time to first token (TTFT) is the arithmetic mean of Claude Code's `ttft_ms` values on streamed `message_start` events. Server tokens per second is the total generated tokens divided by total llama.cpp `eval time` for all completed local model calls in the task window. These are distinct denominators. The server counts include any auxiliary CLI model calls in the same window. Wall time includes CLI, tool, and test overhead. No other deliberate local model traffic ran during the benchmark.

| Task | Wall time | Streamed responses | Mean TTFT | Server completions | Mean generation rate | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Context compaction planner | 524.59 s | 16 | 3.90 s | 19 | 44.28 tokens/s | Original 5 tests passed; review holdout failed |
| Atomic policy snapshots | 121.56 s | 6 | 3.05 s | 7 | 46.11 tokens/s | 5 tests passed |
| **Combined** | **646.15 s** | **22** | **3.67 s** | **26** | **44.63 tokens/s** | One substantive gap in compaction |

Both runs had a failed CLI test before editing, at least one passing CLI test afterward, a successful CLI exit, and passing independent fixture tests. The compaction run needed four post-edit test attempts; the policy run passed on its first post-edit test. The long compaction run had two high TTFT observations, 21.5 and 26.8 seconds, which raise its average. Recent completions otherwise generally started in about 0.4 to 2.1 seconds.

## Review finding

The compaction implementation passes the five original fixture tests but replaces all older dialogue with a fixed notice. A separate holdout input with `EARLY_DECISION` near the start and `LATE_FINDING` near the end forwarded neither fact. That violates the intended historical excerpt behavior of the design. The acceptance fixture now includes this targeted regression test for later runs. This result is a functional miss despite the passing original tests. The benchmark did not change production compaction code.

The policy implementation used one immutable snapshot per request and validated and atomically replaced its JSON file before publishing an update. The five behavioral tests passed, including in-process concurrent reads and staging/production file isolation. They do not establish behavior under multiple processes writing the same file.

## Evidence and reproduction

Raw CLI streams, stderr, edited fixture source, test results, and timing arrays are under the ignored run directories:

- `tests/acceptance/qwen_claude/.runs/subroute-qwen-claude-xkb4u01k/context_compaction/`
- `tests/acceptance/qwen_claude/.runs/subroute-qwen-claude-2cc0r8gl/policy_snapshot/`

The staging gateway returned readiness 200 after the runs; its saved policy was `openrouter` in `force` mode with `gemini-subscription` advisor. Reproduce with:

```powershell
python tests/acceptance/qwen_claude/run.py --task context_compaction --verify-in-cli --timeout 720 --max-turns 28
python tests/acceptance/qwen_claude/run.py --task policy_snapshot --verify-in-cli --timeout 720 --max-turns 28
```

The added context holdout test is in the source fixture, so a new run evaluates it from the start. The recorded October 15 run used the five-test version and was reviewed separately against the holdout after completion.
