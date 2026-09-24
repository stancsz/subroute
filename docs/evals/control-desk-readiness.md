# Control desk and Electron review

**Date:** 2026-09-24
**Verdict:** UI implementation delivered; visual acceptance partial; commercial release readiness not established

## Acceptance ledger

| Area | Result | Evidence and limit |
| --- | --- | --- |
| Shared browser/Desktop control desk | Pass in source | Electron opens the same gateway-hosted `/control` frontend. Routing, advisor, reasoning, policy, source, and usage contracts remain owned by the gateway. |
| Routing scan and policy clarity | Pass in the inspected browser view | Target and advisor have separate controls; policy scope is visible. No route mutation was made during review. |
| Provider inventory truthfulness | Pass for observed API state | Seven configured routes, sign-in required, unavailable quota, setup needed, and unsupported sources have distinct labels. Usage is not estimated. |
| API failure recovery | Pass in source review | Inventory failure changes the summary to unavailable, clears stale cards, and offers refresh. Usage-only failure retains the inventory with a quota error state. |
| Electron URL boundary | Pass in source review | Only loopback HTTP with no URL credentials is accepted; a timed API probe and source payload validation precede the settings write. |
| Browser visual quality | Inspected, one desktop viewport | Direct screenshot at 1208 × 900 showed the routing-first layout, configured cards, quota state, and expandable other providers. This is not a narrow-width or human release approval. |
| Electron visual quality | Unverified | Electron 44.4.5 opened a responsive titled window. The native UI was inaccessible to the available visual-control interface. |
| Narrow browser layout | Unverified | CSS defines narrow breakpoints; no viewport override was exposed for direct inspection. |
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

Inspect the same first-run connection and routing journey at narrow browser width and actual Electron dimensions, including initial no-gateway, invalid URL, incompatible service, offline recovery, and keyboard focus. Separately establish target OS/release channel, packaging and signing, update behavior, provider onboarding, support expectations, and the intended commercial model before claiming launch readiness.
