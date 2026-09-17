# Gateway design review

Reviewed 2026-09-16 against the working tree, including existing uncommitted changes. The original findings below are retained as the rationale for remediation. The status section records subsequent implementation and verification; this is not a live provider compatibility certification.

## Remediation status

- **State and database isolation:** Compose assigns staging its own policy volume and PostgreSQL service/data volume. Production retains its existing policy and database. In the running staging service, changing the model to desktop and restarting preserved the new policy while production remained openrouter-guided, force mode, policy v18. The production policy file hash did not change. Distinct PostgreSQL system identifiers confirmed separate clusters. Staging was restored to its initial openai/alias selection afterward.
- **Codex transport:** The real LiteLLM Chat-to-Responses bridge is exercised in test_codex_bridge.py with a controlled Responses boundary. It preserves stream=False for the advisor call, which does not meet the streaming-only subscription transport contract. Keep the text collector until the native bridge can force upstream streaming and validate completion while returning a completed advisor result. The collector now requires a completed terminal status, rejects failed/incomplete/truncated/malformed events, handles multiline SSE, reports provider usage, rejects unsupported message content, and enforces a total timeout. No live subscription calls were made.
- **Policy authority:** Persisted advisor selection wins over the ADVISOR_MODEL startup default. The routing hook writes a single policy snapshot into request metadata; injection consumes it without importing global control state. Regression tests change policy between hooks and exercise the actual ProxyLogging callback runner.
- **Credential boundary:** Removed the model-prefix pre-call mutator and obsolete system-to-user conversion. Only the exact Codex deployment URL triggers credential reload and shared provider-boundary system-to-developer normalization. Content is retained, generic Responses deployments are untouched, and output limits are no longer silently removed. Provider rejection of unsupported limits remains visible.
- **Antigravity lifetime:** Timeout, cancellation, and communication aborts kill and reap the CLI and drain communication. Tests exercise both controlled process objects and real sleeping child processes for timeout and cancellation. The stock Docker image still does not provision agy or its authenticated session; this channel is not claimed to be operational there.
- **Runtime reproducibility:** Both Compose gateways use the verified deployed image digest (LiteLLM 1.103.0, Python 3.13). Optional Windows development pins the available LiteLLM 1.101.0 package and uses Python 3.11. The configured package index could not resolve 1.103.0, so this distinction is explicit rather than claiming identical packages. Both interpreters satisfy pyproject.toml, the lockfile is current, and verification runs in both environments.

Production was not restarted as part of the code remediation. Staging isolation is active; source changes are tested independently in a disposable instance of the pinned image. A production restart and live provider compatibility checks are separate rollout steps. Shared static source/configuration still require review before restarting either service.

Final verification: 61 tests passed on Windows Python 3.11/LiteLLM 1.101.0 and 61 passed in the pinned Docker Python 3.13/LiteLLM 1.103.0 image. Compose configuration validation, uv lock consistency, and git diff whitespace checks passed. Remaining warnings originate from framework deprecations and Pydantic typing support. Provider fixtures use synthetic credentials and do not spend provider budget.

## Assessment

The gateway has a relatively small custom implementation: 842 lines in five substantive Python modules, plus 9 package-initializer lines. LiteLLM owns the public protocol implementation. The design needs tighter ownership and stronger transport contracts more urgently than feature deletion.

The previous review overstated several bloat claims. GOAL.md explicitly requires the model switcher, routing modes, and persistence. Removing them would remove requirements. Nineteen configured aliases do not imply nineteen execution engines. Guided aliases offer a simple opt-in contract for clients that only expose a model field; replacing them with custom headers or metadata introduces a different compatibility burden. PostgreSQL supports the requested official dashboard, so its removal needs a product decision. There is no usage evidence establishing that particular model aliases are unnecessary.

## Findings, in priority order

### High: production and staging share mutable policy storage

Both Compose services mount ./config at /app/config and use the same database. Neither sets a separate ACTIVE_MODEL_STATE_PATH. RoutingControlPlane reads its state once and then keeps a process-local snapshot. A change in one process updates the common JSON file while the other process retains its old snapshot. Later writes can overwrite each other, and restart behavior can differ from the previously active policy. The process-local RLock cannot coordinate containers.

Use separate state paths and isolate staging database state where required. Preserve the Docker staging workflow. Its startup policy is an operational choice, not evidence of bloat. Do not add distributed synchronization to solve an isolation problem.

### High: the custom Codex advisor owns a second protocol implementation

handlers/codex_advisor.py sends its own HTTP request, translates messages, parses SSE lines, and constructs ModelResponse, while the ordinary Codex route uses LiteLLM's Responses bridge. Its necessity is not explained by a documented limitation of that bridge.

The collector returns success whenever it has nonempty text deltas. It does not require response.completed or reject response.failed and response.incomplete events. A truncated or failed stream after a text delta can therefore become finish_reason=stop. It discards provider usage and substitutes character-based token estimates without marking them as estimates. Input conversion silently drops non-text blocks and maps developer and tool roles to user. No dedicated tests exercise this handler in the current suite.

First establish whether the existing bridge can satisfy the advisor contract on the deployed LiteLLM version. If it cannot, retain a narrow adapter with a documented reason, explicit input restrictions, terminal-state checks, provider usage handling, and failure-path tests. Do not remove a working channel solely because it is custom.

### Medium: advisor policy has competing sources and hidden coupling

Compose defaults ADVISOR_MODEL to codex-terra-advisor, but RoutingState defaults to gemini-subscription and does not initialize that field from ADVISOR_MODEL. The global AdvisorPlugin imports the global control_plane during a request and overwrites its environment-derived setting with the persisted snapshot. Consequently the environment setting does not control the effective advisor when the control plane is available.

Routing and advisor injection also take separate snapshots, so a policy update between hooks can combine an executor from one policy version with an advisor from another. Callback order is essential, but the current ordering test invokes the two hooks manually rather than testing framework dispatch.

Define one precedence rule and resolve one policy snapshot per request. Pass the resolved advisor choice explicitly to injection. Avoid introducing a general policy framework.

### Medium: credential refresh also performs semantic translation

plugins/codex_credentials.py combines credential loading with system-to-user message conversion, metadata removal, and removal of max_output_tokens. The mutation selector includes every openai/responses/ model, while credential injection uses an exact backend URL check. This can apply subscription-specific translation to unrelated Responses models. The custom advisor translator also preserves system roles, unlike this hook.

Keep credential injection scoped to the authenticated deployment. Put any required message translation at the provider boundary, document why it is needed, and test preservation of instruction intent and supported limits. Reading an updated auth file is credential reload, not active OAuth token refresh.

### Medium: Antigravity subprocess lifetime is unbounded locally

The adapter calls process.communicate without an adapter timeout or cancellation cleanup. It buffers stdout and stderr until the CLI exits. A stalled CLI or cancelled request can leave work running. The handler is registered in process, but every invocation launches an external agy process; describing this as entirely single-process execution is inaccurate.

Use a bounded subprocess lifetime and explicit cleanup on cancellation. Verify CLI availability and authentication inside the chosen deployment environment before advertising that channel as usable. Compose does not itself install or mount the CLI and its session.

### Medium: runtime reproducibility and verification are weaker than the integration surface

Compose uses main-latest, while pyproject.toml permits a broad LiteLLM range. The README documents a specific advisor interceptor version and recommends Python 3.14 tests, despite requires-python excluding 3.14. A passing suite in that interpreter does not establish the deployed framework behavior.

Align the supported interpreter, installed dependencies, and deployed image. Add focused integration coverage for framework callback order and the supported advisor endpoint. Keep the database requirement explicit: configuration currently fails requests when the database is unavailable even though spend logs and error tracking are disabled.

## Keep and simplify

Keep Docker Compose as the deployment model, including the gateway, PostgreSQL, and staging roles. The user's priority is a simple, reliable Docker runtime. Keep native provider configuration, explicit guided aliases, the lightweight model switcher, atomic persistence, and visible failure behavior. These have identifiable requirements and modest implementation cost.

Prioritize changes by the failures they eliminate, rather than lines or containers removed:

1. Resolve the duplicated Codex transport and inconsistent message translation. Establish whether the deployed LiteLLM bridge can serve the advisor before replacing it. Preserve supported channel behavior and test failed or truncated streams and unsupported inputs.
2. Separate production and staging mutable policy state within Compose. Verify that changes and restarts in either environment cannot overwrite the other's policy.
3. Establish one advisor-policy authority and a consistent snapshot per request. Test configuration precedence and actual framework callback behavior.
4. Bound Antigravity subprocess work and clean up on timeout or cancellation. Verify that cancelled requests do not leave provider work running.
5. Align and pin the tested runtime dependencies and Docker image so framework changes are deliberate and verifiable.

Cosmetic cleanup follows these changes. Avoid rewriting the gateway, replacing Docker with host processes, generating configuration, introducing a plugin framework, or splitting every small function into a new module. A small amount of repetition is preferable to an abstraction that creates additional coupling. Additional validation and cleanup code can make the design easier to maintain even when it increases the line count.

The earlier audit ran 27 tests successfully. Those results cover the existing unit/configuration suite; they do not cover the transport and deployment gaps above. No paid provider requests are needed to reproduce most of these failure cases with controlled fixtures.
