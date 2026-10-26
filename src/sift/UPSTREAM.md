# Vendored Sift provenance contract

Base: Sift 0.10.0, commit e8dc791ba76d99012a3cd43e6114c01faedeeb6a,
https://github.com/stancsz/sift.

This directory is a patched gateway fork of that pinned base, not a pristine
copy of the release. Per-file comparison against the pinned upstream blob
(see evidence below):

- `core.py`: patched, differs from the pinned base
- `litellm_proxy.py`: patched, differs from the pinned base
- `litellm_responses.py`: patched, differs from the pinned base
- `__init__.py`: identical to the pinned base

Active deployed imports come only from `src/sift`. The standalone Sift package
is not automatically shipped, and upstream Sift main is not a drop-in source
for this copy.

## Current gateway patches

1. Active tool-result and user text recovery with explicit lossy-compaction
   markers, plus exact protected IDs and instructions that are never rewritten.
2. Library-level larger intake and two-times-target CPU relaxation, with
   target and audit support. The experts adapter sets a 32,000 estimated input
   ceiling for every expert; generation is separate from that owner-defined input budget. Normal gateway
   dispatch does not enable these paths.
3. Native Responses item restoration (original item IDs and tool types), with
   opaque tool preservation and a count floor.

## Patch provenance and regressions

| File | Retained difference and reason | Focused regression evidence |
| --- | --- | --- |
| `core.py` | Bounded extractive repair for an oversized active text request and the latest active tool result. It marks loss, retains instructions/call IDs, and never edits the caller's message list. Intake remains bounded separately from the expert's 32K forwarded-input ceiling. | [OpenAI active-cycle recovery](../../tests/test_sift_active_cycle_recovery.py), [Responses preservation and opaque-tool accounting](../../tests/test_target_sift_softfail.py), and the [Experts boundary matrix](../../tests/test_expert_sift.py). |
| `litellm_proxy.py` | Uses LiteLLM's token counter and provider adapters; supports Responses selection views, opaque tool-schema floors, one bounded 25,600-to-32,000 relaxation, correlated failure accounting, and the LiteLLM 1.106.2 busy `ProxyException` needed to retain `Retry-After`. | [Responses behavior](../../tests/test_target_sift_softfail.py), [Experts input/failure/busy/cancellation checks](../../tests/test_expert_sift.py), and [Advisor transport correlation](../../tests/test_expert_transport.py). |
| `litellm_responses.py` | Converts stateless textual Responses history into a private selection view, then restores original item types, encrypted reasoning, call IDs and linkage; only selected tool-output text may be replaced with an explicit lossy excerpt. The private converter is pinned to LiteLLM 1.106.2. | [Responses preservation and output restoration](../../tests/test_target_sift_softfail.py) and [Experts protocol dispatch](../../tests/test_expert_sift.py). |
| `__init__.py` | Matches the pinned Sift blob; no local patch. | The provenance checker asserts this file remains identical. |

The tests above use deterministic token counters, stub providers, or local
LiteLLM HTTP fixtures. They are gateway regression evidence, not live provider
enforcement or a standalone Sift release suite. The current focused run is
recorded in the active goal.

## Why this fork is maintained

Sift is retained for the private input copy of an explicitly invoked Advisor
tool. Normal target traffic has no Sift callback or gateway compaction. The
tagged base does not include these gateway deltas; later upstream revisions
require parity checks. Vendoring avoids a startup install or mutable dependency.
Do not blindly replace this copy with upstream main.

## Retirement

Retire this fork only when a reviewed, pinned Sift package artifact covers all
patches above and shows parity on: the standalone Sift suite, gateway
behavioral tests in Subroute, and a live staging compaction request. Repeat the
live staging compaction request before restarting production after any update.

## Repeatable comparison

Run the checker from the Subroute repository root. It reads the commit from
this file, compares the four vendored files against Git blobs in the local Sift
clone, and emits normalized hashes plus unified diffs for the three expected
gateway patches:

    python scripts/check_sift_provenance.py --upstream-repo D:/github/Sift --output .runs/goal-simplicity-20261025/sift-upstream-source-provenance-20261025.json

The command exits nonzero if the pinned commit is missing or if the observed
patch set differs from the reviewed contract above. It checks source provenance
only, not runtime images, callback behavior, provider acceptance, or standalone
Sift tests. Runtime hashes remain a separate deployment receipt.

## Tests and evidence

- [Core recovery tests](../../tests/test_sift_active_cycle_recovery.py): retained core tool-history
  recovery, separate from normal gateway dispatch
- [Provenance-checker tests](../../tests/test_sift_provenance.py): CRLF-only normalization, pinned Git blob
  comparison, unified-diff output, and rejection when the observed patch set changes
- [Current upstream/runtime hash receipt](../../.runs/goal-simplicity-20261025/sift-upstream-runtime-after-image-isolation-20261025.json): pinned-blob,
  checkout, and all four running Gateway/Experts containers compared with one normalization method
- [Earlier runtime comparison](../../docs/evals/receipts/sift-upstream-comparison-2026-10-25.json): pre-staging-rollout
  snapshot; its container hashes are historical and are superseded by the current receipt above
- [Historical source comparison](../../docs/evals/receipts/sift-upstream-comparison-2026-10-23.json): earlier
  checkout snapshot; retained for historical context
- [Earlier review comparison](../../.runs/design-review-20261023-evidence/comparison.json): historical source snapshot

The current receipt rechecks the pinned Git blobs against this publication tree and compares normalized Sift files in
the running staging Gateway/Experts and production Gateway/Experts. Staging and production Gateway match the
checkout in all five checked files. Production Experts match three of five; litellm_proxy.py and expert_sift.py
still differ. This supersedes the pre-rollout runtime hashes in the earlier receipt. Hash parity does not prove
module load paths, callback invocation, provider acceptance, or a fresh standalone Sift run. The focused gateway boundary suite passed 76 tests; its exact output is linked
from the project goal record. Controlled staging/experts deployment is recorded
in the follow-up evaluation.

The current production Experts differences affect behavior: its `litellm_proxy.py` does not use the pinned
LiteLLM 1.106.2 `ProxyException` workaround that preserves `Retry-After` on busy Sift responses; its
`expert_sift.py` still advertises three retired Gemini aliases and does not reject duplicate case-variant
correlation headers before associating request IDs. Current checkout regressions cover the retry header,
deprecated route allowlist, duplicate correlation headers, and authenticated correlation acceptance. Do not
replace production Experts until the separate staging Advisor and terminal-accounting acceptance gate passes.

\r\n
