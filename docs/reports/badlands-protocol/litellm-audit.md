# Badlands protocol compatibility audit

**Date:** 2026-09-25\
**Auditor:** delegated read-only subagent; handoff recorded by the orchestrator\
**Goal:** [Badlands API Gateway local protocol compatibility](../../../GOAL.md#补充范围badlands-api-gateway-本地协议兼容)\
**Status:** Partial findings; no provider calls or service changes

## Findings

- The running production and staging containers report LiteLLM 1.103.0. The Windows project environment pins LiteLLM 1.101.0 in `pyproject.toml` and `uv.lock`; `docs/evals/design-review-2026-09-16.md` records this intentional difference.
- The installed 1.101.0 Chat-to-Responses bridge passes the caller's `stream=False` to `aresponses`. It collects a stream only if one is returned, so this fixture does not prove a streaming-only backend can complete a non-streaming Chat request. See `tests/test_codex_bridge.py`.
- The installed Anthropic Messages-to-Responses adapter passes through the caller stream setting. For non-streaming requests it requires a `ResponsesAPIResponse`; it does not collect a stream iterator.
- LiteLLM's native `chatgpt` Responses provider forces upstream streaming and assembles completed SSE into a Responses result. Its authenticator expects root-level `access_token` and `account_id`; Subroute's existing auth file stores these under `tokens`. Its request transformer also appends default Codex instructions and rebuilds requests from an allowlist, so it may change or omit caller parameters. It is not a drop-in provider switch.
- A Codex-only `CustomLLM` Chat adapter delegates public protocol conversion to LiteLLM. Its non-streaming handler forces the upstream stream and assembles native Chat chunks; its streaming handler converts Chat chunks to LiteLLM's `GenericStreamingChunk` contract. SDK dispatch fixtures on LiteLLM 1.101.0 cover Chat, Responses, and Messages in both stream modes. Roleless function-call request history is converted into assistant tool calls and tool messages with IDs and content intact. The running 1.103.0 container passed the same six deterministic protocol/mode fixtures in an in-container Python harness. Gateway HTTP endpoints remain unverified.
- LiteLLM accepts Responses string input at its public boundary. Subroute's exact Codex deployment hook previously left string input unchanged, conflicting with the backend's recorded list-only requirement.

## Subroute change and evidence

The Codex credential hook now wraps only string `input` values at the exact Codex API base as one user message. Existing list input is passed through the existing role normalization path. Regression tests preserve the original request object and assert that `store: false`, image content, and function-call output fields survive. A public `litellm.aresponses` dispatch fixture covers roleless function-call request history.

The same boundary no longer removes `max_output_tokens` or `user`; the hook preserves both fields. If the provider rejects either, that rejection remains visible instead of reporting success after silently dropping an output limit.

`tests/test_codex_proxy_http.py` additionally exercises LiteLLM's in-process proxy HTTP routes for Chat Completions, Responses, and Anthropic Messages, with streaming and non-streaming requests. Only the Codex upstream stream is mocked. The proxy serializes its configured deployment identity (`codex-luna`) while the captured backend dispatch uses `openai/responses/gpt-6-luna`. This establishes the LiteLLM proxy boundary only; the external Badlands/OpenRouter target match, active local gateway, staging, and hosted image route remain open.

Focused command: `uv run --extra test --locked pytest -q tests/test_codex_subscription.py tests/test_codex_credentials.py tests/test_codex_bridge.py tests/test_litellm_config.py tests/test_codex_proxy_http.py`\
Result: 29 passed on Python 3.13.11 / LiteLLM 1.101.0. SDK fixtures exercise public protocol conversion and mock only the Codex upstream response stream. A missing provider-usage fixture confirms locally estimated usage is removed from the final response. The HTTP test covers six protocol/mode combinations.

Full repository suite: `uv run --extra test --locked pytest -q` -> 187 passed with 28 dependency deprecation and schema warnings on the same runtime.

The earlier global Python 3.14 / LiteLLM 1.100.1 run is not used as acceptance evidence because it is outside the repository's supported interpreter range. The running Docker container does not include pytest; an in-container Python harness exercised the same LiteLLM SDK routes with mocked streams. No service restart, staging request, or provider generation call was made. The current running HTTP service has not loaded the edited configuration.

## Advisor consultation

- Model: `codex-sol-advisor`
- Prompt/output usage: 1,011 tokens total (512 prompt, 499 completion)
- Advice: intercept only Codex-bound non-streaming Responses dispatch, request upstream streaming, and assemble a complete Responses result with LiteLLM's native machinery; stop if image or tool content cannot be preserved.
- `decision_changed: true`. This directed the next investigation toward LiteLLM's existing provider and stream-assembly path before considering a custom adapter.

## Remaining work

Verify the updated config through local gateway HTTP endpoints and staging for all three protocols, both stream settings, model identity, and hosted image routing. The built-in `chatgpt` provider remains unacceptable because it changes instructions and may filter parameters. No live provider request or gateway restart has been run.
