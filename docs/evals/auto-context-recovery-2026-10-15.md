# Automatic history recovery through local port 11435

The owner requested gateway-side recovery when Claude Code is stuck beyond a
route's context window. The earlier local failures reached roughly 657K
estimated tokens against a 256K route; returning a clearer error alone did not
recover the request.

## Implementation and ownership

The existing Sift core and LiteLLM Proxy integration already supported bounded
Chat Completions and Anthropic Messages compaction, but only Qwen had a hook.
`subroute.sift_plugin.sift_auto_hook` now enables the same core on `auto`, after
route resolution and before the advisor and provider capacity checks. Explicit
model routes and Responses requests keep their existing behavior.

The adapter reads the concrete LiteLLM deployment's configured input limit,
context window and compaction threshold. Its input target is their minimum,
with requested output subtracted from the window and a further 8,192-token
reserve for later gateway guidance. Missing Chat output limits reserve 65,536
tokens without changing the provider request. Sift counts system instructions
and tools through the deployed LiteLLM counter. Intake is bounded to 1M
estimated tokens and two CPU jobs; excess concurrent work returns 429.

Instructions, tool schemas, output caps, the active task and newest whole tool
cycle remain exact. Older text is replaced by explicitly labeled, lossy source
excerpts. Unsupported media and unshrinkable required content fail before
inference. The local forwarder preserves `x-sift-compacted`,
`x-sift-estimated-input-tokens`, and `x-sift-estimated-forwarded-tokens`, including
CORS exposure. It safely reads known context recovery fields from LiteLLM's
`provider_specific_fields.error` wrapper without returning provider diagnostics.

No core compaction algorithm, dependency, service, database state or generation
retry was added. Sift itself performs no provider call; existing advisor policy
continues to apply. The narrow maintenance burden is the auto route's budget
adapter and the forwarder's audit/error allowlists. The adapter can be retired
when Sift's native hook owns equivalent deployment/output-aware budgets.

## Live evidence

The opt-in probe is `tests/acceptance/auto_context_recovery.py`. It sends a
synthetic tool history through the real 11435 entry point, then repeats with one
additional tool cycle. Every response streamed to its normal terminal outcome
and reported the correct earliest and newest failure identifiers.

| Protocol / cycles | Estimated incoming | Estimated forwarded | Seconds | HTTP |
| --- | ---: | ---: | ---: | ---: |
| Chat / 12 | 647,648 | 2,262 | 2.49 | 200 |
| Messages / 12 | 647,624 | 3,028 | 3.38 | 200 |
| Chat / 13 | 647,868 | 2,101 | 4.17 | 200 |
| Messages / 13 | 647,842 | 3,260 | 2.36 | 200 |

Two final small streaming requests returned HTTP 200, the requested answer and
normal terminal events with `x-sift-compacted: false`. A single oversized active
user message returned HTTP 400 with `outbound_context_limit_exceeded` and
protected-content recovery guidance in both protocol shapes.

The first live pass caught use of LiteLLM's model-ID lookup for a group alias.
The implementation now uses concrete deployment metadata, with a real-Router
regression test. Live failure probes also established the native structured
error wrapper that the local forwarder now handles.

Only the staging gateway behind 11435 and the local forwarder were restarted.
The saved staging policy remained exactly unchanged at version 960, with its
selected Sol target and Gemini advisor. The explicit `auto` route preserves its
existing provider chain. Shared source/config will also affect production's
auto route after a future production restart; production was not restarted.

## Automated checks and limits

Subroute: 752 passed, 39 skipped. Local gateway: 208 passed, zero failed, and
`tsc --noEmit` passed. The host test dependency is LiteLLM 1.101.0; the live
pinned container reports 1.103.0. Both paths were exercised. No dependency
upgrade was included in this change.

The live corpus is highly repetitive synthetic text. These results establish
transport recovery, output-budget accounting and selected failure retention,
not broad recall or arbitrary task quality. Sift is lossy. Requests containing
unsupported media, instructions or tool-call structure that cannot fit, or
intake over 1M estimated tokens still fail explicitly. Responses API state is
not compacted. The gateway does not alter client history, so later oversized
requests run compaction again.

## 2026-10-20 oversized active tool results

Staging remained up with zero container restarts and a healthy staging database.
Its logs showed 14 Chat Completions requests rejected with
`outbound_context_limit_exceeded`, `context_pruning_failed`, and a 196,608-token
input target. LiteLLM's public error did not expose Sift's internal rejection
reason, so the exact request shape could not be confirmed from those logs.

The failure path could occur when active tool-result text left too little
context: Sift correctly discarded older history but required active results to
remain byte-for-byte intact. Sift now excerpts only as many large text results
as needed as a last recovery step, with no model call or request retry. It
preserves the latest user task, instructions, output budget, tool calls, result
blocks, and IDs. Each changed result contains an explicit warning that the
excerpts are incomplete and omitted details may matter. If the task,
instructions, tool-call structure, non-text result content, or omission markers
still do not fit, the request continues to fail closed. The client keeps its
original history; the model can request the tool again or open the source.

The adapter also logs the internal Sift failure category, protocol, configured
input target, and intake ceiling without logging message contents. Focused
offline verification passed `tests/test_auto_context_recovery.py` (22 passed),
including oversized active tool-result recovery over both Chat Completions and
Anthropic Messages, preserved protocol IDs and output limits, explicit omission
markers, and unchanged caller input. The staging gateway was restarted on
2026-10-20 without recreating the Compose service; authenticated
`/health/readiness` returned HTTP 200, and the container remained running with
zero restarts. No model request was sent. Instead, the deployed
`compact_proxy_request` was called inside the staging container using its
LiteLLM 1.103.0 counter. Oversized 212,192-token Chat and 212,191-token
Anthropic active tool histories compacted to 240 and 239 tokens; each retained
the failure identifier, tool ID/result structure, and explicit omission marker.
A separate synthetic irreducible 203,915-token user message was rejected before
inference with HTTP 400, and the gateway logged `reason=active_too_large`,
`target=182272`, and input count without logging prompt contents. These checks
exercise the deployed compaction code and counter without spending provider
tokens; a full provider generation was not sent.
