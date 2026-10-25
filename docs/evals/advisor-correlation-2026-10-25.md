# Advisor request and attempt correlation

## Scope

This is a post-closure improvement to the scoped simplicity delivery. It does not change normal routing, provider request bodies, Sift selection, or the completed ten-criterion acceptance record. It carries the gateway request ID and one advisor-attempt ID across the private Experts HTTP hop so the existing accounting and Sift records can be joined reliably.

## Failure mechanism and ownership

Before this change, accounting already created request and per-attempt records, but the IDs did not cross the private 4040 adapter boundary. Concurrent consultations could therefore lack a reliable join key between gateway accounting and the Sift audit. The gateway now injects the pair only for the `advisor` role at the exact configured `http://experts:4040` base. LiteLLM carries them as HTTP headers, outside the JSON body.

At the Experts callback, the pair is accepted only when LiteLLM supplies a `UserAPIKeyAuth` with `PROXY_ADMIN` and `LITELLM_PROXY_MASTER_KEY_ALIAS`. Presented IDs are always stripped from the recognized request and provider-header carriers; an untrusted marker or UUID-formatted caller value cannot claim a Sift log association. The pair is validated as UUIDs, copied into request-local audit state, and removed on success, failure, cancellation, and multimodal bypass. This changes audit correlation only; UUIDs are not authentication credentials.

Relevant source: `src/subroute/usage_accounting.py` (root request and advisor attempt), `src/subroute/plugins/expert_sift.py` (auth gate, header consumption, marker cleanup), and `src/sift/litellm_proxy.py` (correlated audit log fields).

## Verification

- Focused correlation checks in `tests/test_expert_sift.py`, `tests/test_expert_transport.py`, and `tests/test_usage_accounting_protocols.py`: **21 passed, 26 deselected** after adding a real LiteLLM master-key authentication fixture.
- Full non-live suite after that addition: **1,000 passed, 2 skipped, 35 deselected, 193 warnings in 105.05 seconds**. It ran with all project extras, the locked Python 3.13.11/LiteLLM 1.106.2 environment, local model cost metadata, and the two tokenizer cache files shipped in the candidate image. No provider calls were made; live tests were deselected.
- Earlier independent CLI review over the six changed source/test files found no confirmed material defect in the local slice. Its remaining runtime gaps have since narrowed through the local HTTP auth test; the added review still leaves proxy callback dispatch, provider-boundary behavior, and correlated Sift logging unverified.
- Candidate image built as `subroute-advisor-correlation:f7b82d8-local`, image ID `sha256:e5a155b6237b9f17d14378ad92e1791f1535adbafa0531744dca5ee2d7c244ec`. With the Compose `PYTHONPATH=/app/src`, LiteLLM 1.106.2 and the Expert auth gate import successfully. The image's `expert_sift.py` SHA-256 (`7f92983706f0c2e07981402a4fa0adaa52551cafc2a1ad603358cd866593c163`) matches the worktree source.
- Recomputed the same 21-path source ledger after the follow-up: **1,543 fewer maintained source lines** than its recorded baseline, down from 1,641 fewer before this change. The correlation addition accounts for 98 net counted production-source lines. The [updated ledger](receipts/advisor-correlation-source-2026-10-25.json) uses the same nonblank/noncomment line method and LF-normalized UTF-8 SHA-256; tests and docs remain excluded from source counts. This follow-up improves audit reliability and does not claim code reduction by itself.
- At the prior implementation snapshot, the six implementation/test files had a raw diff of 440 additions and 5 deletions; tests accounted for 326 additions. The current working-tree addition is one 69-line test. These diff statistics are separate from the maintained-source ledger and source-code reduction claim.

### Master-key HTTP auth regression check

`test_litellm_master_key_http_auth_enables_expert_correlation` sends a local FastAPI `TestClient` request through LiteLLM's actual `user_api_key_auth` dependency, verifies the `PROXY_ADMIN` identity and `LITELLM_PROXY_MASTER_KEY_ALIAS`, then passes that auth object into `ExpertSiftHook` and checks that validated correlation reaches the Sift hook while the internal marker and reserved headers are removed. An independent read-only CLI review confirmed those claims and found no defect in this test's stated scope. The review also identified its boundary: the test stubs the parent Sift hook and invokes `ExpertSiftHook` directly from a test-owned route, so it does not run LiteLLM's registered proxy callback dispatch, a provider transport, or the correlated production Sift log. It narrows the auth-to-hook gap without closing staging acceptance.

## Runtime limits

No service was started from the candidate and no staging request, route change, or provider call was made. At verification time, staging gateway and Experts were still on `sha256:a99773d74df38fe676e17054ed9228f53a844449395ba420be7a112b59cd62cd`, started on 2026-10-24, both with restart count 0. This proves the running environment was left untouched, not that the new correlation works there.

Existing transport tests construct `UserAPIKeyAuth` directly and capture the outgoing LiteLLM HTTP request with a mocked handler. The added local HTTP test now verifies real LiteLLM master-key authentication and the auth-to-hook handoff, but uses a test-owned route and a stub Sift hook. Actual registered proxy callback dispatch, provider-facing header behavior, and the final correlated Sift log have not been verified end to end. A staging window is still needed for that check. Exact-base matching also assumes the deployed route remains `http://experts:4040`.

The implementation is commit `b539b74`; the last published follow-up snapshot was `3475348fa1df81ad3f88f2d0986ffc3edd4e7d17` on `origin/codex/advisor-correlation-integration`. This turn's test and evidence edits are not yet published. Before those edits, `git ls-remote` matched the remote branch SHA to local HEAD; `origin/main` remained at `f7b82d8a03c8c11207c76408379e23e1c3c90918`. Staging acceptance is still separate from this source.
