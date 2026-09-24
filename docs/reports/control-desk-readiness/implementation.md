# Control desk readiness implementation

**Date:** 2026-09-24
**Scope:** `/control` and the Electron desktop shell
**Status:** Implemented; browser visual review complete at one desktop viewport; Electron visual review is pending

## Product intent

Give a developer a clear place to choose the model for new requests, understand the independent advisor policy, and see which gateway sources are configured and what usage data those sources actually report. The Electron shell shares this control desk and supplies a branded path for connecting to a local gateway when startup cannot reach one.

The North Star remains limited to the local developer workflow in [product direction](../../northstar/README.md). This task does not establish a paid business model, supported release channel, update service, support promise, or launch evidence.

## Changes

- Reworked the shared control desk around a Subroute visual identity, routing-first hierarchy, separate target/advisor controls, policy scope copy, provider grouping, truthful quota states, and responsive CSS breakpoints.
- Made connection and usage failures visible and recoverable. Configured routes stay distinct from sign-in needs, unsupported sources, and unavailable quota; quota remains provider-reported and is not estimated.
- Added a branded Electron connection screen. The desktop accepts only loopback HTTP gateway URLs, probes `/api/source-status`, checks the response shape before saving, stores only the gateway URL, and reports connection failures in the screen.
- Kept browser and Electron on the shared gateway-hosted `/control` frontend. No frontend dependency or remote font service was added.
- Upgraded the Electron development dependency to exact version 44.4.5 after the prior pinned version's audit still returned high-severity advisories.
- Reorganized the existing architecture and design-review documents into `docs/misc/` and `docs/evals/`, and corrected the GOAL link.

## Evidence

- The in-app browser rendered `http://127.0.0.1:4000/control` at a captured viewport of 1208 × 900. I inspected the routing panel, gateway status, configured connection cards, provider-reported quota, and expanded unavailable/setup sources. The endpoint returned routing options and provider inventory, and the controls were enabled at the time of capture.
- The browser screen showed seven configured sources, one source requiring sign-in, three providers with quota unavailable, and six additional sources that were not ready. Provider quota values and reset timing were displayed as returned by the gateway.
- Electron 44.4.5 launched and created a responsive window titled `Subroute | Local AI gateway`. This verifies process/window startup only. The available UI bridge could not inspect the native window or the local connection page, so the recovery flow has not passed visual acceptance.
- Static syntax checks passed with `node --check` for the shared control script and the Electron main, preload, and connection scripts. `git diff --check` passed.
- `npm audit --json` in `desktop/` reported zero vulnerabilities across 13 dependencies; `npm list electron --depth=0` reported `electron@44.4.5`.
- The independent source critic confirmed that the zero-configured-route copy, hostname-dependent loopback badge, and Electron source booleans are corrected. See [critic report](critic.md).

## Not exercised

- No tests were run, per the session instruction not to add or run tests unless requested. Syntax and audit commands are not tests.
- No model-generation request, credential edit, route-policy mutation, package build, installer, signing, update, or distribution workflow was exercised. During live visual review I clicked the existing **Refresh usage** control once; its documented gateway path sent authenticated, read-only usage/status requests to OpenAI, MiniMax, and OpenRouter, plus the configured local Gemini subscription bridge. The request completed and refreshed the displayed snapshot. I did not send any prompt or model-generation request.
- Responsive CSS includes a narrow breakpoint, but this environment did not expose a viewport override. A narrow browser rendering and the Electron connection/desk screens at native dimensions remain unverified.
- Electron 44 is a major version change. The app's window startup was smoke-checked, but release packaging and every host integration still need verification. Its official release notes state that macOS 13+ is required; current desktop agent discovery is Windows-oriented. See [Electron 44 release notes](https://www.electronjs.org/blog/electron-44-0).

## Assessment

The presentation and control semantics are materially stronger and the reviewed browser screen reads as a coherent local developer product. Acceptance is partial until narrow-screen and actual Electron-screen review are completed. Commercial readiness remains unproven: distribution, upgrades, support, first-run provider setup, and a commercial model require their own decisions and evidence.
