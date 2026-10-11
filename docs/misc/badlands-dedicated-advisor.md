# Badlands dedicated advisor

The Badlands public API now sends ordinary inference to OpenRouter MiniMax M3,
with native MiMo coding-plan fallback. Subroute supplies the dedicated
`badlands/advisor` and image-generation capabilities. The API gateway owns this
policy; see `D:/github/api-gateway/docs/operations/openrouter-primary.md`.

The protected service calls `codex-luna-advisor` with server-owned
`advisor_sub_call: true` and low reasoning effort. Responses uses LiteLLM's
`litellm_metadata` bucket; Chat/Anthropic use `metadata`. Public callers cannot
provide the internal override through the Badlands Worker.

Internal advisor requests bypass saved primary routing and semantic image/audio
generation detection. This preserves advice about an image as advice rather than
accidentally launching an image job. Subroute image-generation requests remain
on the existing `codex-luna` image handler. Direct loopback callers retain the
saved local policy.

2026-10-11 verification: 153 targeted policy/reasoning/image-intent tests passed;
fresh-process checks against deployed LiteLLM 1.103.0 passed for all three
protocols. Both gateway containers were restarted and readiness is HTTP 200.
The existing unrelated configuration edits were preserved and are not included
in this change.
