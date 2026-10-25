# Advisor request and attempt correlation

## Scope

This is a post-closure improvement to the scoped simplicity delivery. It does not change normal routing, provider request bodies, Sift selection, or the completed ten-criterion acceptance record. It carries the gateway request ID and one advisor-attempt ID across the private Experts HTTP hop so the existing accounting and Sift records can be joined reliably.

## Failure mechanism and ownership

Before this change, accounting already created request and per-attempt records, but the IDs did not cross the private 4040 adapter boundary. Concurrent consultations could therefore lack a reliable join key between gateway accounting and the Sift audit. The gateway now injects the pair only for the `advisor` role at the exact configured `http://experts:4040` base. LiteLLM carries them as HTTP headers, outside the JSON body.

At the Experts callback, the pair is accepted only when LiteLLM supplies a `UserAPIKeyAuth` with `PROXY_ADMIN` and `LITELLM_PROXY_MASTER_KEY_ALIAS`. Presented IDs are always stripped from the recognized request and provider-header carriers; an untrusted marker or UUID-formatted caller value cannot claim a Sift log association. The pair is validated as UUIDs, copied into request-local audit state, and removed on success, failure, cancellation, and multimodal bypass. This changes audit correlation only; UUIDs are not authentication credentials.

Relevant source: `src/subroute/usage_accounting.py` (root request and advisor attempt), `src/subroute/plugins/expert_sift.py` (auth gate, header consumption, marker cleanup), and `src/sift/litellm_proxy.py` (correlated audit log fields).

## Verification

- Focused correlation checks in `tests/test_expert_sift.py`, `tests/test_expert_transport.py`, and `tests/test_usage_accounting_protocols.py`: **8 passed, 39 deselected**. The auth case runs LiteLLM's actual `ProxyLogging.pre_call_hook` callback dispatch and real local Sift path.
- Full non-live suite after that change: **1,000 passed, 2 skipped, 35 deselected, 193 warnings in 105.25 seconds**. Command: `uv run --all-extras --locked pytest -q -m "not live"`, with process-scoped `TIKTOKEN_CACHE_DIR` and `CUSTOM_TIKTOKEN_CACHE_DIR` pointing to `.test-temp/tiktoken-cache`. No provider calls were made; live tests were deselected. An initial run without the cache variables hit one Responses fixture that tried to download tokenizer data; the suite's socket guard correctly blocked it. Rerunning with the repository's two cached tokenizer files passed.
- Independent read-only CLI review confirms the test parses the posted JSON body, exercises real LiteLLM master-key auth, calls the installed proxy callback loop, and asserts the correlated Sift log. It still distinguishes this from running LiteLLM's own `/v1/messages` route and an actual provider transport.
- Candidate image built as `subroute-advisor-correlation:f7b82d8-local`, image ID `sha256:e5a155b6237b9f17d14378ad92e1791f1535adbafa0531744dca5ee2d7c244ec`. With the Compose `PYTHONPATH=/app/src`, LiteLLM 1.106.2 and the Expert auth gate import successfully. The image's `expert_sift.py` SHA-256 (`7f92983706f0c2e07981402a4fa0adaa52551cafc2a1ad603358cd866593c163`) matches the worktree source.
- Recomputed the same 21-path source ledger after the follow-up: **1,543 fewer maintained source lines** than its recorded baseline, down from 1,641 fewer before this change. The correlation addition accounts for 98 net counted production-source lines. The [updated ledger](receipts/advisor-correlation-source-2026-10-25.json) uses the same nonblank/noncomment line method and LF-normalized UTF-8 SHA-256; tests and docs remain excluded from source counts. This follow-up improves audit reliability and does not claim code reduction by itself.
- At the prior implementation snapshot, the six implementation/test files had a raw diff of 440 additions and 5 deletions; tests accounted for 326 additions. The current working-tree addition is one 69-line test. These diff statistics are separate from the maintained-source ledger and source-code reduction claim.

### Master-key HTTP auth regression check

`test_litellm_master_key_http_auth_enables_expert_correlation` sends a local FastAPI `TestClient` request through LiteLLM's actual `user_api_key_auth` dependency, parses the posted request body, then calls the installed `ProxyLogging.pre_call_hook` with `ExpertSiftHook` registered in `litellm.callbacks`. The real hook validates the authenticated identity, compacts the text using a local counting-router fixture, logs the matching request/attempt IDs, and removes its marker and reserved headers. The test requires `compacted=true`, more input than forwarded tokens, the configured target, and exact request/attempt ID matches. Independent read-only review confirmed the posted body is used and found no remaining issue within this test's scope. The route itself is test-owned, and the token counter is synthetic; the test does not invoke LiteLLM's actual `/v1/messages` endpoint or a provider transport. It strengthens local auth-to-callback-to-Sift evidence without closing provider or staging acceptance.

## Runtime limits

No service was started from the candidate and no staging request, route change, or provider call was made. At verification time, staging gateway and Experts were still on `sha256:a99773d74df38fe676e17054ed9228f53a844449395ba420be7a112b59cd62cd`, started on 2026-10-24, both with restart count 0. This proves the running environment was left untouched, not that the new correlation works there.

Existing transport tests construct `UserAPIKeyAuth` directly and capture the outgoing LiteLLM HTTP request with a mocked handler. The added local HTTP test now verifies real LiteLLM master-key authentication, installed callback dispatch, Sift compaction, and the correlated log. It uses a test-owned route and synthetic token counting, so LiteLLM's real `/v1/messages` route, provider-facing header behavior, provider acceptance, and deployed staging behavior remain unverified. A staging window is still needed for that check. Exact-base matching also assumes the deployed route remains `http://experts:4040`.

The implementation is commit `b539b74`; the last published follow-up snapshot was `299964f7b7caf2281480bdafeb2688fe3126bab2` on `origin/codex/advisor-correlation-integration`. This turn's callback-dispatch test and evidence edits are not yet published. Before these edits, `git ls-remote` matched the remote branch SHA to local HEAD; `origin/main` remained at `f7b82d8a03c8c11207c76408379e23e1c3c90918`. Staging acceptance is still separate from this source.
