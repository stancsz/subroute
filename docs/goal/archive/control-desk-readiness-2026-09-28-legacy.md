# Control desk and Desktop readiness

**Owner:** Orchestrator (root task)
**Status:** Implementation complete; visual acceptance partial
**North Star:** [Product direction](../../northstar/README.md)

## Outcome

Make `/control` and the Electron shell feel like a coherent, trustworthy local developer product while preserving routing policy, provider inventory, and gateway ownership.

## Acceptance

- The browser and Desktop share one polished control desk and retain the existing routing API contract.
- Users can distinguish a ready connection from a source needing setup or unavailable provider quota.
- Routing and advisor selection remain easy to scan, including their independent reasoning settings and policy scope.
- Electron shows a useful, branded connection screen when the configured gateway cannot be reached; it accepts and saves only a responding loopback HTTP gateway.
- No provider secrets move into the Desktop settings file or frontend. The UI does not estimate provider quota or claim capabilities the backend has not exposed.
- Inspect real desktop and narrow browser rendering; record what was and was not exercised.

## Work and decisions

- Shared browser and Electron renderer remains `src/subroute/control/`; `desktop/` keeps only host APIs and connection recovery.
- Provider status and routing continue to use the existing gateway endpoints. The new Desktop handoff checks `/api/source-status` before saving the already-supported gateway URL.
- External font loading is removed to keep rendering offline-capable and avoid unnecessary third-party requests.

## Handoff

The shared control desk and Electron connection recovery are implemented. The live browser view was inspected at desktop width and at a 390 px emulated narrow width. At narrow width, the first fold had no horizontal overflow; a second capture shows the target and advisor controls stacked vertically and the start of the request-policy section. The rest of the policy and source sections could not be visually verified because the browser screenshot helper remained unreliable after scrolling. Native Electron screen inspection also remains unverified. Product distribution, updates, support, and commercial-model decisions remain open. Do not describe the project as commercially ready from this task alone. Task report: [implementation and review](../../reports/control-desk-readiness/implementation.md). Evaluation: [visual and behavior review](../../evals/control-desk-readiness.md).
