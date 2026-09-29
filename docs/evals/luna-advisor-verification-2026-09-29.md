# Luna advisor live verification

Date: 2026-09-29

## Result

A real GPT-6 Luna consultation completed through LiteLLM's native advisor
orchestration with MiniMax M3 as the executor. The final traced request made one
Luna call, injected its returned guidance, and completed the MiniMax answer.
The configured `max` advisor reasoning effort was not verified.

Receipt: [Live diagnostic trace](../reports/luna-advisor-verification-2026-09-29.json).

| Observation | Result |
| --- | --- |
| Saved production policy | `minimax`, `force`, `codex-luna-advisor`, effort `max`, version 65 |
| Deployed LiteLLM | 1.103.0 |
| Observed advisor model | `gpt-6-luna` |
| Advisor provider usage | 373 input tokens, 450 output tokens |
| Advisor elapsed time | 9.14 seconds |
| Guidance injection | One `advisor` tool result, 965 characters |
| Executor completion | MiniMax M3, `end_turn` |
| Entire traced request | 16.80 seconds |
| Effort received by advisor adapter | `null`, despite saved policy `max` |

## Method and evidence boundary

A production HTTP Messages request asking MiniMax to consult its advisor
completed with HTTP 200. That response alone did not identify an actual advisor
invocation: the native interceptor returns only the executor's final answer.

The stronger evidence came from a separate diagnostic Python process inside the
running production gateway container. It loaded the current saved policy, the
configured MiniMax and Luna deployments, installed LiteLLM, and the existing
routing and advisor hooks. Observational wrappers recorded the real Codex
subscription collector's response and the native advisor-result injection.
Provider requests were real; neither response nor usage was mocked. The test
question concerned a synthetic async configuration race, not private user data.

This was not instrumentation of the production HTTP worker. The diagnostic used
the deployed working tree, which included pre-existing uncommitted implementation
changes. This evidence-only commit does not publish those changes or establish
that its Git revision alone reproduces the deployed implementation.

The final diagnostic assertions confirmed one Luna call and one guidance
injection, but the assertion requiring `reasoning_effort == "max"` failed.
Accordingly, this is a successful live consultation and injection check, not a
fully passing configuration check. It does not prove which reasoning effort the
separate production HTTP worker sends. That discrepancy remains unresolved.

Provider token counts above cover the advisor call in the final trace. The
executor's reported usage is retained separately in the receipt and is not an
aggregate cost or token total for the investigation.

Production policy remained at version 65. The post-check readiness endpoint
returned `healthy` with PostgreSQL `connected`. No production configuration,
application source, or running service was changed by this verification.
