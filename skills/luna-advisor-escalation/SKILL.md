# Luna Advisor migration notice

The former Luna Advisor skill has moved to the maintained **[Codex Advisor skill in Northstar](https://github.com/stancsz/northstar/tree/main/skills/codex-advisor)**. Install the four-skill package: `northstar`, `codex-qa`, `codex-subagents` and `codex-advisor`.

This file is a redirect, not an active skill. Do not use the archived Luna-specific task ownership or model-selection guidance. Subroute owns the Advisor service, gateway runtime and provider authentication. Worker model selection follows the gateway's current saved routing target; this document does not pin workers to Luna.
