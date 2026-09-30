# Dedicated Advisor image input

Date: 2026-09-30. Base `9125725` plus the scoped adapter/config/test change. Owner requests the Northstar caller to proactively transmit images when invoking Advisor, while allowing Advisor-directed source reading. This extends the dedicated Subscription Advisor input conversion; no new service, provider transport, image-generation tool, native tool execution, retry or fallback is added.

## Mechanism and development checks

The installed locked LiteLLM has image conversions, but this `CustomLLM` bypasses them. Its `build_responses_input` was the rejecting boundary. It now validates user `image_url` objects or Responses-style `input_image` blocks and preserves actual data/HTTP(S) image URL, detail, text order and existing tool-history translation. Invalid image envelopes, unsupported roles, malformed base64 and unsupported URL/detail fields fail visibly before upstream dispatch. It validates input syntax, not decoded pixels. Retire this narrow conversion when a native path preserves the established credentials, instruction roles and completion/usage contract.

Focused locked suite: `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --basetemp tmp/advisor-images/backend/pytest tests/test_codex_advisor.py`: **69 passed**, one existing dependency warning. Includes actual LiteLLM dispatch against a mocked upstream, not only calling the content builder. Whitespace checks pass. Local dependency version 1.101.0 differs from deployed LiteLLM 1.103.0; offline checks alone do not establish deployment behavior.

## Live scope and limitations

Restarted only existing `experts` after inspecting its Compose identity and source/config mounts. Dedicated `4040` readiness is healthy. The Northstar caller sends local screenshot pixels as structured base64 image blocks. A real Sol call correctly read image-only title/label and clipping; a separate four-image call read original system-card chart values. A completed image-plus-Advisor-directed-Pi run returned source-valid evidence and a final image-backed answer. Request IDs: screenshot `chatcmpl-codex-advisor-2823b78d213e`, final image/source answer `chatcmpl-codex-advisor-23250b3cedaf`, charts `chatcmpl-codex-advisor-d768ddabe7b3`.

Northstar maintains the full caller/reader receipts, three failed source-reader runs, exact token accounting, primary-source research and the separate nonauthor READY review in its `docs/evals/advisor-images.md`. The failed runs were caller/reader evidence failures, not successful consultations, and remain part of its record. One completed combined run does not prove generally reliable source reading or improved aesthetic quality. Pi stays an isolated text-only source reader; this backend does not supply browser/screenshot tools.

Saved production/staging routing policy is unchanged; their containers were not restarted. Installed skill copies were not refreshed. This validates the dedicated experts route, not automatic Advisor use on all public gateway protocols. Standing owner Docker authorization covers scoped direct-main publication and affected-service update. Git refs and the completion handoff identify the published revision and final health result.

## Learning

Generic framework image support does not prove a custom provider preserves images. Inspect the actual rejecting boundary, retain original pixels/roles, exercise real LiteLLM dispatch, then verify a pixel-dependent provider answer. Keep source-reader failures distinct from image transport and from final acceptance. Stronger model advice and transport correctness do not waive owner approval or independent QA.
