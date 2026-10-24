# Native header discovery repair review

Verdict: READY for the frozen leaf-alias source/test repair. No blocking finding. Deployment/live composite acceptance is still pending.

The previous 27-case parity probe called hook methods directly. It missed the framework discovery boundary and cannot establish proxy header exposure. Its limited evidence is preserved, and the earlier broader header-exposure acceptance is explicitly superseded.

## Confirmed gap and minimal repair

Inspected the installed LiteLLM 1.106.2 primary source. `ProxyLogging.callback_capabilities()` checks `type(callback).__dict__` for `async_post_call_response_headers_hook`; it does not follow inherited header overrides. `post_call_response_headers_hook()` returns early when that cached capability is false, and common request processing also gates dispatch. A callable inherited method alone is therefore insufficient when this expert callback is the only header callback.

The parent adds exactly one leaf-class method alias to the existing Sift implementation, with its version-specific reason and retirement condition. No duplicate formatting, wrapper, registry, transport or framework patch is added. The subclass simplification retains single base initialization, budgets, private-copy/forgery protections, CPU/cancellation logic and pre-policy. The leaf alias exposes the unchanged method to native capability discovery.

The maintained regression now uses actual `ProxyLogging`, its real capability cache, `DualCache`, `UserAPIKeyAuth` and native header dispatch. It checks exact headers and ordinary/absent-audit empty results. This exercises the failure mechanism that the earlier direct-method regression missed.

## Independent verification

- **18 focused tests passed**, three existing warnings, 7.66 seconds. Receipt: `nonauthor-discovery-tests.txt`.
- Ran actual LiteLLM ASGI HTTP routes through the maintained fixture with only the upstream stubbed. All **six positive cases** passed: buffered and streamed Chat, Responses and Messages. Each request exceeded 32,000 estimated tokens, compacted privately below 25,600 and returned all three native `x-sift-*` headers. Input fixture bytes were unchanged, and exactly one stubbed upstream invocation occurred per request. No deployed port or actual provider was contacted.
- Added a reviewer-owned negative control: a further subclass with a concrete pre-hook but only inherited header method. It still produced a real compacted audit, while native capability discovery was false and the actual HTTP response had no Sift headers. This reproduces the observed discovery failure independently and demonstrates sensitivity of the proxy-path check.
- Source hash stayed stable across checks; scoped whitespace inspection passed. Safe result: `nonauthor-proxy-header-verification.json`; local probe: `nonauthor_proxy_header_probe.py`; identities: `nonauthor-discovery-verification.json`.

The fixture uses a synthetic Codex-compatible upstream and synthetic usage. It proves native callback discovery, translation and public-header plumbing, not real-provider tokenization, provider usage/billing or new live answer acceptance. The previous model-selected probe remains on hold until parent-reviewed image/runtime prerequisites are satisfied.

The parent reported a newer Gemini exact-marker success accompanied by missing headers; that composite probe remains failed. This review does not reinterpret that live result or independently accept the separate Gemini source change. Another peer owns its receipt reconciliation.

No source edits, deployed requests, provider inference, service/lifecycle operations, policy/schema changes or publication were performed by this reviewer. Only local provider-free checks and owned review artifacts were used. Parent retains integration, image rollout and later live evidence reconciliation.
