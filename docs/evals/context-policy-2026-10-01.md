# Subroute context policy, 2026-10-01

**Follow-up:** The user subsequently requested a smaller Advisor budget to reduce consultation input. Current Advisor routes use a 32,000-token maximum and 25,600-token compaction target. Target routes and Desktop session profiles retain the 256,000 / 204,800 policy evidenced below; the original all-routes result records the earlier state. For oversized Advisor copies, the deployed implementation tries one MiniMax M3 compaction and then one GPT-6 Luna compaction if needed. Each gets at most one call and a timeout; MiniMax has an 8,192-token output cap, while the Codex subscription endpoint manages Luna's output limit because it rejects caller output caps. Provider usage is recorded separately from the LiteLLM token estimate, and the main request remains unchanged. Production and staging readiness returned HTTP 200 after restart. Provider-backed recovery has not been exercised to avoid an extra billed request during verification.

At the initial verification, the requested policy was 256,000 tokens for every Subroute model and automatic client compression at 204,800 tokens (80%). The budget includes the full active request: instructions, conversation, tool definitions/results, attachments and carried summaries. Native tokenizers estimate these components differently, particularly multimodal inputs.

## Ownership and implementation

- At the initial verification, `config/litellm.yaml` covered 21 deployments, including dynamic `current`, Auto and its private fallbacks, subscription and local routes. `config/litellm.experts.yaml` covered four dedicated Advisors. The follow-up above supersedes the initial Advisor values; target route and client values remain as measured here.
- LiteLLM's existing `enable_pre_call_checks` enforces estimated input limits. Its native counter handles Chat, Responses and Messages instructions and tools. No separate tokenizer, request trimming, conversation store or gateway summarizer was added. This input check does not guarantee input plus requested output fits an upstream provider's window.
- LiteLLM runs router checks after proxy callbacks. The existing early Advisor consultation needs a narrow guard before spending provider tokens. It reuses the live router's configured limit and native counter on a worker thread. LiteLLM checks the final target request after advice injection. Retire this guard when the framework checks input before callbacks. Its private counter is the new compatibility burden, checked on both installed framework versions.
- `desktop/agents.cjs` owns client session compression settings. Codex uses scope `total` and 100% effective window; Claude Code uses native window/percentage environment controls; OpenCode reserves 51,200 input tokens; Hermes uses ratio and absolute threshold; installed DSH defaults to ratio 0.8; OpenClaw uses native agent settings. Clients own compression and its failures.
- No service, dependency, conversation store, retry or fallback was added. Compose production, staging and expert roles remain intact. Routing selections and policy versions stayed unchanged.

## Local profiles and precedence

Generated Desktop profiles were refreshed under `%APPDATA%/subroute-desktop/launch/`. Existing direct Subroute profiles were updated at `%USERPROFILE%/.codex/router.config.toml` and `%LOCALAPPDATA%/hermes/config.yaml`. Codex uses the registered `current` alias and a matching catalog. Hermes's staging provider and delegation use `current` and the same budget. Other provider profiles were preserved.

Existing-file backups are in `%USERPROFILE%/.codex/backups/subroute-context-2026-10-01/`. Relaunch existing sessions and restart the Desktop shell to load the launcher code. Explicit client/project overrides can supersede native defaults, including OpenClaw project `settings.json`. External clients need equivalent native settings; gateway metadata cannot control their conversation managers.

## Evidence

| Check | Result |
| --- | --- |
| Python suite | 467 passed, 11 skipped, 29 dependency warnings; 24.19 seconds |
| Desktop tests | 5 passed after final OpenClaw correction |
| Compose | Configuration validation passed |
| Live metadata/readiness | Production 4000: 21 entries; staging 4005: 21; experts 4040: 4. Every entry has the four policy fields; all three readiness checks pass. |
| Native LiteLLM | 256000 accepted, 256001 rejected. Instruction/tool overhead counted in all three protocols. Checked on development 1.101.0 and deployment 1.103.0. |
| Staging HTTP matrix | 24 cases passed: all inventory entries through Chat, plus Responses instructions, Messages system and Chat tool schema. Public routes reject oversized input with HTTP 400; private aliases retain their existing HTTP 404 access boundary. |
| Production/experts HTTP | 8 cases passed: production current/auto, Responses instructions and Messages system; all four expert Advisor aliases. Context-limit HTTP 400. |
| Routing preservation | Snapshots unchanged, including production version 80 and staging version 480, target, mode, Advisor and effort. |
| Codex 0.154.0 live | Production Responses request completed and emitted the requested marker. Persisted token-count event reports context 256000. Usage: input 9732, cached input 146, output 45, reasoning 40. |
| Hermes native compressor | Context 256000, threshold 204800; should-compress false at 204799 and true at 204800. |
| OpenCode 1.15.10 | Native debug config confirms Subroute/current, input/context 256000, reserve 51200, automatic compaction, pruning disabled. Native overflow check includes cached tokens. |
| DSH 0.1.1-rc.2 | Adapter config has context 256000; installed native compaction defaults to enabled, ratio 0.8, full assembled-request measurement. Threshold uses floor(contextWindow times ratio). |
| OpenClaw 2026.9.7 | Generated profile checked against published source. Retired main-config context/reserve keys removed; agent settings.json owns reserve 51200. Runtime not installed locally. |

The first Codex probe preceded gateway readiness and failed to connect. A separate probe after readiness passed; no automatic retry was introduced. Early HTTP probes assumed every inventory entry was public and encountered existing private-alias 404s; the final matrix checks that boundary.

See the [sanitized receipt](context-policy-2026-10-01.json). Detailed local probe outputs remain in `%USERPROFILE%/.codex/tmp/subroute-context-2026-10-01/`.

## Verification boundaries

Evidence establishes configured budgets, native calculations, real HTTP refusals and a normal live Codex request. It does not establish a live 204,800-token compression cycle in every client, successful 256,000-token provider requests or live OpenClaw compatibility. Claude Code controls were checked against documentation without a long-session probe. Smaller provider windows still apply. Stateless callers must manage their own compression.

Primary references: [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference), [Claude Code environment controls](https://code.claude.com/docs/en/env-vars), [OpenCode 1.15.10 overflow calculation](https://github.com/anomalyco/opencode/blob/v1.15.10/packages/opencode/src/session/overflow.ts), [OpenClaw 2026.9.7 settings storage](https://github.com/openclaw/openclaw/blob/v2026.9.7/src/agents/sessions/settings-storage.ts), [OpenClaw reserve application](https://github.com/openclaw/openclaw/blob/v2026.9.7/src/agents/agent-settings.ts), [OpenClaw full-request pressure](https://github.com/openclaw/openclaw/blob/v2026.9.7/src/agents/embedded-agent-runner/run/preemptive-compaction.ts).
