# Qwen GPU-only budget

Updated 2026-10-21. The owner's new budget is interpreted as **10 GiB of VRAM for Qwen**, leaving roughly 5 GiB for Enlisted and the desktop. The earlier 12 GiB container RAM/no-swap limit remains a separate host-memory setting. GPU-only execution replaces CPU layer offload; CPU tokenization and HTTP orchestration are still normal. The latest profile has two concurrent requests and two 40,960-token slots, with the 32K Sift input target retained. Gaming validation at the 40 displayed-FPS floor is deferred for this standalone test.

The machine has one NVIDIA RTX 5070 Ti. NVIDIA reports 16,303 MiB total VRAM. Live free memory fluctuates with the game; after overlay cleanup it was about 9.9 GiB. Per-process WDDM allocations can include shared surfaces, so process totals cannot be added as reclaimable physical VRAM.

## Latest standalone load, 2026-10-21 evening

The owner stopped this new load after the first completed paired round by saying "thats good enough". Receipts are in `.runs/qwen-sustained-load-20261021b`; independent audit is `.runs/qwen-stress-20261021-review/sustained-final-audit.json`. Round two was interrupted at the owner's request and round three did not start. Their missing terminal results are excluded from completed-request throughput. This is a new bounded run, separate from the earlier afternoon deadline.

Both first-round requests completed exactly 32,768 prompt plus 4,096 generated and streamed tokens, zero prompt cache reuse, no truncation and normal output-limit termination. All 65/65 layers, input embedding, model weights and live KV/recurrent state were verified on CUDA0. This proves placement, not exhaustive operator tracing.

| Measurement, completed round only | Result |
| --- | ---: |
| Two-request wall time, first send to last terminal | 393.59 s |
| Output including prefill, queueing and final taper | **20.81 tokens/s combined** |
| Output while both requests generated, 243.48 s | **31.51 tokens/s combined** |
| Whole decode interval, including sibling prefill/taper | 23.87 tokens/s combined |
| Full 60-second both-generating windows, observed range | 29.90 to 32.52 tokens/s |
| Native prompt timing, per-request rates | 653.05 and 240.46 tokens/s |
| Native decode timing, per-request rates including stalls | 12.43 and 15.91 tokens/s |
| Time to first token | 50.40 and 136.28 s |
| Request completion latency | 379.76 and 393.59 s |
| Total verified evaluated input / generated output | 65,536 / 8,192 tokens |
| Peak WSL dedicated GPU allocation proxy, whole run | 9,469.61 MiB, about 9.25 GiB |
| Peak container cgroup RAM, whole run | 676,933,632 bytes, about 646 MiB |
| Swap / OOM kills | 0 / 0 |

The two requests were concurrent and native input counts were exact. Prompt processing competes with the sibling's generation, explaining the substantial difference between whole-request and both-generating throughput. Per-request native rates must not be summed into combined throughput. Prompt reuse was disabled; model loading and the tiny gateway smoke were outside benchmark timing. Container RAM is not total host/WSL cache consumption; the hard 12 GiB RAM/no-swap limit remained configured. The GPU figure is monitored allocation, not a Docker hard GPU quota.

This synthetic native benchmark bypasses Sift. A separate staging gateway marker passed in 0.46 s with 19 prompt / 6 output tokens; it verifies the local route only, not 32K gateway throughput or compaction quality. No actual Claude coding or game/FPS trial occurred. The whole-decode 30 tokens/s target was not met, even though the both-generating average exceeded 30. The earlier 30-minute useful-work requirement remains unaccepted. Qwen stopped and unloaded cleanly, independent guardian confirmed the same container, and GPU usage returned to 3,177 MiB total at the final sample.

## Selected two-slot, two-bit trial

The owner approved **UD-IQ2_XXS of the same Qwen3.8-27B**, reduced concurrency to **two**, and requested benchmarking without the game. Gaming validation is deferred. Each slot retains 40,960 tokens, preserving a 32,768-token Sift target, 4,096 output tokens and 4,096 allowance.

| Core GPU allocation for two slots | GiB |
| --- | ---: |
| Exact IQ2_XXS used tensors, including input embedding | 6.7568 |
| Two 40K Q4_0 attention caches | 1.4063 |
| Minimum recurrent state | 0.2922 |
| **Core lower bound** | **8.4553** |

This leaves 1,581.77 MiB nominal margin under 10 GiB for CUDA/runtime overhead. Measured runtime WSL dedicated-allocation proxy peaked at 9,469.61 MiB (about 9.25 GiB), below the 10 GiB target. This is an observed proxy, not a Docker GPU quota. The pinned file is 7,266,070,528 bytes, revision `4ca720788d1e01f1bff70c033e0d0028fd02e502`, SHA-256 `e792d8fb3142fe6d9171876d6da0f71f05a71028718debc72dbec93ff645e67d`. Its 851 trunk tensors already omit MTP. Subtracting MTP again would understate memory.

The experiment is under `.runs/qwen-gpu-only-2slot-iq2-20261021`. Its Compose override selects the pinned IQ2 script, `--n-gpu-layers all`, explicit CUDA0 input-embedding placement, GPU Q4 KV, two 40K slots, b128/u32, fitting off, zero host prompt-cache RAM and zero context checkpoints. Automatic restart is disabled. CPU tokenization/HTTP orchestration remains normal. Before inference, allocation logs must verify all layer offloads, the embedding override, and CUDA0 model/KV/RS buffers with no CPU weight/layer fallback. This proves residency/configuration, not an exhaustive operator trace.

The standalone guard retains the 10 GiB WSL dedicated-allocation target, hard 12 GiB cgroup RAM/no swap and fresh host-resource checks. The owner explicitly deferred game-presence/FPS checks. The original 23:53:10 UTC workload deadline remains.

The first substantial probe used two uncached **36,864-input/4,096-output** requests. It failed at the exact per-slot boundary: slot 0 returned `truncated=true`, 4,096 generated versus 4,094 streamed tokens; the benchmark cancelled its sibling. This does not pass full-40K terminal coverage. Pinned server code checks `prompt.n_tokens()+1>=slot.n_ctx` before the output limit, so requests need a token margin. A single recovery under `.runs/qwen-gpu-only-2slot-iq2-margin-20261021` uses **32,768 input plus 2,048 output**, retaining both allocated 40K slots. The failed run measured 30.72 tokens/s during the partial both-active overlap, versus 22.95 across the whole partial decode interval. Neither is a successful complete benchmark. No Claude work ran in the failed attempt. Container RAM peaked at 7,353,245,696 bytes; swap/OOM remained zero and unload succeeded. Steady 30/60-second generation windows require both decodes. The original controller gated Claude workers on native terminal/count/cache and drain verification; neither native trial cleared both-request terminal checks. Separately scoped Claude work was subsequently authorized for quality/duration validation after verified unload, preserving the failed benchmark verdicts and original deadline. Context, throughput, memory, quality, duration and later gaming acceptance remain separate. Two-bit fit does not establish coding quality or 30 combined tokens/s.

For comparison, four slots fail the core bound even at two bits: IQ2_XXS 10.154 GiB and IQ2_S 11.184 GiB, before overhead. The owner's two-slot change is material.

## Standalone workload status at 23:21 UTC

The safety-margin recovery also remains failed overall: its 246-second watchdog cut off slot 1 at 1,957 streamed tokens. Slot 0 completed at 237.29 seconds with exactly 32,768 input tokens, 2,048 generated and streamed tokens, zero prompt cache reuse and no truncation. Both-active partial throughput was 34.08 tokens/s, versus 20.32 across the whole partial decode interval. The 60-second steady window measured 34.23 tokens/s. This does not prove two-request terminal completion. The model was unloaded successfully with no OOM, swap or cleanup errors.

A separately scoped two-worker Claude Code run began at 23:18:34 UTC under `.runs/qwen-gpu-only-2slot-iq2-claude-20261021`. It retains the original 23:53:10 UTC deadline and the same verified GPU placement and resource guards. It does not retry or relabel the failed native benchmarks. Two workers operate disposable snapshots of actual Subroute context, streaming, routing and usage tests. Only newly assigned regression tests and notes are writable; actual source/configuration and provider policy remain protected. Read/Write/Edit/PowerShell audit and independent test/artifact review are required. Initial nested Windows pytest runners exit without captured output, so independent collection/results are needed before accepting tests. Duration, near-32K forwarded requests, coding quality and gaming checks remain pending.

## Productive Qwen defaults and corrected workload

Sift is enabled by default through `subroute.sift_plugin.sift_qwen_hook` in the gateway callbacks. Requests need no opt-in. Its alias map and deployment configuration agree on the 32,768-token target; staging logs confirm actual Sift processing and compaction. Gateway extraction and Claude Code's own summary compaction are separate mechanisms.

| Setting | Current local Qwen default |
| --- | --- |
| Client history | 256,000 tokens |
| Client compaction | 90% of that window, or 230,400 tokens |
| Gateway Sift target | 32,768 estimated input tokens |
| Native capacity | Two slots of 40,960 tokens |
| Output allowance | 4,096 tokens in CLI and native defaults |
| Thinking | Disabled, matching native reasoning-off mode |
| Container RAM / swap | Hard 12 GiB / none |
| GPU placement / target | All model weights and live state on GPU / monitored 10 GiB |

The repository Qwen runner owns its per-process CLI environment. Autonomous assignments use that same default. This does not replace the user's unrelated global Claude provider settings. `--client-context-tokens 1000000` is available as a bounded configuration, matching Sift's one-million estimated-token intake ceiling. Explicit request outputs above 4,096 are rejected rather than silently clamped.

The large client window does not enlarge Qwen's attention window or configured GPU KV cache. Sift selects a lossy subset within its 32K budget. Instructions and required active content must fit, or admission fails. More retained history costs client/gateway RAM and CPU, and selection can omit useful details. Full 256K and 1M payload latency and retention quality remain unverified.

The initial Claude phase was stopped after 780.75 seconds elapsed, including 728.01 sampled active-inference seconds and 670.52 seconds with both slots active. It produced no test artifact. Its 1,024-token experiment override confounded quality by truncating client compaction summaries and losing task criteria. Unload succeeded without OOM, swap or cleanup errors.

The corrected 4K-output phase under `.runs/qwen-gpu-only-2slot-iq2-claude-4k-20261021` added protected task criteria in snapshot `CLAUDE.md` and captured pytest/JUnit results with positive collection checks. A harness defect rejecting bounded Reads with an omitted optional offset was repaired. Ordinary Reads now default to line 1; exact file permissions, the 200-line bound and explicit caller-verified ranges remain enforced.

That phase accumulated 590.58 active seconds and 469.61 seconds with both slots active. It produced four generated cases and an independently verified 12-test pass. Review accepted one useful developer-role boundary, rejected duplicate cases and unsafe test cleanup, and found a weak assertion. Only the useful case was integrated into `tests/test_local_context.py`, with a stronger existing-metadata assertion; all nine local-context tests passed. This is useful regression coverage, not general coding-quality acceptance. The streaming worker ended at Claude Code's local 40K `blocking_limit`, with no provider API error, before producing a test.

The owner's larger-history request is being exercised under `.runs/qwen-iq2-client256k-20261021`, retaining the original 23:53:10 UTC deadline and the same native GPU profile. A successful marker response reports a 256,000-token client window, while native capacity remains 40,960 per slot, Sift remains 32,768 and output remains 4,096. This proves the configured client window and route, not a full-size payload. Durations across changed harnesses remain separate; they do not establish 30 minutes of validated coding under one fixed harness.

Default/context validation passed 37 tests and 11 subtests. Three targeted Read-guard boundary tests passed. The earlier Qwen capacity check passed; an unrelated pre-existing experts-port binding assertion remains failed and unchanged.

## Final standalone result

The run ended at 23:52:52 UTC. Qwen is stopped and unloaded; cleanup succeeded, provider policies stayed unchanged, and no container OOM or swap occurred. The saved two-slot IQ2 profile and 256K client defaults remain configured for the next invocation.

| Evidence | Result |
| --- | --- |
| Live client-window marker | Passed, reports 256,000 tokens |
| Native capacity / Sift / output | 2 × 40,960 / 32,768 / 4,096 |
| Peak measured WSL dedicated GPU allocation | 9,471.61 MiB, about 9.25 GiB |
| Latest real-work completed output | 3,730 tokens across 411.00 seconds: **9.08 tokens/s** |
| Latest real-work evaluated prompt tokens | 156,160 across the same interval: **379.95 tokens/s** |
| Active inference across changed Claude harnesses | **1,683.42 seconds, or 28.06 minutes** |
| Useful integrated regression | One; all nine local-context tests pass |
| Full 256K / 1M payload quality and latency | Unverified |
| Gaming coexistence / FPS | Deferred by the owner |

The latest rates count completed native requests only, excluding cancelled in-flight work. Their denominator includes input processing, tool pauses and cleanup. Re-evaluated prompt tokens are not unique retained context. No 30/60-second interval of continuous two-request decoding qualified in the latest real-work phase. Earlier synthetic partial overlap measured about 31 to 34 combined generated tokens/s, but neither native terminal benchmark passed overall.

The latest 256K phase reached 23,164 native prompt tokens, so its client marker must not be presented as a full-size payload test or a new 32K concurrent-load result. Earlier actual Claude requests did exercise concurrent native contexts around 34K to 35K. Client history capacity, admitted prompt size and native allocation remain distinct.

The newest generated test was rejected by independent review: it introspects an imported module and contains an assertion that always passes. Earlier, the runner correctly rejected a generated file with no collected pytest cases. Neither worker completed the full assignment with notes. The integrated developer-role regression is a small useful outcome, not evidence of reliable general coding capability. The 30-minute active/fixed-harness coding criterion remains unmet, and no gaming success is claimed.

Raw receipts, independent throughput and quality reviews, and the exact final summary are under `.runs/qwen-iq2-client256k-20261021`, particularly `final-result.json`, `phase-final-independent-audit.json`, `qa-independent-quality.json` and `client-window-smoke.json`.

## Earlier three-bit/four-slot calculation

The installed pinned UD-IQ3_XXS GGUF was inspected without loading inference or initializing CUDA. Exact tensor sizes were calculated from dimensions and quantization block types. The 15 unused MTP tensors were excluded.

| Core allocation | GiB |
| --- | ---: |
| Used model tensors | 9.8467 |
| Four 40K Q4_0 attention caches | 2.8125 |
| Minimum recurrent state | 0.5845 |
| **Minimum total** | **13.2437** |

This excludes CUDA workspaces, recurrent rollback rows, checkpoints and allocator overhead. It therefore exceeds both the previous 12 GiB and new 10 GiB GPU budgets before inference begins. Omitting the input embedding still does not meet either budget at this capacity. Reducing CPU threads, batch size or host prompt-cache RAM cannot remove this core floor. Docker's CPU/RAM limits do not enforce a consumer-GPU VRAM quota.

The reproducible header-only calculation and source URLs are in `.runs/qwen-gpu-only-20261021/inspect-gpu-fit.py` and `gpu-fit-proof.json`. The arithmetic was independently recomputed. The budget interpretation is recorded in `budget.json`. The proof is a capacity lower bound, not a measured full-GPU allocation or performance result. A full-GPU model load was not attempted above the owner's budget.

`--n-gpu-layers -1` fully offloads model layers, but the pinned runtime still places the input embedding on CPU by default. Strict all-weight GPU residency requires explicit tensor placement and runtime verification. A future trial must also verify that attention and recurrent state stay on GPU and that no automatic fit or CPU fallback changes the profile.

## Cleanup and next decision

Enlisted reported about 4,869 MiB in per-process dedicated allocation counters. NVIDIA Overlay was the largest optional visible allocation. Its processes were stopped once, but the NVIDIA app restarted them. The normal NVIDIA App setting was then changed to turn the optional overlay off, and the settings window was closed. Overlay processes subsequently disappeared. NVIDIA says the setting takes full effect on the next game launch; Enlisted was not restarted. No game graphics, frame-generation, power, clock or driver settings were changed. Desktop/compositor, Codex, Docker and user document applications remain running. No separate idle Python/Ollama/llama/vLLM/ComfyUI CUDA workload was identified.

The previously proposed smaller-model route is historical; the owner selected the same 27B at two bits with two slots instead. **Qwen3.5-9B Q4_K_M is a proposed alternative, not a replacement already made.** Its published GGUF is 5.68 GB, and its configuration has eight full-attention layers, so raw four-slot Q4 attention KV is 1.40625 GiB. A bounded 16 MiB header inspection at pinned revision `3885219b6810b007914f3a7950a8d1b469d598a5` gives exact used tensors plus raw Q4 cache and minimum recurrent state of **6.883 GiB**, leaving about 3.117 GiB nominal margin under 10 GiB. This excludes runtime overhead and does not prove actual fit, 30 tokens/s or acceptable coding quality. The reproducible calculation is `9b-header-proof.json` with `inspect-9b-header.py` in the same run directory. It would need a pinned download/checksum, verified GPU placement, measured allocation, a sustained four-way 32K trial and real Claude tests while gameplay remains observable. The owner did not select the 9B alternative; no full 9B model was downloaded or started, only its bounded header was inspected.

Sources: [published 9B Q4 GGUF](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/blob/main/Qwen3.5-9B-Q4_K_M.gguf), [official 9B configuration](https://huggingface.co/Qwen/Qwen3.5-9B/blob/main/config.json), [pinned runtime input placement](https://github.com/ggml-org/llama.cpp/blob/091b6a7fc561b78da90a0ed2d524ec995458c657/src/llama-model.cpp).

Official client references: [custom/gateway model window override](https://code.claude.com/docs/en/model-config#correct-the-window-for-a-gateway-or-custom-model-id) and [environment variables](https://code.claude.com/docs/en/env-vars). Installed Claude Code 2.1.251 contains both client-window controls. The live marker receipt confirms an effective 256,000-token window.
