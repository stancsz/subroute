# Gateway Advisor efficiency

Date: 2026-10-14

## Decision

Use one short guidance contract on gateway preconsultation and low reasoning effort for the currently selected production and staging Advisors. Select `adaptive` / `frequent` / `local` for both gateways so classified turns still receive one bounded preconsultation, without exposing a second native Advisor tool call. Keep the full Advisor copy of the conversation and the existing 32,000-token limit. Do not add a summarization call, truncate history, or impose an output cap unsupported by the subscription transports.

The requirement is to lower consultation cost while retaining actionable advice and response speed. Before this change, the preconsultation path sent the whole Advisor context and had no specific answer length instruction. The Codex subscription endpoint rejects caller output limits, and the Antigravity CLI bridge does not expose one. A lower context threshold would invoke the existing paid overflow compactor more often, adding a provider call and latency. A deterministic history cut could omit task constraints or break tool context. The new contract adds one developer message to the preconsultation copy, leaving target messages untouched. The maintenance burden is one shared string and its targeted tests; there is no new service, setting, store, adapter, retry, or fallback.

## Controlled samples

The three staging Messages calls used the same user prompt and saved OpenRouter target with Gemini 3.8 Flash Advisor. The prompt asked for the largest implementation risk and a concrete check. Staging policy retained `hybrid` / `frequent` / `local`; only the brief and then the saved Advisor effort changed. Each call logged one completed preconsultation, returned HTTP 200 and `end_turn`, and gave a concrete risk and check. The target did not select a second Advisor call in these samples.

| Staging sample | Advisor effort | Advisor input / output provider tokens | Target output tokens | End-to-end seconds |
| --- | --- | ---: | ---: | ---: |
| Before brief | high | 5,427 / 3,709 | 505 | 41.6 |
| Brief added | high | 3,999 / 2,050 | 71 | 66.5 |
| Brief plus low effort | low | 2,089 / 129 | 87 | 70.4 |

The high-effort brief sample used 45% fewer Advisor output tokens than the earlier call, and the low-effort sample used 94% fewer than the high-effort brief sample. The changed Advisor input counts are observations, not evidence of input compression: the same task context was sent, with a small added instruction. Provider metering, hidden prompt overhead, and caching can vary. End-to-end latency did not improve in this three-request sample; the OpenRouter target and upstream variation are not isolated by this measurement. The target answers identified different plausible risks, so this is a completion and usefulness check, not a blinded quality evaluation.

A separate direct Gemini low-effort consultation using the brief returned 272 output tokens in 6.9 seconds, but one response included extra finish-control prose. The gateway's low-effort end-to-end sample still completed normally. Direct Codex Luna calls with the same task and brief used identical 191-token provider inputs: low effort returned 44 output tokens in 1.6 seconds, medium returned 268 in 3.6 seconds. Both identified original-context corruption as a risk and proposed checking message preservation. These two calls support choosing low for this task, but do not establish general quality equivalence.

Production retained its MiniMax target, Codex Luna Advisor, and other routing fields. Saved Advisor effort changed from medium to low at policy v103. After restart, a real production Messages request returned HTTP 200, `end_turn`, one Luna Advisor receipt of 191 input / 177 output provider tokens, and a concrete risk/check in 6.3 seconds end to end. Staging retained OpenRouter/Gemini and changed Advisor effort from high to low at policy v654. After the native-path finding below, production and staging trigger modes were changed from `hybrid` to `adaptive`, retaining `frequent` / `local`, at policy v104 and v657 respectively. Both gateways were restarted on final source and returned readiness 200. A final production adaptive request returned HTTP 200, `end_turn`, one Luna Advisor receipt of 191 input / 316 output tokens, and a concrete risk/check in 10.0 seconds. This is a second sample, not an output ceiling.

## Native Advisor tool finding

An additional staging request temporarily selected `model` mode and asked the OpenRouter target to use the Advisor tool once. The target and Advisor completed, but the Advisor receipt recorded 12,943 input and 10,175 output provider tokens. The sidecar selected Gemini 3.8 Flash High despite saved low effort. LiteLLM's installed interceptor invokes `advisor_router.aanthropic_messages` directly, bypassing the proxy pre-call hook that owns the brief and overflow compaction. The private Advisor model still has its separate 32,000-token router cap configured. The interceptor forwards the target `max_tokens`, but the Gemini CLI bridge does not enforce that output limit. The native path therefore cannot be described as receiving the same prompt, overflow handling, or saved effort as preconsultation. Staging was restored from temporary `model` mode before selecting adaptive for the active policy.

This is a confirmed cost and policy-consistency defect in the optional native path. It was not addressed by adding a second orchestration implementation or silent transcript reduction. `hybrid` remains available for users who value a target-requested follow-up consultation and accept this current cost behavior. The active adaptive setting retains classified preconsultation without invoking that path. A future native-path repair should use a LiteLLM-supported interception point or a narrow upstream fix that carries the selected effort and brief into the router subcall; it must preserve native tool semantics and the 32,000-token Advisor cap.

## Verification and limits

`python -m pytest tests/test_advisor_plugin.py tests/test_dynamic_routing.py tests/test_codex_advisor.py tests/test_litellm_config.py -q` passed 194 tests. The focused checks cover preconsultation receiving the brief, the target conversation remaining unchanged, routing policy, and transport contracts. The deployed LiteLLM interceptor was inspected to confirm that it marks the native Advisor request with `advisor_sub_call` metadata and passes the target's `max_tokens` to that subcall; the live check showed that the router subcall bypasses the proxy hook.

The advice word target is a prompt instruction, not a guaranteed cap. Reasoning effort remains user selectable. The 32,000-token Advisor limit and overflow compaction still handle large preconsultation contexts; proactive lossy compression was avoided because its effect on advice quality is unmeasured. The sampled savings apply to preconsultation output and shorter guidance injected into the target. They do not demonstrate a lower Advisor input bill, a reliable end-to-end speed gain, or equivalent advice quality across coding, image, tool, and long-context tasks.
