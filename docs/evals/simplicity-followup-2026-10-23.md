# Simplification follow-up, 2026-10-23

Checkpoint `53681a7` is published on `codex/simplify-gateway`. This follow-up closes two lifecycle gaps and exposes the private Sift audit. Whole-goal and production acceptance remain open.

LiteLLM 1.106.2 invokes the deployment hook before native Messages Advisor interception. The old outer orchestration receipt could remain pending after success. The fix uses LiteLLM's resolved provider and interception predicate to avoid creating that orchestration-only receipt, preserving actual native Anthropic receipts. Native buffering also omits failure hooks when cancelled before response headers. A request-local list now lets that native handler settle its unfinished attempts through the existing store on cancellation. Completed records retain measured facts. No provider loop, retry, transport or store is added; both workarounds document retirement conditions.

The expert wrapper delegates Sift's existing response-header hook. It strips caller-supplied reserved Sift audit metadata on the private request before compaction or multimodal bypass, preventing false compaction headers on skipped requests. Normal dispatch still has no Sift callback. Every expert retains the 32,000-token input policy, including Gemini; output is separate.

## Checks

- Exact selected source: **981 passed, two environmental skips, 35 live tests deselected**, 98.47 seconds. Command: `python -m pytest -q -m "not live" -p no:cacheprovider --basetemp=<task-owned-temp> --tb=short --disable-warnings tests`. Python 3.13.11, locked LiteLLM 1.106.2/MCP 2.3.0, offline cost metadata and cached tokenizers. [Console receipt](receipts/simplicity-followup-tests-2026-10-23.txt).
- Integrated original working-tree focused suite: 48 passed. Independent actual Router/native Advisor/SDK HTTP fixtures cover success, provider error, pre-response cancellation, concurrent requests, cancelling one concurrent request, and preserving completed executor/expert records when a later executor is cancelled. Every created row is inspected. Fixture usage is synthetic.
- Independent parent checks reject forged audit metadata in either or both metadata carriers, preserve original input and verify native header delegation. All nine expert aliases passed offline compaction with the real hook/native counter and unchanged original payload bytes.

## Deployment and real requests

Staging and experts run immutable image `sha256:24f4c0d751fedaf65f97811712ce8a16b8025831f2e85cdfda351f6c0edabff7`, with baked source/configuration, authenticated catalogs and stable zero-restart containers. Production was not restarted. Policy bytes, auth boundaries and other service lifecycles were checked per rollout. This integration image preserves unrelated local durable-outbox code, so its identity is separate from the clean selected Git tree.

| Explicit scenario | Observed outcome | Acceptance |
| --- | --- | --- |
| Messages, output limit 64 | HTTP 200, message_stop, max_tokens, 64 reported output tokens; expected answer marker absent. One provider plus one bridge, both complete; no expert/retry/fallback. | Response check failed, accounting closure passed. Failed result retained. |
| Separate reviewed Messages scenario, output limit 256 | HTTP 200, expected answer, end_turn and message_stop. One provider plus one bridge, both complete and correlated; no expert/retry/fallback. | Passed. |
| Codex Luna private expert | Expected answer, stop terminal; Sift estimated input 33,213 to 15,863; input/output as reported 15,693 / 7. | Full response, compaction and input-budget checks passed. |
| Gemini 3.8 Flash private expert | Stop terminal and the same Sift estimates; input/output as reported 19,460 / 283; exact-answer marker false. | Compaction/input evidence present; response check failed. Whole two-provider harness failed. |

Expert calls used separate private requests and retained identical client payload bytes before/after dispatch. Codex explicitly requested 256 output tokens, within its existing 1,024 ceiling; Gemini had no invented output cap. The expert harness made exactly two client POSTs and did not retry. Reported usage is distinct from Sift estimates and does not independently establish billing. The safe Gemini receipt contains no answer text, so the failure's cause is unverified. No additional expert probe is claimed.

## Scope and remaining work

[Sanitized receipts](receipts/simplicity-followup-2026-10-23.json) retain failed and successful scenarios. The [current source ledger](receipts/simplicity-current-source-2026-10-23.json) reports **1,639 fewer maintained source lines** over its explicit 20-file scope. It retains original per-file historical baseline hashes/counts, adds the selected accounting changes against Git base `df7de14`, and computes current after-hashes from the selected publication tree. Configuration, tests, docs and unrelated dirty work are excluded from the reduction.

Independent source/fixture review accepted the lifecycle fixes; independent parent review accepted the expert audit repair. Live receipt reconciliation is a separate check. Investigate the Gemini exact-answer gap with adequate diagnostic evidence, finish remaining context/protocol acceptance and the full-goal audit, and handle production promotion separately. Do not mark the whole goal complete or silently repeat probes until a result passes.
