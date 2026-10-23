# Scoped technical debt

## Relay shared runner/supervisor helpers

Observed 2026-10-23: the optional supervisor imports instruction loading, inference environment, file locks and state-file writing from `runner.py`. A leaf-only instruction simplification initially broke supervisor startup through that shared loader. The integrated repair preserves supervisor discovery and makes only the leaf invocation opt-in; execution tests now cover both consumers.

Nonblocking follow-up: when these helpers next change, extract the shared edge utilities behind a small explicit module without changing their behavior. Preserve default leaf isolation, supervisor discovery, credential filtering, locks and private state. Ratchet with the existing tests before and after structural work. Do not add a service, generic plugin registry or alternate backend merely to resolve module ownership.

## Existing Advisor policy fixture drift

Observed 2026-10-23 during MiniMax reliability validation: four tests in tests/test_reasoning_policy.py and two test_gemini_subscription_messages_preserve_formatting variants in tests/test_thinking_format.py fail against the current mandatory-Advisor and default-alias policy. A read-only baseline restoring HEAD's deployment hook reproduces all six failures. Current MiniMax regressions pass separately. Reconcile those fixtures with the intended policy in a separate change; do not weaken runtime behavior or assertions to accept this feature. Evidence: .runs/minimax-reliability/baseline-policy.txt and baseline-gemini.txt.

## Relay leaf terminal discipline

Observed in the owner-authorized native Docker stress follow-up, 2026-10-23: one whole work/review goal passed, but the second round's worker exhausted its CLI turn bound after passing tests, pursuing extra environment/receipt reconnaissance and receiving permission denials. Successful reviews also repeated checks. Keep this model-behavior risk separate from reliable lease/cancellation/acceptance enforcement. The current assignment includes fleet-wide goal acceptance in leaf context, which may encourage role drift; causality is unverified.

Follow-up only with a concrete task-contract/runtime change and a distinguishing bounded live check, preserving goal alignment, separate review, failed history and current caps. Do not add orchestration services or keep rerunning the unchanged task until it passes. Evidence: docs/evals/relay-2026-10-23.md and relay-live-stress-2026-10-23.json.

Leaf-contract mitigation, 2026-10-23: the prompt now separates coordinator-owned goal acceptance and receipt persistence from the leaf assignment/local checks, preserving all goal context. The reviewed change passes 53 tests and all 11 Docker fault groups. One unchanged-cap native probe completed four accepted tasks with no observed coordinator-state reconnaissance; local post-test checks and three permission denials remain, including a denied broad-root/GOAL search before testing. This mitigates the observed failure mode in one probe and does not close repeatability, physical-device, headless-default or sustained acceptance. Original failed evidence remains in the linked metrics.
