# Acceptance after every implementation

[AGENTS.md](../../AGENTS.md#hard-rules-preserve-upstream-success-and-pass-acceptance) is authoritative for upstream-success preservation and mandatory acceptance. This procedure applies to each implementation increment, including fixes, refactors, configuration and dependency changes. A green historical run cannot accept a later edit.

## Required behavior

Compare the same admitted request, selected provider/model, credentials, policy and caller limits. A healthy upstream must yield a semantically valid successful response through Subroute. Preserve instruction roles/content, tool schemas/arguments/results and IDs, output limits, caller-visible identity, protocol events and terminal status. Do not turn provider success into a gateway schema error, premature timeout, truncated stream, broken tool continuation, or observational-storage failure.

Enforce documented authentication/authorization, signed identity integrity and explicit security/budget controls. Do not silently add new admission restrictions or label an upstream-supported payload unsupported merely because an adapter is missing. Such a translation gap remains a gateway defect. A provider refusal, limit stop, client cancellation or incomplete stream must retain its real meaning; never manufacture successful completion.

Accounting, logs and optional services must not become required dependencies of ordinary inference. Bound their work and isolate failures. Preserve numeric facts when possible, and report missing measurements explicitly. The contract does not permit bypassing credentials, dropping tool semantics, hidden provider retries or policy changes to make a test pass.

## Gate for each increment

1. Before editing, state the expected user-visible behavior, affected protocols/routes/runtime, failure case and acceptance command in the existing goal or task record. For refactors, establish the relevant baseline before changing structure.
2. After the final implementation change, run behavioral acceptance that exercises the affected public boundary and failure path. Pair a captured defect with a regression that would fail on the prior behavior. Exercise streams through terminal events and tools through their matching result/continuation, not just the first HTTP response.
3. Run affected regression checks. Shared routing, protocol, accounting, dependency or image changes require the complete non-live repository suite in addition to the specific boundary checks. Explicitly invoke any relevant suites under `tests/acceptance/`: default pytest discovery excludes that directory.
4. For provider compatibility, actual CLI/client behavior or deployment claims, obtain authorized acceptance in the affected runtime. Where practical, compare a controlled upstream success with the same request through the gateway. Offline fixtures prove local behavior only. Match source/image/dependency identity and recheck after container recreation for a durable rollout claim.
5. Record the final diff/source hash or revision, command, environment/runtime, pass/fail/skip counts, terminal result, sanitized receipt location and remaining gaps. Separate production target, staging target and Advisor/Experts results. Call recovery proven only with matching request/attempt records; otherwise record unknown.
6. Acceptance must pass before the increment is called complete, merged or promoted. Fix failures and rerun after the repair. A failed, skipped, blocked, stale or narrower test does not satisfy the required gate. Preserve unrelated baseline failures and leave affected acceptance open until resolved; do not delete assertions, lower requirements or infer a pass from readiness.

An emergency restart or minimal repair can restore service before complete verification when authorized. Mark it provisional, retain rollback/evidence, and perform post-repair acceptance. Copying a file into a container does not prove the image will retain it. The same implementation must survive recreation before rollout is complete.

## Existing checks and their limits

Run from the repository root with the checked-in lock and supported Python (currently 3.13 in the Docker recipe). The following offline gate sends no provider requests; the non-live socket guard in `tests/conftest.py` rejects deployed gateway/provider connections:

Use the verified tokenizer cache for offline runs: point `TIKTOKEN_CACHE_DIR` and `CUSTOM_TIKTOKEN_CACHE_DIR` at the same existing cache (the Docker recipe packages it at `/opt/subroute/tokenizer-cache`). Keep `LITELLM_LOCAL_MODEL_COST_MAP=true` to avoid remote cost-map refreshes. Missing cache/dependency prerequisites are failed or unavailable test evidence, not provider failures; prepare the environment and rerun without bypassing the network guard or assertions.

```powershell
uv run --locked --all-extras --python 3.13 python -m pytest -m "not live" -q
```

For focused diagnosis, choose the affected modules below. A focused pass does not replace the full suite when shared code changes.

| Boundary | Existing checks | Required observation |
| --- | --- | --- |
| Public Chat, Responses and Messages conversion | `tests/test_codex_proxy_http.py`, `tests/test_client_stream_contracts.py`, `tests/test_thinking_format.py` | Real proxy/serializer with provider fixtures; preserved payload, tool continuation, reasoning separation and terminal events |
| Receipt failure isolation and recovery | `tests/test_usage_outbox.py`, `tests/test_usage_accounting.py`, `tests/test_usage_accounting_protocols.py` | Dispatch/output survive storage faults; usage remains truthful, bounded, isolated and recoverable |
| Routing and requested identity | `tests/test_dynamic_routing.py`, `tests/test_auto_routing.py`, `tests/test_response_model_identity.py` | Selected route/policy and visible model identity are preserved; no private expert exposure |
| Optional Advisor and private expert boundary | `tests/test_advisor_plugin.py`, `tests/test_expert_sift.py`, `tests/test_expert_transport.py` | Target and expert failures remain separate; explicit consultation budget, tool semantics and continuation are respected |
| Actual deployed provider/client behavior | `tests/test_live_gateway_failure_matrix.py`, `tests/test_live_coding_clients.py`, `tests/acceptance/claude_cli_4000/README.md` | Authorized real task-shaped requests, tools and terminal outcomes in the named runtime |
| Desktop-only change | `npm --prefix desktop test` | Relevant launcher/control behavior using the shipped test command |
| Docs/instructions only | Validate edited-document consistency, relative links/anchors and referenced commands/files | Hard rules agree across active entry points; historical receipts remain dated; no unverified behavior is presented as accepted |

These files are a starting map, not a claim of exhaustive coverage. Add the missing behavioral case for the changed requirement, including full outbox capacity and recovery, storage delay, and provider-specific tool translations when relevant. Required tests cannot be deselected merely because they fail.

Live tests require explicit authorization, credentials and the documented opt-in variables. Read each harness first: some temporarily change staging policy or create scoped credentials, and the Claude CLI suite targets production port 4000. Do not launch them under a no-provider-call, no-credential-change or no-production-change mandate. Record the missing authorized evidence as an open gate, continue permitted checks, and do not mark live acceptance passed.

Readiness, HTTP 200, builds, source hashes and service restart receipts prove only their own boundary. Gateway/upstream attribution needs matching evidence; a fallback success cannot prove recovery of the original attempt. Redact prompts, generated content, tool arguments, secrets and user identifiers from retained reports.
