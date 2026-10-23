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
| CPU repetitive 4M fixture | 4,035,280 estimated input → 234; 1.502 seconds |
| CPU dense checkpoint, target 1M | 4,000,998 estimated input → 682,533; 8.236 seconds; 7,812 selected active excerpts |
| CPU active tool history, target 32K | 1,372,236 estimated input → 6,672; 2.390 seconds; latest call ID/arguments and diagnostic retained |
| Focused Python checks | Final combined suite: 169 passed |
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

The observation process exited successfully. These were Chat requests. Access status alone cannot establish streaming Responses acceptance: the following report exposed a later failure inside an HTTP 200 stream.

### Fresh Responses failure and repair

Production requests at 18:01 to 18:02 UTC soft-failed Sift's unsupported private input view, then failed upstream with `responses_feature_not_supported`: `additional_tools` requires the Codex Responses Lite opt-in header. Subsequent header-only attempts exposed required context fields and an existing Responses-to-Chat conversion that dropped the private input item and `reasoning.context`. Native custom-provider endpoint opt-in was also checked; it failed LiteLLM's `LlmProviders` enum validation and was removed.

The existing Codex deployment hook now selects LiteLLM's native OpenAI Responses adapter only for `additional_tools` Responses dispatches to the authenticated Codex endpoint. It retains the bare provider model, original input items and other fields, enables the one required header, and defaults omitted `reasoning.context` to `all_turns` and omitted `parallel_tool_calls` to false. Explicit caller choices remain unchanged. The existing Codex `fast` to `priority` service-tier translation is preserved. Chat/Messages and ordinary Responses retain their existing adapter, including buffered requests to the streaming-only Codex backend. This narrow provider remap can retire when LiteLLM supports native custom-provider Responses dispatch without its enum failure. No transport, retry, service or dependency was added.

Checks: 39 passed across credential, proxy HTTP and configuration tests, including a streamed public route's final outbound HTTP body with private items, required defaults, caller effort and fast-tier translation preserved, followed by an ordinary buffered Responses request through the original adapter. A direct native provider stream using the same required header and fields reached `response.completed`, returned the exact marker, and reported 28 input plus 9 output tokens. Initial malformed probe fixtures returned validation errors for omitted role/context/tool-concurrency fields; they are retained as failed setup checks, not successful inference receipts.

The bounded CLI implementation worker authored the initial hook and unit cases, then lost its handoff to a provider high-demand error. The parent completed the native adapter correction and integration regression. An independent CLI review of the final private-input boundary and buffered compatibility returned READY through the live local `default` route. Its actual upstream model was not independently established. That CLI workflow had failed before the repair. This is static code review plus a successful real CLI workflow, not proof of every Responses feature.

### Subsequent 400 and active tool-cycle recovery

At 18:20 to 18:21 UTC, staging received large Chat requests from the separate local gateway on port 11435. Sift reported `active_too_large` at roughly 875K tokens, then native provider checks rejected about 870K tokens against their 256K limits. The two-minute post-deployment watch recorded two HTTP 400 responses and two terminal proxy errors, so that watch was stopped and retained as failed. Request bodies were not retained, so the exact failing payload could not be replayed.

A bounded CLI worker established a concrete recovery bug with an offline fixture: when the latest tool result needed extraction, Sift included all earlier active tool calls in the protected floor. Old arguments could prevent recovery even though those completed cycles were removable history. Recovery now works on the latest essential cycle and replaces only that original tail, preserving indexes for subsequent historical selection. Exact instructions, the current user task and the latest tool call ID/name/arguments remain protected. The parent independently inspected the runtime diff and verified it with the deployed LiteLLM counter. The worker was stopped at its two-minute cap; the parent corrected a test assertion that named an obsolete marker without weakening the lossy-disclosure check.

The reproducible CPU case above now reduces 1.37M estimated tokens to 6,672 while preserving the latest call and diagnostic. A corresponding live staging Chat request completed with HTTP 200, exact `ACTIVE_CYCLE_OK`, terminal `stop`, 7,067 provider input tokens and 6 output tokens in 4.114 seconds. Its final Sift hook header reports the already compacted 6,710-token request, rather than the original input of the earlier auto hook. This proves the reproduced failure family; the unretained original request remains a separate replay limitation.

Staging and production were recreated sequentially after the final repair. The final watch ran after readiness from 18:30:05 to 18:40:05 UTC (600.6 seconds):

| Elapsed minutes | Inference requests in interval | HTTP / terminal errors | Router error records | Production / staging readiness |
| --- | --- | --- | --- | --- |
| 0 | 0 | 0 / 0 | 0 | 200 / 200 |
| 2 | 33 | 0 / 0 | 15 | 200 / 200 |
| 4 | 37 | 0 / 0 | 13 | 200 / 200 |
| 6 | 26 | 0 / 0 | 5 | 200 / 200 |
| 8 | 24 | 0 / 0 | 10 | 200 / 200 |
| 10 | 51 | 0 / 0 | 2 | 200 / 200 |

Total: 171 inference requests, zero client HTTP errors, zero terminal proxy/provider stream errors, and both services ready at all six checkpoints. The observation process exited successfully. A broader check of every POST path in that interval found no HTTP error, and the separate front gateway on port 11435 logged no gateway error records. There were 45 router error records from provider attempts followed by successful fallback; these are retained, not described as an absence of internal errors.

All 115 observed successful compactions stayed within 2x their selected target. Real traffic reached 1,061,199 estimated input tokens; the largest forwarded estimate was 65,528. Original failing payload replay and a full native-client matrix remain unverified as described above. This is a bounded operating result, not a guarantee against future provider failures.

## Skill learning

For large-request failures, inspect callback order and the final restored payload, not only the compactor's intermediate messages. A successful selection can still be undone by a restoration map; an added instruction can itself make compaction unsupported. Keep extraction input immutable and prove a complete request through the deployed gateway before declaring capacity verified.

For active tool histories, protect the latest required cycle rather than carrying removable old arguments into its recovery floor. Inspect the final provider request and terminal stream outcome: HTTP 200 and a correct intermediate compaction are insufficient when a later protocol bridge drops fields.
