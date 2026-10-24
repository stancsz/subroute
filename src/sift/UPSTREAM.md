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

Compare each vendored file against its pinned upstream blob in the local Sift
clone, not against standalone Sift main:

    git -C D:/github/Sift show e8dc791ba76d99012a3cd43e6114c01faedeeb6a:src/sift/core.py

Normalize both sides the same way (UTF-8 text, CRLF to LF only), then hash and
diff the normalized files (sha256, then diff). Per-file upstream and vendored
hashes and identical flags are recorded in the evidence file.

## Tests and evidence

- [Core recovery tests](../../tests/test_sift_active_cycle_recovery.py): retained core tool-history
  recovery, separate from normal gateway dispatch
- [Current comparison receipt](../../docs/evals/receipts/sift-upstream-comparison-2026-10-23.json): pinned-blob
  hash comparison (exact commands and normalized hashes)
- [Earlier review comparison](../../.runs/design-review-20261023-evidence/comparison.json): historical source snapshot

The current receipt rechecks the pinned Git blobs against this publication tree.
It does not claim a fresh standalone test run. Gateway checks and controlled
staging/experts deployment are recorded in the follow-up evaluation.
