# Control desk and Electron review

**Date:** 2026-09-24; visual recheck 2026-09-25
**Verdict:** UI implementation delivered; visual acceptance partial; commercial release readiness not established

## Acceptance ledger

| Area | Result | Evidence and limit |
| --- | --- | --- |
| Shared browser/Desktop control desk | Pass in source | Electron opens the same gateway-hosted `/control` frontend. Routing, advisor, reasoning, policy, source, and usage contracts remain owned by the gateway. |
| Routing scan and policy clarity | Pass in the inspected browser view | Target and advisor have separate controls; policy scope is visible. No route mutation was made during review. |
| Provider inventory truthfulness | Pass for observed API state | Seven configured routes, sign-in required, unavailable quota, setup needed, and unsupported sources have distinct labels. Usage is not estimated. |
| API failure recovery | Pass in source review | Inventory failure changes the summary to unavailable, clears stale cards, and offers refresh. Usage-only failure retains the inventory with a quota error state. |
| Electron URL boundary | Pass in source review | Only loopback HTTP with no URL credentials is accepted; a timed API probe and source payload validation precede the settings write. |
| Browser visual quality | Inspected, one desktop viewport | Fresh read-only browser capture at 1278 × 910 on 2026-09-25 shows the routing-first layout, separate target/advisor controls, policy scope, and configured source cards. [View the capture](control-desk-live-2026-09-25.png). The live page showed production `codex-luna`, Force mode, policy v10. No routing controls were changed. This is not a narrow-width or human release approval. |
| Electron visual quality | Unverified | Electron 44.4.5 opened a responsive titled window. The native UI was inaccessible to the available visual-control interface. |
| Narrow browser layout | Partial | At a 390 × 844 emulated viewport, the first-fold capture showed the routing header and target card in a single column. `document.documentElement.scrollWidth` was 390, equal to the viewport, so there was no horizontal overflow. A second capture at the same viewport visibly shows the target and advisor controls stacked vertically and the beginning of the request-policy section ([first fold](control-desk-narrow-live-2026-09-25.png), [target, advisor, and policy transition](control-desk-narrow-mid-live-2026-09-25.png)). The screenshot helper remained unreliable after scrolling, so the rest of the policy and source sections were not visually verified. |
| Commercial launch readiness | Not established | No installer, signing, update path, supported release channel, customer onboarding commitment, support model, pricing, or production rollout was delivered or validated. |

## Independent source review

The critic's first review found misleading inventory failure state, overbroad Electron response acceptance, privacy wording, and small text. The final source review confirms those source-level concerns were addressed by an explicit unavailable/retry state, gateway response field validation, hostname-dependent browser wording, and larger essential UI text. The critic could not independently inspect rendered browser or Electron output. Its full account is in [the report](../reports/control-desk-readiness/critic.md).

## Local verification

- `node --check` passed for `src/subroute/control/app.js`, `desktop/main.cjs`, `desktop/preload.cjs`, and `desktop/connect.js`.
- `git diff --check` passed.
- Full `npm audit --json` in `desktop/` returned 0 total vulnerabilities.
- Electron startup produced a responsive window with title `Subroute | Local AI gateway`.
- No tests or model-generation requests were run. One existing **Refresh usage** interaction was used to populate live UI data; the gateway sent authenticated quota/status reads to OpenAI, MiniMax, OpenRouter, and the configured local Gemini bridge. No route or credential settings were changed.

## Follow-up for release evaluation

Finish visual review of the lower control-desk sections at narrow browser width and inspect actual Electron dimensions, including initial no-gateway, invalid URL, incompatible service, offline recovery, and keyboard focus. Separately establish target OS/release channel, packaging and signing, update behavior, provider onboarding, support expectations, and the intended commercial model before claiming launch readiness.
