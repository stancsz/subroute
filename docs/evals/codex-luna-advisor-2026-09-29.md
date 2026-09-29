# GPT-6 Luna as an Advisor

**Status:** deployed to production and staging on 2026-09-29\
**Request:** make Luna selectable and usable as an advisor.

## Design

The Codex advisor adapter already recognized `gpt-6-luna`, but the gateway's advisor catalog and dispatch map omitted it. The control desk derives its advisor choices from `model_info.advisor_selectable`, so the missing catalog entry also made Luna unavailable through the UI and `/api/advisor-model`.

Added the `codex-luna-advisor` alias mapped to `codex-advisor/gpt-6-luna`, added the same name to the advisor dispatch and supported-tool-history sets, and reused the existing Codex subscription request collector. The alias exposes the reasoning efforts already declared for the `codex-luna` subscription target. This adds one configured route and its compatibility mapping, with no new service, dependency, transport, retry, or fallback. It does not use the removed OpenAI API-key deployment.

The feature applies to the existing Anthropic Messages Advisor flow. Selecting Luna causes a pre-consultation on supported target paths, injects the bounded guidance as a developer message, then dispatches the base request. The default policy was not changed: production still has no advisor selected, so a user must choose GPT-6 Luna in the control desk to enable it.

## Verification

- Configuration, routing selection, Advisor injection, and tool-history tests pass. The full locked suite passed **257 tests**, skipped 10 explicit opt-in live tests, and reported 28 dependency warnings.
- The real staging alias test passed in **31.46 seconds**. It exercised Gemini Subscription and Codex Subscription aliases plus Terra, Astra, and Luna advisors on a Codex Luna base route.
- Staging gateway log receipt: `consultation_id=713d72f900fc413dabb1f50d47745962`, `model=codex-luna-advisor`, `input_tokens=29`, `output_tokens=18`, `base_model=codex-luna`. The base Messages request returned HTTP 200 with `end_turn` and provider usage.
- Both production (`4000`) and staging (`4005`) were recreated and returned healthy readiness with PostgreSQL connected. Both `/api/routing-options` responses include `codex-luna-advisor`; both inventories contain 18 model IDs and exclude `openai`, `gemini-api`, and `openai/gpt-5.2-codex`.
- Production policy was not selected or changed for a paid live generation. Staging policy fields were restored after the live test.

Ruff passed for the changed Python implementation and test files. `docker compose config --quiet` and `git diff --check` passed.
