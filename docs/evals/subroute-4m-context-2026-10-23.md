# 4M input and Sift stability, 2026-10-23

## Shipped policy

- Gateway text intake supports 4M tokens. Its 4,194,304 estimated-token guard allows for LiteLLM's large-input extrapolation. A fully tokenized 4M fixture was estimated at 4,035,280 tokens.
- Ordinary target requests aim for 32,768 tokens. `metadata.sift_target_tokens` can select up to 65,536. One CPU-only relaxation can use at most twice the selected target when required content prevents the optimum. A Sift failure preserves the original request for LiteLLM's provider limits and existing fallback chain.
- Local Qwen keeps its actual 32,768 input ceiling. Provider capacity and output limits are independent of client history and Sift optimization targets.
- Advisor copies have a strict 16,384 estimated-token dispatch boundary. Oversized copies use Sift and the existing bounded cheap summarizer. An unsafe copy skips that Advisor call and allows target routing to continue. Main, fallback and supplemental Advisor dispatches recount their copies immediately before sending.
- Gateway client-compaction metadata and Desktop launch profiles use a 1M trigger. Desktop advertises a 4M virtual session. Native client managers own their conversation checkpoints. DSH's web profile disabled its compaction plugin; the launch patch explicitly enables it with threshold ratio 0.25.

## Confirmed defects corrected

1. A ledger reminder appended after the conversation became a late developer instruction. Sift rejected it with HTTP 422 when input crossed its target. The reminder now enters the instruction prefix.
2. Codex Responses input with opaque reasoning or built-in tool definitions was rejected by Sift. Opaque reasoning is retained when selected; tool definitions stay unchanged and receive a serialized counting floor. Unsupported compaction views pass through to native routing instead of rejecting the target request.
3. Responses restoration replayed an original large active user/tool payload after Sift excerpted it. Restoration now forwards the changed excerpt while retaining tool output IDs and linkage.
4. Active-user extraction replaced an element in the caller's message list. The core now owns its working list, keeping the original available for soft failure.
5. The Advisor callback applied target capacity before making its own small copy. That early gate is removed. Instruction-only Sift counting candidates are supported, and final Advisor output still requires a user turn.

## Bounded algorithm

The active-message query uses the first 4,096 characters, twelve distributed 256-character samples and the last 2,048 characters. Candidate ranking remains extractive and chronological. A target-scaled index is bounded at 16,384 candidates, so an intermediate 1M checkpoint can retain substantially more than the normal 32K/64K prompt. Binary budget selection uses the caller's counter, and a relaxation reuses the raw count. No summarization model is added to this CPU path.

Applications preparing a checkpoint can call the existing `compact_messages(history, counter, target=1_000_000, raw_limit=RAW_INTAKE_LIMIT)` before their own checkpoint workflow. Target-model calls still use the smaller gateway target. Instructions and tool structure remain protected; excerpts are explicitly marked lossy.

## Evidence

Executed against the deployed LiteLLM 1.103.0 image and vendored Sift source:

| Check | Result |
| --- | --- |
| Live staging Chat request, full cl100k fixture plus Chat framing | 4,000,000 tokens; HTTP 200; exact `SIFT_4M_OK`; terminal `stop`; 10.316 seconds |
| Live production Chat smoke | HTTP 200; exact `PROD_CONTEXT_OK`; terminal `stop`; provider usage 379 input and 6 output tokens |
| Live Sift audit | 4,041,326 estimated input tokens; 380 estimated forwarded tokens |
| Provider usage on that request | 564 input, 38 output, 602 total tokens |
| CPU repetitive 4M fixture | 4,035,280 estimated input → 234; 1.333 seconds |
| CPU dense checkpoint, target 1M | 4,000,998 estimated input → 682,533; 7.051 seconds; 7,812 selected active excerpts |
| Focused Python checks | 145 passed; additional intake-headroom check plus target regression group: 8 passed |
| Desktop generated profiles | 5 passed; native DSH composed configuration confirms compaction enabled and threshold 0.25 |
| Independent Codex CLI review through local LiteLLM | Completed with `NO_BLOCKERS`; actual upstream model not established by the review receipt |

The CPU cases are reproducible with `Get-Content scripts/benchmark_sift_context.py -Raw | docker compose exec -T gateway-staging python -`. They establish bounded capacity, speed and budget behavior on these fixtures. They do not establish retention of every fact or broad task quality. Native client parsing was checked through generated settings and DSH composition; a full 1M-token conversation was not run separately in every supported client.

## Deployment and observation

Both local Compose services were recreated after the final intake correction. Production uses port 4000; staging uses 4005. Existing routing and Advisor selections were preserved. An earlier six-minute window saw 53 inference requests and no HTTP errors before the final intake correction.

Final observation ran from 17:50:21 to 18:00:21 UTC, with 79 inference requests and no HTTP errors. Both readiness endpoints returned 200 at every checkpoint:

| Elapsed minutes | Inference requests in interval | HTTP errors | Production / staging readiness |
| --- | --- | --- | --- |
| 0 | 0 | 0 | 200 / 200 |
| 2 | 14 | 0 | 200 / 200 |
| 4 | 16 | 0 | 200 / 200 |
| 6 | 16 | 0 | 200 / 200 |
| 8 | 16 | 0 | 200 / 200 |
| 10 | 17 | 0 | 200 / 200 |

The observation process exited successfully. A broader subsequent check of all POST access statuses since the observation start also found no HTTP errors, including paths without a `/v1` prefix. A fresh user report of 422 could not be correlated: the last 45 minutes of production, staging, canary and experts logs contained no matching HTTP 422 or Sift failure entry. The error body and client/endpoint were requested. This receipt establishes the bounded observation, not a diagnosis or resolution of an unseen request.

## Skill learning

For large-request failures, inspect callback order and the final restored payload, not only the compactor's intermediate messages. A successful selection can still be undone by a restoration map; an added instruction can itself make compaction unsupported. Keep extraction input immutable and prove a complete request through the deployed gateway before declaring capacity verified.
