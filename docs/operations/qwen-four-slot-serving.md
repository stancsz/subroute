# Qwen four-slot serving with desktop headroom

Date: 2026-10-21. Status: the 49-layer deployment failed throughput and gaming acceptance and was stopped for recovery. A later experimental 40-layer trial is recorded in [the gaming profile report](qwen-gaming-profile.md); use that report for the latest runtime state.

## Requirements

- Serve Qwen3.8-27B through the existing llama.cpp, Subroute and LiteLLM stack.
- Support four concurrent inference requests, with at least 30 generated tokens/s combined, not per request.
- Keep a 32,768-token Sift target for each request, a 4,096-token output budget and 4,096 tokens of context allowance. Each slot therefore needs 40,960 tokens, with 163,840 tokens of total server capacity.
- Apply the owner's final budget: Qwen may use up to 12 GiB VRAM and 12 GiB system RAM, with approximately 4 GiB VRAM for the game and desktop combined. Retain CPU and system RAM for Sift and the gateways. This supersedes the earlier proposal to keep an additional 20% of VRAM free while the game was already loaded.
- Try an Unsloth Dynamic 3.0 three-bit model before considering two-bit quantization. Do not silently substitute a two-bit model or reduce context/concurrency to pass a benchmark.

## Model decision

Dynamic 3.0 is Unsloth's quantization recipe generation, not a statement that every tensor uses three bits. The existing UD-IQ3_S is already from the Dynamic model family. The deployed trial uses the smaller UD-IQ3_XXS from the same pinned model revision, preserving a three-bit-class model while reducing weight memory by about 1 GiB. The selected filename, revision and SHA-256 remain authoritative in `sidecars/qwen-local/start.sh`.

Three-bit quantization is expected to preserve more model fidelity than a more aggressive two-bit alternative, but neither a quality advantage on this workload nor a speed advantage is established without an actual comparison. No two-bit model was downloaded for this change.

On the RTX 5070 Ti, the final Qwen allocation budget is 12,288 MiB (12 GiB), rather than an additional free-VRAM margin on top of existing graphics use. The four contexts require approximately 2.81 GiB of raw Q4_0 full-attention KV cache. Approximate file weights plus that cache are 13.99 GiB for UD-IQ3_S, 12.96 GiB for UD-IQ3_XXS and 10.61 GiB for UD-IQ2_S. Recurrent states, checkpoints, execution buffers, graphics and allocation differences are additional. File bytes are not identical to GPU-resident weights. Some CPU offload may be necessary for the three-bit trial and can prevent the throughput requirement from passing.

## Configuration and ownership

| Owner | Setting | Trial value |
| --- | --- | --- |
| Compose qwen-local | CPU time / RAM / RAM plus swap | 6 CPUs / 12 GiB / 12 GiB |
| llama.cpp | Slots / total context / per-slot context | 4 / 163,840 / 40,960 |
| Compose and live verification | GPU budget / placement | Up to 12 GiB / 49 GPU layers, native autofitting disabled |
| llama.cpp | KV cache / flash attention | Q4_0 K and V / enabled |
| llama.cpp | CPU threads / polling | 6 generation and batch threads / disabled |
| llama.cpp | Logical / physical batch | 512 / 128 |
| llama.cpp | Host prompt-cache maximum | 1,024 MiB |
| llama.cpp | Reasoning / output | low, 512 reasoning tokens / 4,096 tokens |
| config/litellm.yaml | Input / Sift default / output / context | 32,768 / 32,768 / 4,096 / 40,960 |
| Host .wslconfig | All WSL2 CPU / RAM / swap | 8 logical CPUs / 24 GB / 4 GB |
| Host .wslconfig | Cache reclamation | gradual |

Compose limits protect system RAM and CPU time, not GPU resources. Equal RAM and RAM-plus-swap limits prevent container swapping and can terminate inference if its RAM budget is exceeded. WSL settings apply to all WSL2 distributions and require a WSL/Docker restart. Production and staging retain their separate persisted routing policies; both read the same model declaration and use the same inference service.

The native Sift callback requires an explicit alias-to-target map, mirrored from the YAML declaration and checked for parity by regression tests. The vendored Sift 0.10.0 implementation hardcodes two nonblocking compaction permits. A real four-request gateway probe returned two premature `429 Sift is busy` responses. The Qwen-only adapter increases that existing semaphore to four without changing native compaction, cancellation cleanup or the Auto route's two-worker bound. Retire this private-field compatibility override when the pinned Sift constructor exposes compaction concurrency. A regression checks four workers admitted, a fifth rejected, and an executing worker retaining its permit after caller cancellation.

The installed LiteLLM 1.103.0 admission checks do not enforce the configured output ceiling, and the pinned llama.cpp permits per-request overrides of its default `--n-predict`. The Qwen callback therefore reads the existing YAML-owned output limit through the router and rejects out-of-range explicit output requests before compaction/inference. It does not clamp requests or inject a missing output limit. Remove this validation when the deployed native admission path enforces the same limit across the public protocols.

The normal Claude acceptance runner advertises a 40,960-token client context. The separate `run_codex.py` harness intentionally retains a one-million-token client declaration for Sift intake evaluation; it is not a claim about model context capacity.

The final GPU budget is implemented through fixed layer placement in Compose and must be verified against actual allocations. Automatic fitting was rejected after a live trial: CUDA reported approximately 9.7 GiB free while NVML reported only 143 MiB free; Windows attributed about 13.8 GiB dedicated memory to the WSL VM and moved much of the game into shared memory. A startup-margin calculation cannot provide a reliable quota with these contradictory views. The trial was stopped. The fixed 49-layer configuration moves remaining model work to CPU RAM, retains all four 40K contexts, and targets at most 12 GiB GPU residency. This remains a measured operating profile, not a hard CUDA allocation quota. Native host prompt cache is capped at 1 GiB under the hard 12 GiB container limit. There is no established Docker Desktop mechanism on this RTX GPU that guarantees 20% GPU compute to a Windows game. Whole-card power limits also constrain the game. At initial inspection the GPU already reported a 252 W limit against a 300 W default. Do not change power or clocks as a substitute for workload isolation.

## Acceptance and deployment

1. Pin and verify the three-bit download, preserving the prior model cache for rollback.
2. Validate Compose and gateway configuration, then start/recreate the inference service. Verify model identity, four 40K slots, health, effective CPU/RAM limits, fixed layer placement, and actual dedicated/shared GPU memory. Do not accept a startup fitting estimate as a GPU quota.
3. Apply the documented host budget and verify it after restarting Docker/WSL. Restore the previously running containers and verify gateway readiness.
4. Exercise real buffered/streamed inference and gateway Sift handling. Preserve production/staging routing and advisor selections.
5. Run four independent approximately 32K-token prompts concurrently. Record prompt-processing latency and aggregate generation throughput separately; do not add individual throughput numbers and call that wall-clock throughput. Record free VRAM, RAM, CPU offload and terminal outcomes. Bound probe duration and cancel failed requests.
6. Accept the speed requirement only if the four-way measurement reaches at least 30 generated tokens/s combined. A short-prompt measurement does not establish the long-context result. Record a failed target honestly and retain a usable three-bit service where it can run safely.
7. Run the intended game during the combined test; measure frame times before accepting gaming performance. The 4 GiB game/desktop allowance can be consumed by those applications; it is not a requirement to leave an additional 4 GiB unused after they launch. If the actual game/desktop exceeds that allowance, Qwen may receive less than its 12 GiB maximum.

## Results

The document was written before implementation, then updated with the live results. Enlisted was running throughout the final inference trials. The deployed model is `Qwen3.8-27B-UD-IQ3_XXS.gguf`, revision `4ca720788d1e01f1bff70c033e0d0028fd02e502`, verified SHA-256 `c0b7c3038681ed2e3040456c1dd45f9858b6c2290bed172c70388a94874f3eee`. The file is 10,934,860,704 bytes. The CUDA server is pinned at image digest `sha256:b0baccbe2e00b5d74e63f85f9f023c93d61d0f329475269e1e7002e87eb875ea`, llama.cpp commit `091b6a7fc561b78da90a0ed2d524ec995458c657`.

| Check | Observed result | Assessment |
| --- | --- | --- |
| Short four-way generation | Four independent 512-token prompts, 32 generated tokens each; 5.16 tokens/s aggregate decode, 6.55 tokens/s during all-four overlap, 36.18 s wall time | **Fails 30 tokens/s** |
| Prompt reuse in that test | `timings.cache_n` was zero for all four requests | Fresh-prompt measurement |
| GPU residency | Windows WSL VM dedicated-memory samples about 11.3 to 11.5 GiB, plus a few hundred MiB shared/system memory | Observed under the 12 GiB VRAM budget; not a hard quota |
| Container RAM | Hard 12 GiB memory and memory-plus-swap limits; peak about 3.9 GiB during the short trials; no OOM events or swap | Effective RAM bound verified |
| Gateway concurrency and Sift | Four simultaneous historical requests estimated at 75,509 tokens each all returned HTTP 200 and `READY`; Sift compacted each to 112 estimated tokens, provider counted 140 | Four-request admission and compaction verified; not a 32K inference throughput test |
| Output ceiling | Explicit 4,097-token requests rejected with HTTP 400 on Chat, Messages and Responses | 4,096-token ceiling verified |
| Long-context attempt | Four fresh 32,768-token prompts, 128 requested output tokens each; no generated tokens within the 90-second deadline | Not accepted; full-context completion/throughput unverified |
| Structured tool smoke | Native server and gateway returned `add_numbers(a=2, b=3)` with supported required-tool mode | Narrow tool-call smoke passed |
| Arithmetic smoke | 285 + 157 returned 442 | Narrow smoke passed, not a general quality evaluation |

The 32K attempt is recorded in `.runs/qwen-four-slot-20261021/long-fixed49-sixcpu.json`. The runner closed all four connections at the deadline, but the pinned server continued prefill after disconnect. The inference container was restarted to clear those probe tasks, then health and idle slots were verified. Do not leave this probe unattended or assume socket cancellation immediately stops native prefill. A timeout is not evidence that a completed 32K request exceeds memory capacity; it establishes that this bounded performance attempt did not complete.

The pinned native server also reads `tool_choice` as a string. A named-tool object produced a warning and silently fell back to `auto`, so its empty tool result is not a quantization-quality finding. A separate native request with one declared tool and `tool_choice: required` returned the expected `add_numbers` call with arguments `a=2, b=3`. The Qwen callback now rejects named tool choices with HTTP 400 across Chat, Messages and Responses rather than weakening the request. Ordinary `auto` and supported required-tool mode retain native handling. The HTTP 400 rejection was verified live on all three gateway protocols. This narrow compatibility guard can be removed when a pinned server upgrade enforces named choices. See [the exact native parsing code](https://github.com/ggml-org/llama.cpp/blob/091b6a7fc561b78da90a0ed2d524ec995458c657/tools/server/server-common.cpp#L1196).

The short benchmark receipt is `.runs/qwen-four-slot-20261021/short-fixed49-sixcpu-rechecked.json`. Its `acceptance_passed` is false. Aggregate decode is measured across wall-clock time, not the sum of per-request rates. The separate all-four-overlap rate also fails the target. `tokens_cached` at terminal completion is resident prompt state, not evidence of reused prompt tokens; the benchmark checks `timings.cache_n` for actual reuse.

The RAM limit is enforceable through Docker cgroups. The GPU figure is a Windows WSL VM allocation proxy, including any other WSL CUDA work, rather than a per-container NVML measurement. Windows can page graphics allocations into system RAM, so Linux cgroup accounting alone does not establish a hard combined driver-plus-container host-memory quota. Fixed placement is reproducible here but must be rechecked after model, context, batch or driver changes. Whole-card free VRAM can be low while the game uses its allowance. A subsequent four-Claude-worker trial was stopped after the owner reported heavy game lag. Gaming coexistence therefore failed this profile. A matched FPS baseline and frame-time impact were not measured. See [the aborted soak report](qwen-claude-soak-20261021.md).

Docker/WSL was restarted to apply 8 logical CPUs, 24 GB RAM and 4 GB swap for the whole WSL VM. All ten previously running containers were restored. Production and staging readiness passed, and the persisted routing/advisor policy files retained their pre-restart SHA-256 values. The model's hard 12 GiB RAM limit is separate from that VM-wide budget.

Focused configuration, context, cancellation and acceptance-runner regressions passed: 196 tests, 38 subtests, with one unrelated existing experts-listener expectation excluded. A broader routing run exposed three existing policy-expectation mismatches outside this change. Compose validation and Python compilation passed. These checks do not substitute for live throughput or model-quality acceptance.

This configuration defines an experimental three-bit service at `http://127.0.0.1:8088`, with Subroute at ports 4000/4005. The 49-layer trial was stopped for gaming recovery before the later gaming-profile experiment. It does **not** satisfy the combined speed/context/concurrency/gaming requirements. CPU offload is required by the measured 12 GiB GPU budget and the selected quant/context; more host RAM allowance does not itself make that CPU work fast enough. Reaching 30 tokens/s requires a separately measured alternative, such as more available GPU memory, a smaller model, or a different quantization. No such tradeoff was silently applied.

## Reproduce the deployment and probe

The repository files own the service settings. Start or recreate only Qwen with:

```powershell
docker compose config --quiet
docker compose up -d qwen-local
```

The host WSL settings are in `C:\Users\stanc\.wslconfig`; unlike the Qwen container limit, they affect all WSL distributions. They are already applied on this machine. Changing them later requires restarting Docker/WSL and restoring any other WSL workloads.

A bounded fresh-prompt probe is:

```powershell
.venv/Scripts/python.exe tests/acceptance/qwen_serving/benchmark.py --input-tokens 512 --output-tokens 32 --parallel 4 --timeout-seconds 90 --output-json .runs/qwen-short.json
```

For the long-context attempt use `--input-tokens 32768 --output-tokens 128` and a distinct receipt filename. An unsuccessful acceptance result returns a nonzero exit code. Check `/slots` after cancellation; if the four probe tasks continue processing, restart only `qwen-local` to release them. This interrupts any other inference in that container, so run probes when it is otherwise idle.

## Sources

- [Unsloth model card and Dynamic 3.0 description](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF)
- [Pinned UD-IQ3_XXS artifact](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/blob/4ca720788d1e01f1bff70c033e0d0028fd02e502/Qwen3.8-27B-UD-IQ3_XXS.gguf)
- [Qwen architecture](https://huggingface.co/Qwen/Qwen3.8-27B/raw/main/config.json)
- [llama.cpp server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [Compose resource settings](https://docs.docker.com/reference/compose-file/services/)
- [WSL settings](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
- [NVIDIA MPS limits](https://docs.nvidia.com/deploy/mps/when-to-use-mps.html)
