# Subroute Desktop

This is the native shell for the shared Subroute Control Desk. Both Electron and `/control` load the same HTML, CSS, JavaScript, provider inventory, routing state, and usage data from the gateway. Electron adds only the host capabilities that a browser cannot provide: folder selection, agent installation, and terminal launch. Docker Compose does not start or depend on Electron.

```powershell
cd desktop
npm install
npm start
```

On first launch it creates `%APPDATA%/subroute-desktop/sources.json` from `sources.example.json`. That file contains only the loopback gateway URL. If the gateway is not running or the address needs to change, Desktop opens a connection screen with a local address check and retry. The address is saved only after the gateway responds. Provider inventory and usage adapters are owned by the gateway, so they cannot drift between the browser and Desktop interfaces. Do not place API keys in the Desktop file.

## Launch agents

The launcher starts each installed CLI in a visible PowerShell window rooted at the selected working directory. It checks the CLI's local `--version` command before enabling its Launch button. Claude Code, Codex, OpenCode, OpenClaw, Hermes, and DeepSeek Harness receive a session-only Subroute configuration; their generated profile files live under `%APPDATA%/subroute-desktop/launch/`, not in the agent's existing home directory. The terminal stays open so a gateway or provider error remains visible instead of being reported as a false successful launch.

Each launch follows `current`, so switching the gateway's target keeps the same **256,000-token context budget** and **204,800-token (80%) compaction trigger**. The launcher owns these session settings in `agents.cjs`; they override inherited compaction-disable flags for Claude Code and OpenCode. Relaunch an existing terminal to pick up new settings.

| Client | Native configuration |
| --- | --- |
| Codex | Explicit window and compaction threshold in the catalog, profile and CLI overrides; `model_auto_compact_token_limit_scope = "total"` counts the entire active context, including the carried summary prefix. The catalog uses 100% of the configured window. |
| Claude Code | Unknown alias `current`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS=256000`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW=256000`, and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80`. |
| OpenCode 1.x | Context and input limits of 256000, automatic compaction, no separate tool-output pruning, and a 51200-token reserve. Its native check counts input, output and cache tokens together. |
| Hermes | `model.context_length=256000`, enabled compression with ratio 0.8 and absolute threshold 204800; disable its Codex threshold autoraise. |
| DeepSeek Harness | `contextWindow=256000`; the installed native `compaction-basic` policy defaults to ratio 0.8 and measures the full assembled request. |
| OpenClaw 2026.9 | Isolated provider and agent directory, model context 256000, enabled compaction with reserve 51200 in the agent's native `settings.json`. Project settings can override that reserve. |

Compaction is performed by each client's existing conversation manager. The gateway advertises the policy and enforces the input cap through LiteLLM; setting gateway metadata alone cannot make an arbitrary third-party client compact. Tool results, instructions and attachments are part of the active request budget; multimodal counts depend on the client's/provider's tokenizer and remain estimates where native usage is unavailable. These settings do not enlarge an upstream provider's physical context window.

OpenClaw is not installed on the validation host; its generated settings were checked against the published 2026.9.7 source, without a live session. See [context policy validation](../docs/evals/context-policy-2026-10-01.md) for the runtime checks and remaining verification boundaries.
