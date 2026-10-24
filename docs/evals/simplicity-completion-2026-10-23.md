# Callback reuse and reliable expert answers

The normal path remains routing plus LiteLLM admission/provider dispatch. Sift remains at the private 4040 expert boundary with a 32,000-token input policy for all nine aliases, including Gemini. Output is separate. This follow-up fixes two observed integration failures and reduces duplicated callback plumbing.

## Changes and failure mechanisms

`ExpertSiftHook` now reuses `SiftLiteLLMHook` directly: one initialization and one implementation of compaction, audit and CPU/cancellation behavior. LiteLLM 1.106.2 discovers response-header hooks only in the concrete class, so one explicit method alias exposes the inherited implementation. Its comment records the native gap and retirement condition. The intermediate candidate without that binding compacted successfully but lost headers; its failed live receipt is preserved. Direct-method parity had missed discovery, so the maintained regression now exercises actual native capability discovery and dispatch. Six independent native HTTP cases and a negative subclass control cover that boundary.

A separately instrumented request confirmed that the CLI plain response contained a `toolAction/toolSummary` JSON prefix before the requested answer. Non-audio Antigravity answers now use the CLI's existing parsed schema output and the adapter's existing strict text-decision validation. No input prompt change, regex cleanup, trimming, new parser, transport, retry or service was added. Exact nonblank whitespace/Unicode/escapes are preserved. Blank/invalid plain results fail closed. Caller-tool decisions and audio retain their existing contracts. This is deliberate output translation; existing labeled-role CLI input is unchanged and is not native system/developer role transport.

## Verification

- Final selected source: **989 passed, two environmental skips, 35 live tests deselected**, 97.77 seconds. Command: `python -m pytest -q -m "not live" -p no:cacheprovider --basetemp=<task-owned-temp> --tb=short --disable-warnings tests`. Locked Python/LiteLLM/MCP environment, offline metadata and cached tokenizers. [Console receipt](receipts/simplicity-completion-tests-2026-10-23.txt).
- Behavior-preserving callback comparison: 25 focused checks before/after, 27 nine-alias/three-protocol direct-result comparisons. Those direct comparisons alone did not prove hook discovery. Final native discovery checks: 18 passes, six actual ASGI HTTP positives and a negative inherited-only control.
- Plain-answer verification: independent real loopback HTTP bridge fixtures replace only the CLI subprocess. They cover contaminated prose plus authoritative native output, exact text/usage, missing/mismatched/invalid/blank results, unchanged tool schemas and audio. Final changed-boundary run: 89 passes. The preserved original-worktree broad run passed 998 before the final one-line leaf binding; its final private-hook focused checks are separate, not relabeled as that broad run.
- The [current ledger](receipts/simplicity-current-source-2026-10-23.json) records **1,641 fewer maintained source lines across 21 explicit paths**. Tests, docs, config/build/data and unrelated dirty work are excluded. Per-file baseline and current hashes/counts remain explicit.

## Current staging evidence

Staging and experts run reviewed immutable image `a99773d74df3`, with 42 baked source/config files and two runtime-lock files independently verified. Policy/auth boundaries are preserved, both containers have zero restarts, and source/config mounts do not shadow the image. The integration image retains unrelated local outbox work; exact selected-Git-image identity is not claimed. Production remains its prior image and lifecycle, with host mounts; its loaded new behavior is not certified.

| One explicit scenario | Observed result |
| --- | --- |
| Same prior synthetic Gemini input | HTTP 200/stop, exact `EXPERT_OK`, unchanged 189356-byte body. Sift headers: 33213 estimated input to 15863 forwarded. Usage as reported: 17075 input / 41 output. Combined acceptance passed. |
| Model-selected consultation | One client Messages request, native auto tool choice, final `ROUTE_OK`/end_turn/message_stop. Exactly two MiMo inference HTTP attempts and one private expert HTTP attempt, plus two protocol bridges. All five correlated records complete with observed usage; one private Messages access; no retry/fallback or policy/lifecycle changes. |

The second scenario is caller-requested optional consultation, not automatic gateway advice. Transport records do not independently count the CLI's internal vendor generations or establish billing. These are bounded selected-route observations, not universal latency, quota, all-provider or sustained-load guarantees. Earlier failed probes remain failed in [portable evidence](receipts/simplicity-completion-2026-10-23.json).

## Scope limits

Existing context admission and media/bridge byte limits remain. A generic pre-parse HTTP body-size limit was not configured before or after this work; the installed native middleware is premium-gated. That pre-existing hardening item is recorded separately rather than bypassing licensing or adding a proxy/middleware during simplification. No universal HTTP byte-cap claim is made.

The active goal retains every original acceptance requirement and links the verbatim execution history, including superseded combined-context questions. Source publication and any production activation remain distinct steps; no automatic production restart occurred.

## Independent acceptance evidence

The independent reviews are retained in this repository: [native hook discovery](simplicity-native-hook-review-2026-10-23.md), [native plain text](simplicity-native-text-review-2026-10-23.md), [actual model-selected cycle](simplicity-model-selected-review-2026-10-23.md), and [ingress baseline challenge](simplicity-ingress-review-2026-10-23.md). The original worktree final focused run passed 25 checks in 8.92 seconds.

## Skill learning

Direct method parity does not prove a framework will discover inherited hooks. After this demonstrated gap, verify the installed framework's discovery and actual HTTP path; preserve the failing candidate and use one concrete binding with a retirement condition. For CLI answers, prefer existing native structured output over cleaning up provider prose. These methods apply to the pinned runtime and observed boundaries; they do not establish universal compatibility.
