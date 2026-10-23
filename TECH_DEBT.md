# Scoped technical debt

## Relay shared runner/supervisor helpers

Observed 2026-10-23: the optional supervisor imports instruction loading, inference environment, file locks and state-file writing from `runner.py`. A leaf-only instruction simplification initially broke supervisor startup through that shared loader. The integrated repair preserves supervisor discovery and makes only the leaf invocation opt-in; execution tests now cover both consumers.

Nonblocking follow-up: when these helpers next change, extract the shared edge utilities behind a small explicit module without changing their behavior. Preserve default leaf isolation, supervisor discovery, credential filtering, locks and private state. Ratchet with the existing tests before and after structural work. Do not add a service, generic plugin registry or alternate backend merely to resolve module ownership.

## Existing Advisor policy fixture drift

Observed 2026-10-23 during MiniMax reliability validation: four tests in tests/test_reasoning_policy.py and two test_gemini_subscription_messages_preserve_formatting variants in tests/test_thinking_format.py fail against the current mandatory-Advisor and default-alias policy. A read-only baseline restoring HEAD's deployment hook reproduces all six failures. Current MiniMax regressions pass separately. Reconcile those fixtures with the intended policy in a separate change; do not weaken runtime behavior or assertions to accept this feature. Evidence: .runs/minimax-reliability/baseline-policy.txt and baseline-gemini.txt.
