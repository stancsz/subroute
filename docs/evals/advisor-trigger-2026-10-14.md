# Configurable Advisor trigger

Date: 2026-10-14

## Requirement and existing mechanism

The user wants to control how often an Advisor is consulted, use a fast local complexity classifier when useful, and still allow the target model to ask for advice before its final answer. The deployed LiteLLM 1.103.0 advisor interceptor offers a synthetic tool to the target, then performs a subcall only if the target selects that tool. Subroute already has a bounded preconsultation path for selected target and Advisor pairs. The gap is that the synthetic tool gives the gateway no control over consultation frequency and a live MiniMax/Gemini trial did not select it.

## Design

The existing routing state now owns `advisor_trigger`, `advisor_sensitivity`, and `advisor_classifier`. Existing saved policies default to `model`, preserving their behavior. `adaptive` classifies before target dispatch and preconsults only above threshold. `hybrid` does the same and also leaves LiteLLM's native advisor tool available, including after a preconsultation. `always` preconsults. `No advisor` remains the off switch. A single request captures one policy snapshot before routing and Advisor hooks.

The optional local classifier calls the existing Ollama endpoint with JSON schema output, thinking disabled, zero temperature, a two-call semaphore per gateway, an 8.5-second overall deadline, and at most 6,000 characters from the latest user turn. The default local model is `qwen3.5:4b`, configurable with `ADVISOR_CLASSIFIER_MODEL`; no new service, dependency, or persistent store was added. On timeout, connection failure, invalid JSON, or abnormal terminal result, structural rules decide. The fallback is explicit in request metadata and gateway logs. Production and staging retain separate policy files while sharing the configured Ollama endpoint and model resources.

Sensitivity thresholds are levels 3, 2, and 1 for conservative, balanced, and frequent. The local classifier grades intent from 0 to 3, with recent tool history setting a minimum of 2. Rules-only mode uses length, code or diff blocks, listed steps, and recent tool history. The model and rules are advisory frequency controls, not validated measures of intrinsic request difficulty. Hybrid can spend on both preconsultation and a later native advisor subcall; the latter retains LiteLLM's configured `max_uses` cap.

## Verification

- The full non-live Windows suite passed: 716 passed, 39 skipped. Compose configuration parsed. Focused classifier, Advisor, and routing tests passed 107 checks after the final prompt-excerpt and fallback-log edits.
- On staging OpenRouter + Gemini, adaptive/balanced/local returned a simple Messages request with HTTP 200 and no Advisor usage receipt. A complex Messages request produced a Gemini Advisor receipt and a target response. Repeating with a larger output budget ended with `end_turn`; the target answered substantively instead of returning the requested marker. This verifies consultation and response completion, not adherence to an exact-output instruction.
- Staging hybrid/balanced/local produced one preconsultation and a target response with HTTP 200, but the first sampled 1,024-token response ended at `max_tokens`. A subsequent 4,096-token request completed with `end_turn` and nonempty text. The target did not choose a second native Advisor call in either sample. Unit tests verify that the hybrid request includes the native Advisor tool both below and above the classifier threshold.
- Staging also tested the production target/Advisor pair, MiniMax M3 with Codex Luna, in hybrid/balanced/local mode. It returned HTTP 200, `end_turn`, nonempty text, and one completed Luna Advisor receipt. The target did not choose a second native Advisor call in that sample. Staging routing and Advisor fields were restored afterward.
- The staging control desk displayed the three new settings. Selecting hybrid saved policy v642 and enabled sensitivity and classifier controls; restoring model mode saved v643 and disabled them. Later live checks restored staging's original policy fields at v651.
- Both Compose gateways were recreated, restarted on the final source, and reported readiness HTTP 200. Before activation, production retained MiniMax/force/Luna/low at policy v98 and staging retained OpenRouter/force/Gemini/high at policy v651, both with `model` trigger. The user then selected production activation: its policy became v99 with `hybrid` trigger, `balanced` sensitivity, and `local` classifier. MiniMax/force/Luna/low stayed selected, production readiness remained HTTP 200, and staging remained at v651 with `model` trigger at that point.
- At the later monitoring check, the user had changed both saved policies to `hybrid` / `frequent` / `local` (production v102, staging v653). In the initial three-minute staging log window, 38 requests used `/v1/chat/completions`; there were no `/v1/messages` requests or advice injection logs. Production had no inference requests in the corresponding observation window. The implementation intentionally gates these settings to Anthropic Messages, so that Chat Completions traffic did not exercise the trigger.
- During continued staging monitoring, two Messages requests preconsulted Gemini Subscription at high effort for the OpenRouter target. The gateway logged `advisor advice_injected` at 20:58:37 UTC (3,510 input and 1,988 output provider tokens) and 20:59:07 UTC (2,548 input and 928 output provider tokens). The staging usage database recorded two complete Advisor receipts with provider usage. The corresponding `/v1/messages` requests completed HTTP 200 at 20:58:46 and 20:59:14 UTC. This verifies consultation and completed target requests under the user's current staging policy; it does not measure the advice's effect on answer quality. Two earlier Messages requests returned 200 without advice injection, which can be expected below the classifier threshold or for an unsupported request shape; their decision reason was not retained in these logs.

The local model choice has only a small calibration sample: on eight authored English/Chinese prompts, `qwen3.5:4b` matched six labels exactly and `qwen3.5:0.8b` matched five. That sample does not establish broad classification accuracy. Local model availability and latency depend on the host; the bounded rules fallback is part of the intended behavior. No live auth, quota, or Ollama outage injection was performed.

## Sources

- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
- [Ollama chat API](https://docs.ollama.com/api/chat)
- [Ollama Qwen 3.5 models](https://ollama.com/library/qwen3.5)
