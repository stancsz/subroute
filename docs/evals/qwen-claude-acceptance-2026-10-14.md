# Local Qwen 3.8 Claude Code acceptance, 2026-10-14

## Setup

The native Claude Code 2.1.251 CLI used the staging gateway's Anthropic Messages endpoint with model `qwen3.8-27b`. The model was Unsloth's pinned `Qwen3.8-27B-UD-IQ3_S.gguf` in the optional llama.cpp CUDA service. The server had a 32,768-token context, Q8 KV cache, low reasoning effort, a 512-token reasoning budget, and one request slot. The acceptance harness copied checked-in fixtures into ignored workspaces, allowed only Claude Code's Read and Edit tools, and restored staging routing and advisor policy after every run. The tests ran against the edited copies after the CLI returned.

## Coding tasks

| Task | Initial state | Claude Code actions | CLI result | Elapsed | After-run tests |
| --- | --- | --- | --- | ---: | --- |
| Routing policy bug | 3 failures in 5 tests | Read, Read, Edit | success | 44.49 s | 5 passed |
| JSONL cache report implementation | 1 failure and 1 error in 3 tests | Read and Edit over several turns | success | 123.18 s | 3 passed |

Both edited only the requested source file. The final code passed the fixture tests. The model server logged roughly 45 to 47 generated tokens per second during these runs. `nvidia-smi` reported 14,552 MiB used and 1,444 MiB free on the 16,303 MiB GPU after the tuned runs. These are end-to-end task times, including CLI and tool overhead, not just inference time. The model's own explanations traced the tests; the acceptance harness, not Claude Code, executed the tests.

Claude Code wrote an `unrecognized_model` diagnostic while generating a session title. It did not block either task or change the requested model route.

## Shell-enabled, multi-turn follow-up

The earlier tasks allowed Read and Edit only, so they did not establish that Claude Code could run a failing test, use the result, and verify a repair. The `--verify-in-cli` mode also allows PowerShell and asks for a test run before and after the edit. The harness now requires a failed CLI test before the first edit and a passing CLI test afterward, as well as a successful final CLI result and its independent after-run test.

| Task | Assistant turns | Observed CLI test loop | End result | Elapsed |
| --- | ---: | --- | --- | ---: |
| Routing policy bug | 12 | 3 failures before edit; 1 failure after edit | Hit the turn limit; independent tests failed | 211.65 s |
| JSONL cache report | 5 | 1 failure and 1 error before edit; all 3 tests passed after edit | Success; independent tests passed | 88.44 s |

The routing failure is a model behavior failure, not a shell or harness failure. Its edited source left `return requested` inside the preceding `if` block. Claude Code observed the remaining test failure, then investigated bytecode caches and filesystem details instead of moving that statement. This is a concrete reliability limit for autonomous debugging: one of two test-driven fixture attempts succeeded. The earlier Read/Edit-only passes remain valid, but they do not prove that a richer self-test loop is reliable.

An earlier unrestricted reasoning run of the JSONL task also passed but took 213.85 seconds. A zero-token reasoning budget finished the routing task in 25.20 seconds but left one test failing while claiming success. A 256-token budget reached passing code in 99.49 seconds but hit the ten-turn CLI limit before a final answer. These failures are why the deployed setting is 512 tokens.

## Long-history probe

The staging Anthropic Messages endpoint received a 585,011-character prior log, a later system entry, and a short current request. Gateway compaction preserved the system role and current request, forwarded a 15,065-token prompt, and returned `PONG` with HTTP 200 in 11.42 seconds on the initial run. After gateway restarts, an identical request returned in 1.05 seconds with 15,061 cache-read tokens and 4 new input tokens. The gateway's system-entry translation was necessary because the GGUF chat template rejects system messages after dialogue. LiteLLM still owns Anthropic-to-Chat translation and streaming.

This probe demonstrates automatic compaction for a long text history, not a million-token Claude Code session. The gateway's one-million-token limit is an estimated CPU intake limit. The model still attends to only the compacted request in its 32K context. Excerpting old history can omit facts; an oversized current turn or unsupported media fails closed.

## Reproduction

```powershell
docker compose --profile local-qwen up -d qwen-local
python tests/acceptance/qwen_claude/run.py --task all
python tests/acceptance/qwen_claude/run.py --task all --verify-in-cli
python tests/acceptance/qwen_claude/probe_long_history.py
.venv\Scripts\python.exe -m pytest tests/test_local_context.py -q
```

The scripts require the local staging gateway, Docker, the native Claude Code CLI, and its staging API key. They restore the saved staging policy in their normal exit path. The generated CLI transcripts and result JSON files are under `tests/acceptance/qwen_claude/.runs/` and are ignored by Git.
