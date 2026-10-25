# Advisor request and attempt correlation

## Scope

This is a post-closure improvement to the scoped simplicity delivery. It does not change normal routing, provider request bodies, Sift selection, or the completed ten-criterion acceptance record. It carries the gateway request ID and one advisor-attempt ID across the private Experts HTTP hop so the existing accounting and Sift records can be joined reliably.

## Failure mechanism and ownership

Before this change, accounting already created request and per-attempt records, but the IDs did not cross the private 4040 adapter boundary. Concurrent consultations could therefore lack a reliable join key between gateway accounting and the Sift audit. The gateway now injects the pair only for the `advisor` role at the exact configured `http://experts:4040` base. LiteLLM carries them as HTTP headers, outside the JSON body.

At the Experts callback, the pair is accepted only when LiteLLM supplies a `UserAPIKeyAuth` with `PROXY_ADMIN` and `LITELLM_PROXY_MASTER_KEY_ALIAS`. Presented IDs are always stripped from the recognized request and provider-header carriers; an untrusted marker or UUID-formatted caller value cannot claim a Sift log association. The pair is validated as UUIDs, copied into request-local audit state, and removed on success, failure, cancellation, and multimodal bypass. This changes audit correlation only; UUIDs are not authentication credentials.

Relevant source: `src/subroute/usage_accounting.py` (root request and advisor attempt), `src/subroute/plugins/expert_sift.py` (auth gate, header consumption, marker cleanup), and `src/sift/litellm_proxy.py` (correlated audit log fields).

## Verification

- Focused correlation checks in `tests/test_expert_sift.py`, `tests/test_expert_transport.py`, and `tests/test_usage_accounting_protocols.py`: 20 passed, 26 deselected after the commit.
- Full non-live suite on the committed source: **999 passed, 2 skipped, 35 deselected, 193 warnings in 104.65 seconds**. It ran with all project extras, the locked Python 3.13.11/LiteLLM 1.106.2 environment, local model cost metadata, and the two tokenizer cache files shipped in the candidate image. No provider calls were made; live tests were deselected.
- Independent CLI review over the six changed source/test files: no confirmed material defect in the local slice. It explicitly leaves real Experts authentication/callback/header consumption and correlated Sift logging unverified; those are staging acceptance gaps, not evidence of a local source defect.
- Candidate image built as `subroute-advisor-correlation:f7b82d8-local`, image ID `sha256:e5a155b6237b9f17d14378ad92e1791f1535adbafa0531744dca5ee2d7c244ec`. With the Compose `PYTHONPATH=/app/src`, LiteLLM 1.106.2 and the Expert auth gate import successfully. The image's `expert_sift.py` SHA-256 (`7f92983706f0c2e07981402a4fa0adaa52551cafc2a1ad603358cd866593c163`) matches the worktree source.
- Recomputed the same 21-path source ledger after the follow-up: **1,543 fewer maintained source lines** than its recorded baseline, down from 1,641 fewer before this change. The correlation addition accounts for 98 net counted production-source lines. The [updated ledger](receipts/advisor-correlation-source-2026-10-25.json) uses the same nonblank/noncomment line method and LF-normalized UTF-8 SHA-256; tests and docs remain excluded from source counts. This follow-up improves audit reliability and does not claim code reduction by itself.
- The six implementation/test files have a raw diff of 440 additions and 5 deletions; tests account for 326 additions. This diff statistic is separate from the maintained-source ledger.

## Runtime limits

No service was started from the candidate and no staging request, route change, or provider call was made. At verification time, staging gateway and Experts were still on `sha256:a99773d74df38fe676e17054ed9228f53a844449395ba420be7a112b59cd62cd`, started on 2026-10-24, both with restart count 0. This proves the running environment was left untouched, not that the new correlation works there.

Tests construct `UserAPIKeyAuth` directly and capture the outgoing LiteLLM HTTP request with a mocked handler. The real inbound Experts master-key authentication, callback carrier shape, provider-header stripping, and final correlated log have not been verified end to end. A staging window is still needed for that check. Exact-base matching also assumes the deployed route remains `http://experts:4040`.

The implementation is commit `b539b74`; the follow-up code and evidence are published on `origin/codex/advisor-correlation-integration` at `b61fe01b9f6c295976e69466539442109be83dbd`. A fresh `git ls-remote` matched that SHA to local HEAD; `origin/main` remained at `f7b82d8a03c8c11207c76408379e23e1c3c90918`. Staging acceptance is still separate from this published source.
