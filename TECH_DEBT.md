# Scoped technical debt

## Gateway startup and source isolation

Observed during the 2026-10-23 Sift simplification: importing `dynamic_router.py` constructs the routing control plane, may create or migrate its saved-state file, and registers routes. This couples source inspection and test setup to startup effects. It is a design risk, not evidence of a new runtime failure. When startup ownership next changes, move these effects to explicit application initialization with the existing LiteLLM lifecycle; preserve route registration and atomic validated state. Do not introduce a service or general plugin framework.

The shared source/config bind risk is addressed in the current Compose source: the locked image contains application code and three static configs; runtime binds retain only existing credentials and state. Production's original host policy is preserved through `/app/legacy-config`, and staging retains its named state volume. The independently reviewed immutable candidate passed protocol/authentication and legacy-policy persistence checks. Only staging has been recreated with that image; production and experts still require their separately gated rollout. Do not treat a Compose edit as migration of those running roles. See [acceptance and boundaries](docs/evals/sift-advisor-only-2026-10-23.md).

## Relay shared runner/supervisor helpers

Observed 2026-10-23: the optional supervisor imports instruction loading, inference environment, file locks and state-file writing from `runner.py`. A leaf-only instruction simplification initially broke supervisor startup through that shared loader. The integrated repair preserves supervisor discovery and makes only the leaf invocation opt-in; execution tests now cover both consumers.

Nonblocking follow-up: when these helpers next change, extract the shared edge utilities behind a small explicit module without changing their behavior. Preserve default leaf isolation, supervisor discovery, credential filtering, locks and private state. Ratchet with the existing tests before and after structural work. Do not add a service, generic plugin registry or alternate backend merely to resolve module ownership.

## Existing Advisor policy fixture drift

Observed 2026-10-23 during MiniMax reliability validation: four tests in tests/test_reasoning_policy.py and two test_gemini_subscription_messages_preserve_formatting variants in tests/test_thinking_format.py fail against the current mandatory-Advisor and default-alias policy. A read-only baseline restoring HEAD's deployment hook reproduces all six failures. Current MiniMax regressions pass separately. Reconcile those fixtures with the intended policy in a separate change; do not weaken runtime behavior or assertions to accept this feature. Evidence: .runs/minimax-reliability/baseline-policy.txt and baseline-gemini.txt.

Optional-Advisor validation also reproduced `tests/test_usage_accounting.py::test_advisor_and_compaction_roles_keep_observed_usage` against unchanged HEAD's Advisor plugin: its mock router lacks `_count_pre_call_check_tokens`, which the existing compaction path requires. Leave the runtime boundary intact and reconcile this accounting fixture separately. Evidence: `.runs/minimax-reliability/advisor-optional-head-baseline.txt`; the changed-code run passed the other 173 checks, including the independent malformed-usage acceptance cases.

## Relay leaf terminal discipline

Observed in the owner-authorized native Docker stress follow-up, 2026-10-23: one whole work/review goal passed, but the second round's worker exhausted its CLI turn bound after passing tests, pursuing extra environment/receipt reconnaissance and receiving permission denials. Successful reviews also repeated checks. Keep this model-behavior risk separate from reliable lease/cancellation/acceptance enforcement. The current assignment includes fleet-wide goal acceptance in leaf context, which may encourage role drift; causality is unverified.

Follow-up only with a concrete task-contract/runtime change and a distinguishing bounded live check, preserving goal alignment, separate review, failed history and current caps. Do not add orchestration services or keep rerunning the unchanged task until it passes. Evidence: docs/evals/relay-2026-10-23.md and relay-live-stress-2026-10-23.json.

Leaf-contract mitigation, 2026-10-23: the prompt now separates coordinator-owned goal acceptance and receipt persistence from the leaf assignment/local checks, preserving all goal context. The reviewed change passes 53 tests and all 11 Docker fault groups. One unchanged-cap native probe completed four accepted tasks with no observed coordinator-state reconnaissance; local post-test checks and three permission denials remain, including a denied broad-root/GOAL search before testing. This mitigates the observed failure mode in one probe and does not close repeatability, physical-device, headless-default or sustained acceptance. Original failed evidence remains in the linked metrics.

## Relay supervisor cycle boundary and learning reuse

The headless supervisor now has an explicit finite-cycle/final-handoff instruction. Provider-free success/failure, reservation, unused-budget inactivity and cleanup checks pass; one native six-turn missing-peer cycle returned successfully with no post-pause tool calls. The earlier failed cycle remains failed. The polling host persists the latest final reply, but does not automatically feed it into the next model prompt. Known evidence/learning should be included in the first authorized goal record when allowed; do not claim a durable general learning system or add a new history store without a concrete retention/input contract. Independent direct native transcript verification remains blocked by reviewer file permissions; no permission widening or raw-trace export is authorized as a workaround. Productive multi-cycle, normal-24 and sustained acceptance remain outstanding. See the existing Relay evaluation and metrics.
