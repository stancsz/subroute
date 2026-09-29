# Uncommitted change review

Date: 2026-09-29. Scope: the complete working-tree implementation, configuration,
tests, and accompanying historical evidence based on commit `c0a4aac`.
The same agent performed this review, repairs, and verification.

## Findings repaired before publication

| Finding | Failure and repair | Regression evidence |
| --- | --- | --- |
| Gemini structured output | Non-object JSON raised `AttributeError`; fenced malformed output could be accepted as plain text; an incorrect JSON decoder offset could skip a conflicting result. Validate shapes before access, restrict the plain-text exception, and advance to the decoder's absolute end offset. | Six new malformed-output cases failed before the fix and pass afterward. |
| Gemini tool history | Valid OpenAI assistant tool calls with `content: null` were rejected. Treat null as empty only for assistant messages that carry tool calls. | Null-content call/result history is retained. |
| Bridge response validation | Null/non-object success payloads and usage could escape as internal errors. Validate the envelope and usage before returning a provider result. | Three malformed success-envelope cases fail as 502. |
| Terminal result integrity | The bridge could use text from one terminal result and usage from another. Missing usage was logged as success, then returned as 400 or disconnected the client. Require one successful terminal result, classify missing usage as 502, and log success only after validation. | Real local HTTP bridge tests cover missing usage; duplicate terminal results are rejected. |
| Target versus advisor profile | Plain target requests selected the concise advisor agent because profile selection depended on the presence of a tool schema. Use an explicit internal advisor flag; all target requests use the target profile. | Command selection and advisor dispatch tests exercise both roles. |
| Process cleanup | On Linux, an exited CLI parent could leave a child holding its pipes; the cleanup path then blocked past the deadline. Signal the process group even after the parent has exited. | A real subprocess regression reproduced a three-second overrun before the fix and passes in the Compose image afterward. |
| Schema validation network work | The installed jsonschema resolver can fetch caller-supplied remote references synchronously. Reject remote references before provider dispatch and validate with a registry that does not retrieve remote resources. | Remote references are rejected before dispatch; local fragment references still validate. |
| Provider status | Capacity and inventory failures were presented as sign-in failures. Represent authentication as unknown for those failures and show unavailable status. | Capacity/timeout status and control-card mapping tests. |
| Auto routing contract | A stale test required an empty fallback list and README force-mode wording omitted the explicit Auto exception. Update the contract and exercise LiteLLM's real fallback traversal. | Auto under alias/force/off; first/second/third-provider success; exhaustion; fixed-route fail-closed. |

## Deliberate compatibility boundary

The installed Antigravity CLI 1.2.11 exposes no output-token-cap flag in its help.
During this review the owner chose to document backend-managed Gemini output
limits, preserving the existing Messages path. README and the control desk now
state that client caps are not enforced. Tests preserve completed output and
provider-reported usage beyond a caller's cap for both buffered and SSE responses.
This does not claim that the provider honored the cap. Codex's existing documented
backend-managed limit behavior remains unchanged.

LiteLLM continues to own public protocols and Auto fallback traversal. The changes
add no service or transport. JSON Schema validation uses the already required
jsonschema/referencing packages. The internal advisor flag selects between the
two existing constrained CLI profiles. The temporary scripts, copied test tools,
and local policy receipts under `tmp/` are retained locally and excluded from Git.

## Verification

- Locked Windows environment, LiteLLM 1.101.0: **286 passed, 11 skipped**, 26
  dependency warnings. Ten skips are opt-in provider tests; one is the POSIX-only
  process-group regression, which passed in Docker.
- Pinned Compose image, LiteLLM 1.103.0 / jsonschema 4.26.0: **263 passed, 10 skipped**,
  26 dependency warnings. The container had networking disabled and a read-only
  repository mount with temporary policy storage. The 24 host-only skill tests
  were excluded because the runtime image has no Git executable; they passed in
  the Windows suite. The first broad Docker run exposed that tooling limitation,
  not a gateway test failure.
- Ruff for changed implementation and review test files, lock consistency,
  Compose configuration, and whitespace checks passed.
- Model inventory now reuses the same bounded process-group runner as generation.
  After that final consolidation, bridge/status regressions passed 32 tests on
  Windows (one POSIX skip) and all 23 bridge tests in the pinned Linux image.
- The pinned Antigravity image built successfully; its service was recreated and
  both gateways restarted. Both readiness endpoints returned healthy with their
  databases connected. Both inventories exposed 21 model IDs, including Auto and
  Luna Advisor. Unknown-model requests returned 404 and unsupported embeddings
  returned 400 before provider dispatch. Both served the updated control assets.
- No provider generation was requested during this review. Historical live
  provider receipts remain historical evidence; these new deterministic results
  do not establish fresh subscription compatibility or external fault coverage.

## Runtime policy observation

The review sent no routing-policy mutations to either running gateway. Staging
remained at version 458 with Codex Luna, force mode, and Gemini advisor. During
the review, production changed from MiniMax/version 65 to OpenRouter/version 66,
retaining force mode and Luna advisor. The cause was not established. The newer
selection was preserved instead of restoring the earlier snapshot. Production
and staging therefore cannot both be described as unchanged during this review.

## Remaining boundaries

The optional Windows and deployed LiteLLM versions intentionally differ; both
affected execution environments were tested. Production and staging still share
the Antigravity subscription state. Gemini SSE remains buffered, vision remains
unsupported, and subscription output limits remain backend-managed. Commercial
readiness, broad client compatibility, and live provider auth/quota/outage fault
injection are not established by this publication review.
