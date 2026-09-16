# unified-llm-gateway

A deliberately small Python 3.12 kernel for sending one OpenAI-compatible
messages request through LiteLLM and returning LiteLLM's `ModelResponse`.

## What version 0.1 contains

- One Pydantic request model: `model`, `messages`, `temperature`, `max_tokens`,
  and `timeout`.
- One async `complete(request, adapter)` entry point.
- Thin adapters for OpenAI subscription, Gemini through the authenticated local
  Antigravity CLI, FreeToken, and a generic desktop local API. OpenAI
  subscription uses LiteLLM's Responses-to-Chat bridge. Gemini returns the same
  LiteLLM `ModelResponse` contract without requiring a web sidecar.
- Direct MiniMax API support with multiple keys through LiteLLM Router's native
  deployments and `simple-shuffle` strategy.

The bridge defaults mirror the locally verified LeanRouter topology, but this
project does not import or modify LeanRouter:

| Channel | Default endpoint or provider |
| --- | --- |
| OpenAI subscription | `https://chatgpt.com/backend-api/codex` |
| Gemini subscription | Local authenticated `agy` executable |
| MiniMax API keys | LiteLLM `minimax/` provider |
| FreeToken | `http://127.0.0.1:1919/v1` |
| Desktop local API | `http://127.0.0.1:11434/v1` |

OpenAI subscription reads `~/.codex/auth.json` by default and requires both the
access-token and account-id fields. Set `AGY_PATH` when the Antigravity CLI is
not on `PATH`. Override endpoints with `OPENAI_SUBSCRIPTION_BASE_URL`,
`FREETOKEN_BASE_URL`, or `DESKTOP_LLM_BASE_URL`. MiniMax reads comma-separated `MINIMAX_API_KEYS` plus
the optional single-key compatibility variable `MINIMAX_API_KEY`. Empty and
duplicate keys are removed without logging key material.

Antigravity accepts `model`, `messages`, and `timeout`. Its CLI does not expose
the kernel's temperature or output-token controls, and it does not report token
usage. The adapter therefore labels no usage source and includes only the same
text-length estimate used by the original bridge.

## Explicit non-goals

There is no HTTP server, provider selection policy, schema clone, retry layer,
fallback, response normalization, streaming parser, configuration framework,
database, queue, UI, telemetry exporter, or plugin system. LiteLLM owns provider
calls and MiniMax key routing. A future channel should normally be another thin
adapter or an external sidecar, not more kernel policy.

Public streaming is intentionally not supported in version 0.1. The OpenAI
subscription backend requires a stream, so that adapter lets LiteLLM aggregate
it into the same non-streaming `ModelResponse`; the kernel owns no stream parser.

## Development

```powershell
py -3.12 -m pip install -e ".[test]"
py -3.12 -m pytest -q
```

Tests use fake LiteLLM clients. They require no provider credentials and make no
network requests.
