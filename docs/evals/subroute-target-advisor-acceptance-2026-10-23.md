# Subroute target and Advisor route acceptance, 2026-10-23

## Scope and environment

Live checks used staging on `127.0.0.1:4005` and the authenticated private experts service on `127.0.0.1:4040`. The saved staging policy was `mimo-v2.6-pro`, force mode, with `gemini-subscription` selected as the Advisor. The gateway and experts services used image `sha256:a99773d74df38fe676e17054ed9228f53a844449395ba420be7a112b59cd62cd`. Production on port 4000 was not exercised. No service was restarted and no saved routing policy was changed.

The live experts catalog listed nine private aliases. Each probe used a short unique marker, a 128-token request limit, and the correct bearer credential. The experts configuration has zero retries and no fallback routes.

## Results

### Selected target route

Staging resolved `default` to the saved `mimo-v2.6-pro` target. Correlated `LiteLLM_SpendLogs` rows show one successful upstream `openai/mimo-v2.6-pro` attempt for each of these five calls:

| Protocol | Result | Provider usage |
| --- | --- | --- |
| Chat Completions, buffered | Pass: HTTP 200, `stop`, exact marker | 18 input / 42 output |
| Anthropic Messages, 64-token limit | Incomplete answer: HTTP 200, `max_tokens`, no text marker | 139 input / 64 output |
| Anthropic Messages, 256-token limit | Pass: HTTP 200, `end_turn`, exact marker | 143 input / 78 output |
| Responses, buffered | Pass: `response.completed`, exact marker | 19 input / 35 output |
| Responses, streamed | Pass: `response.completed`, exact marker | 20 input / 32 output |

Normal target responses had no Sift-compaction header. The 64-token Messages case is retained as a failed answer check, not counted as a pass; raising the caller limit to 256 produced a complete answer.

### Private experts service on port 4040

| Alias | Result |
| --- | --- |
| `gemini-3.8-flash-advisor` | Pass, exact marker, `end_turn`, usage 1,020 / 171; `x-sift-compacted: false` |
| `gemini-4-argon-advisor` | Pass, exact marker, `end_turn`, usage 1,055 / 47; `x-sift-compacted: false` |
| `codex-luna-advisor` | Pass, exact marker, `end_turn`, usage 18 / 9; `x-sift-compacted: false` |
| `codex-sol-advisor` | Pass, exact marker, `end_turn`, usage 18 / 9; `x-sift-compacted: false` |
| `codex-astra-advisor` | Pass, exact marker, `end_turn`, usage 18 / 9; `x-sift-compacted: false` |
| `codex-gpt-6.1-sol-advisor` | Pass, exact marker, `end_turn`, usage 18 / 9; `x-sift-compacted: false` |
| `gemini-3.7-flash-advisor` | Fail, HTTP 502 before provider usage: installed Antigravity CLI does not recognize `Gemini 3.7 Flash (Low)` |
| `gemini-3.6-flash-advisor` | Fail, HTTP 502 before provider usage: installed Antigravity CLI does not recognize `Gemini 3.6 Flash (Low)` |
| `gemini-3.1-pro-advisor` | Fail, HTTP 502 before provider usage: installed Antigravity CLI does not recognize `Gemini 3.1 Pro (Low)` |

Each failed Gemini response listed the CLI's available models and stated that no fallback was attempted. An unauthenticated experts catalog request returned 401; an unknown private alias returned 400 without provider usage.

### Gateway-to-expert path

Direct public requests to two internal `experts/...` aliases returned 404 with zero provider tokens. These aliases are marked non-selectable in the route configuration, so this is not counted as a public target-route failure.

One forced Advisor-tool probe through staging returned a completed target answer. The gateway spend ledger contains one target attempt for that request, but it does not provide a correlated expert attempt receipt. The experts service disables spend logs. Therefore tool selection, expert invocation, and result injection remain **unverified** from this run; the successful target answer alone is not proof of an Advisor round trip.

## Offline checks and limits

The host Python 3.14 environment could not collect the focused tests because its installed LiteLLM package lacks the pinned `anthropic.pass_through` module. The repository `.venv` is Python 3.13.11 but has no LiteLLM. In the existing Python 3.13.11 / LiteLLM 1.106.2 evaluation environment, the first two `tests/test_expert_sift.py` checks passed; the exact 32,000-token input-limit test made no progress for 110 seconds, so that run was stopped. This is not a focused-suite pass. Large-input Sift compaction, irreducible-input behavior, cancellation, and correlated live Advisor invocation were not accepted here.

At the time of this initial report, no independent review had run. A read-only local Codex review was completed during the 2026-10-24 follow-up below.

No source files, saved policy, or service lifecycle were changed by the initial 2026-10-23 acceptance run.

## Follow-up investigation (2026-10-24)

### 32,000-token input boundary

The exact Windows pytest case was rerun with bounded faulthandler capture, first under the default Proactor event loop and then under `WindowsSelectorEventLoopPolicy`. Both traces stopped in `socket.accept` / `_fallback_socketpair` while `asyncio.run` was creating the event loop, before `async_pre_call_hook` or the test body ran. This is an environment stall, neither a hook failure nor a pytest pass. Raw output is in `tmp/acceptance-32k-3a4fda73841647a28324e0c72b3a9bc8/` and `tmp/acceptance-32k-selector-3ad5880066284f5eb159bddda0e05b29/`.

On the pinned staging image `sha256:a99773d74df3`, an equivalent check exercised the real `ExpertSiftHook` in the deployed Linux runtime with the test fixture's counting router. The exact 32,000-token input passed unchanged with the independent 1,024-token Codex output bound; the 33,024-token input compacted and preserved the original request. These were hook checks, not pytest runs, and they made no provider calls. The independent reviewer treated these container results as supplied evidence, not as results it independently reproduced.

### Advisor invocation evidence

The forced Advisor probe proves that the target completed an answer. Its correlated staging spend row records one successful target attempt, but its stored request and response objects are empty and it contains no nested expert route trace. The experts service has spend logging disabled, and the sanitized log query returned no per-request access record. This leaves two possibilities unresolved: the expert was not invoked, or it was invoked without retained accounting. The probe therefore does not establish tool selection, expert invocation, or result injection; missing accounting is not proof of non-invocation.

### Deprecated Gemini routes

After the live alias checks above, the user directed removal of the deprecated Gemini 3.7 Flash, 3.6 Flash, and 3.1 Pro entries. Their source-card labels, selectable target aliases, private expert aliases, and Advisor allowlist mappings have been removed from configuration and code. Gemini 3.8 Flash and Gemini 4 Argon remain listed. The saved routing policy was not changed, and shared services were not restarted or reloaded; live catalog/UI behavior after these source edits remains unverified.

Ten focused offline tests passed, covering the remaining target aliases, expert route allowlist, expert service catalog, deprecated route removal, and policy persistence. The UI source JSON was parsed and its Gemini card summary was checked directly. A broader Windows pytest attempt stalled in the first async test after a blocked remote LiteLLM cost-map fetch; it was stopped without a suite result. The known 32,000-token test was excluded from the focused run. The separate UI `TestClient` check also did not complete in this host environment.

### Independent review and route smoke

A read-only Codex CLI reviewer, using the configured `subroute-subagents` profile, reviewed the report, GOAL, test, and raw Windows diagnostics. It judged the report evidence-bounded but acceptance partial, agreed that target completion does not prove an Advisor round trip, and cautioned that the three alias failures apply to the tested aliases in the pinned environment rather than proving the underlying Gemini models are universally unsupported. It also confirmed the Windows stall occurs before the hook. Its review is saved at `tmp/acceptance-review-20261024/review.txt`.

A separate local profile smoke was launched against the configured default route on port 4000, but its wrapper output was truncated and the expected response could not be recovered. It is inconclusive and receives no route or completion credit. No production or staging services were restarted, and the existing selected target and Advisor policy values were not changed.
