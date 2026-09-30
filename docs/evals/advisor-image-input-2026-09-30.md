# Dedicated Advisor image input

Date: 2026-09-30. Base `9125725` plus the scoped adapter/config/test change. Owner requests the Northstar caller to proactively transmit images when invoking Advisor, while allowing Advisor-directed source reading. This extends the dedicated Subscription Advisor input conversion; no new service, provider transport, image-generation tool, native tool execution, retry or fallback is added.

## Mechanism and development checks

The installed locked LiteLLM has image conversions, but this `CustomLLM` bypasses them. Its `build_responses_input` was the rejecting boundary. It now validates user `image_url` objects or Responses-style `input_image` blocks and preserves actual data/HTTP(S) image URL, detail, text order and existing tool-history translation. Invalid image envelopes, unsupported roles, malformed base64 and unsupported URL/detail fields fail visibly before upstream dispatch. It validates input syntax, not decoded pixels. Retire this narrow conversion when a native path preserves the established credentials, instruction roles and completion/usage contract.

Focused locked suite: `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp tmp/advisor-images/backend/pytest tests/test_codex_advisor.py`: **69 passed**, one existing dependency warning. Includes actual LiteLLM dispatch against a mocked upstream, not only calling the content builder. Whitespace checks pass. Local dependency version 1.101.0 differs from deployed LiteLLM 1.103.0; offline checks alone do not establish deployment behavior.

## Live scope and limitations

Restarted only existing `experts` after inspecting its Compose identity and source/config mounts. Dedicated `4040` readiness is healthy. The Northstar caller sends local screenshot pixels as structured base64 image blocks. A real Sol call correctly read image-only title/label and clipping; a separate four-image call read original system-card chart values. A completed image-plus-Advisor-directed-Pi run returned source-valid evidence and a final image-backed answer. Request IDs: screenshot `chatcmpl-codex-advisor-2823b78d213e`, final image/source answer `chatcmpl-codex-advisor-23250b3cedaf`, charts `chatcmpl-codex-advisor-d768ddabe7b3`.

Northstar maintains the full caller/reader receipts, three failed source-reader runs, exact token accounting, primary-source research and the separate nonauthor READY review in its `docs/evals/advisor-images.md`. The failed runs were caller/reader evidence failures, not successful consultations, and remain part of its record. One completed combined run does not prove generally reliable source reading or improved aesthetic quality. Pi stays an isolated text-only source reader; this backend does not supply browser/screenshot tools.

Saved production/staging routing policy is unchanged; their containers were not restarted. Installed skill copies were not refreshed. This validates the dedicated experts route, not automatic Advisor use on all public gateway protocols. Standing owner Docker authorization covers scoped direct-main publication and affected-service update. Git refs and the completion handoff identify the published revision and final health result.

## GPT-6.1 Sol-only follow-up

The owner's subsequent requirement removes Astra and older Sol from this dedicated Advisor endpoint. The sole catalog entry `codex-gpt-6.1-sol-advisor` maps to `codex-advisor/gpt-6.1-sol`; the existing Subscription collector now recognizes that exact model. Shared worker-gateway aliases remain outside this dedicated catalog change. Versioned routing fails on an older service instead of silently selecting the old Sol. Zero retries/fallbacks are unchanged.

Focused suite now **72 passed**, one existing dependency warning. Both direct CustomLLM and real LiteLLM dispatch against mocked upstream prove `gpt-6.1-sol` reaches the existing transport with original pixels and reasoning. A separate config regression checks sole alias/model and retry/fallback boundaries. Northstar reports 35 Python and two Node passes, including pre-call Astra CLI rejection. The same nonauthor reviewer independently checks this follow-up; primary owns acceptance/publication.

Restarted only `experts`. The first readiness poll during startup ended prematurely; subsequent readiness is healthy and the models API lists only the versioned alias. Requests using old `codex-sol-advisor` and `codex-astra-advisor` return HTTP 400 invalid model, without substitute advice. Experts has no database configured; its readiness is not database verification.

Fresh 6.1 pixel-backed request `chatcmpl-codex-advisor-dc05ce70d92b` reads the screenshot's title/label and clipping. Image-plus-reader requests `chatcmpl-codex-advisor-11bd52afb683` / `chatcmpl-codex-advisor-7d15032d7399` use the same versioned expert before and after one Advisor-generated source question; final feedback combines validated caller line 71 and image observations. Northstar preserves these three new receipt files in `docs/evals/assets/advisor-images/sol61-*.json`. Earlier screenshots/charts/source-reader receipts above are old-Sol history, not fresh 6.1 proof. New calls report 3,199 expert + 5,190 Pi = 8,389 tokens; cumulative whole increment 65,506 tokens including failures, monetary cost unknown. This remains one narrow source success with unknown actual Pi model, not aesthetic approval, general reader reliability or savings.

Only the dedicated endpoint/model pin, tests and its current documentation are in this follow-up. Production/staging settings, containers and installed skill copies remain unchanged. Publication uses standing Docker-service authorization; commit/remote refs and final health must be checked before handoff.

Primary accepts the scoped pin after inspecting the nonauthor READY review recorded in Northstar's `docs/reports/central-package/advisor-image-review.md`. Independent checks covered 6 caller tests, 5 backend tests, exact 6.1 request/pixel/source evidence, promoted receipt equality and complete token sums. Six related files form the direct-main follow-up; Git and final handoff identify the actual published revision and post-publication health. No broader gateway model removal or skill installation is claimed.

Learning: pin and test the upstream model as well as the public alias, reject removed selections visibly, and obtain fresh pixel/source evidence when the expert version changes.

## Learning

Generic framework image support does not prove a custom provider preserves images. Inspect the actual rejecting boundary, retain original pixels/roles, exercise real LiteLLM dispatch, then verify a pixel-dependent provider answer. Keep source-reader failures distinct from image transport and from final acceptance. Stronger model advice and transport correctness do not waive owner approval or independent QA.

## Current model selection after owner clarification

The subsequent owner clarification supersedes the GPT-6.1-only restriction above. Both gateway Advisor catalogs now expose GPT-6 Luna, GPT-6 Sol, GPT-6 Astra and GPT-6.1 Sol; GPT-5.6 Terra Advisor is removed. Production uses OpenRouter MiniMax M3 with Luna/high. Callers retain explicit model and effort selection. The version-restricted results above remain historical evidence.

Verification for the clarified catalog: **194 related offline regressions passed**. Production, staging and experts were recreated with Compose and all three readiness checks passed. Production retained MiniMax/Luna/high at policy v74; staging retained its prior policy v476. Four real port-4040 requests returned `ADVISOR_ROUTE_OK`, terminal `stop`, and provider usage, one for each configured OpenAI Advisor. A production Messages request under the saved policy returned 323 and `end_turn`; its final response does not separately expose the native advisor invocation, so this is request-completion evidence rather than a correlated advisor consultation receipt.

A separate mocked dispatch check inside the deployed LiteLLM 1.103.0 container confirms the exact four upstream model IDs, Luna's high default, and an explicit low-effort override. This check spends no provider calls and is not live-provider evidence. The catalog update adds no service, dependency, transport or store. It removes the accidental single-model catalog restriction and the obsolete 5.6 Advisor route while preserving image conversion, explicit selection and zero retries/fallbacks. See [raw runtime receipt](../reports/advisor-routes-2026-09-30.json).
