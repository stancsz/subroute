# Independent review: control-plane save to proxy dispatch

**Reviewer:** `/root/current_policy_test_review`\
**Status:** Passed after assertion repair\
**Date:** 2026-09-25

## Objective

Assess whether `verify_current_model_uses_control_plane_policy_through_proxy` proves that a control-plane API save affects a subsequent public LiteLLM HTTP dispatch through `model: current`, and whether its routing metadata and policy version are checked faithfully. This is deterministic integration evidence only, not evidence of live provider generation.

## Scope

Reviewed root `AGENTS.md`, `docs/northstar/README.md`, `docs/goal/README.md`, root `GOAL.md`, `docs/evals/badlands-protocol-2026-09-25.md`, the targeted test and its `_configured_proxy` test-client setup in `tests/test_codex_proxy_http.py`, and `/api/active-model` plus `async_pre_call_hook` behavior in `src/subroute/plugins/dynamic_router.py`. No source or shared documentation was changed. No live provider calls or service restarts were made.

## Findings

- **The test exercises POST then public route dispatch through the same temporary control plane.** It builds a temporary `RoutingControlPlane` from a temporary YAML file and state path, passes that instance to `DynamicRoutingPlugin`, and patches the route module's `control_plane` global to the same object. `_configured_proxy` mounts the real `proxy_server.app` in FastAPI `TestClient`, installs the plugin in LiteLLM callbacks, and creates a Router with `current`, `codex-luna`, and `codex-terra` deployments. The test posts `codex-terra`/`alias` to `/api/active-model`, then posts `model: current` to `/v1/chat/completions` on that same app and client. The API endpoint calls `control_plane.update`; the hook resolves the same instance's snapshot; LiteLLM dispatches to the terra fixture deployment. The captured `litellm.acompletion` upstream is stubbed and the test checks it received `openai/responses/gpt-6-terra`.

- **The metadata and saved snapshot assertions now cover model, mode, and version.** Following the initial review, the test was updated to assert the POST response contains `active_model == "codex-terra"` and `mode == "alias"`; `metadata.routing` contains the requested and resolved models, mode, and a policy version matching the POST response; and `metadata.gateway_policy` contains the same active model and policy version. In `async_pre_call_hook`, these metadata objects come from the one `RoutingState` returned by `resolve`. The earlier assertion gap is closed in the current test source.

- **GOAL and evaluation wording is appropriately bounded.** GOAL.md says the check uses local and LiteLLM 1.103.0 running-image stubs and does not replace real model generation acceptance. The evaluation also explicitly says this does not establish a real provider completion or staging integration. Do not describe this as successful live generation or proof of the running gateway process.

## Evidence

The targeted test passed locally during the initial review:

```powershell
uv run --extra test --locked pytest -q tests/test_codex_proxy_http.py::test_current_model_dispatch_uses_saved_control_plane_policy
```

Result: **1 passed, 3 warnings** on the locked local environment. After the assertion repair, the parent reported the latest full suite result as **199 passed, 28 warnings** from `uv run --extra test --locked pytest -q`. The parent also reported the LiteLLM 1.103.0 in-container harness result as **PASS**, with `dynamic_routing_cases=1` and `provider_calls=0`. Those latest full-suite and container-harness runs were not independently repeated during this report update.

## Limitations and next action

The test validates one temporary policy update, one alias-mode Chat Completions dispatch, one selected Codex fixture deployment, and metadata produced by the hook. The provider stream is synthetic. It does not test an external HTTP listener, real OAuth, real provider generation, selected-model content quality, production/staging state isolation, or current service configuration. The report's assertion-gap recommendation is resolved; keep the remaining GOAL gates open.
