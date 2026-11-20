# Subroute local model routing

Serve coding clients through a local LiteLLM endpoint. Subroute maintains
Codex and Antigravity subscription adapters and the local model selector.
LiteLLM owns configured API routes, fallbacks and usage reporting.

Production and staging use separate databases and state on one PostgreSQL
server. Antigravity and staging are opt-in Compose profiles.

- A client using `default` follows the saved selection across restarts.
- A client specifying a model uses its configured route.
- Codex Chat Completions, Responses and Messages preserve streaming and tool calls.
- Antigravity retains explicit text and audio routes. Codex retains explicit image generation.
- The gateway image contains runtime dependencies. Optional tools run as HTTP clients.

Earlier implementation and acceptance evidence is in Git history and
[the evaluation receipts](docs/evals/receipts).
