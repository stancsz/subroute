# unified-llm-gateway

A deliberately small LiteLLM Proxy deployment for coding tools. The project
does not implement OpenAI Chat Completions, OpenAI Responses, or Anthropic
Messages itself. LiteLLM Proxy owns the public HTTP protocols, streaming, tool
events, request translation, and standard provider adapters.

## Runtime shape

```text
Codex / Claude Code / other coding tools
                  |
                  v
       LiteLLM Proxy on 127.0.0.1:4000
                  |
        explicit model_name selection
                  |
 OpenAI / Gemini / MiniMax / OpenRouter / Ollama / in-process Antigravity
```

The checked-in configuration exposes one virtual alias and ten physical model
aliases:

| Alias | LiteLLM transport |
| --- | --- |
| `current` | Virtual entry resolved by the dynamic routing callback |
| `openai` | Native `openai/` provider |
| `openai-guided` | OpenAI executor with automatic Advisor injection |
| `gemini-api` | Native `gemini/` provider |
| `openrouter` | Native `openrouter/` provider, defaulting to `minimax/minimax-m3` |
| `minimax` | Native `minimax/` provider |
| `minimax-guided` | MiniMax executor with automatic Advisor injection |
| `freetoken` | Native OpenAI-compatible transport |
| `desktop` | Native `ollama/` provider |
| `codex-subscription` | LiteLLM Responses bridge to `gpt-5.6-sol` plus per-request credential refresh |
| `codex-astra` | LiteLLM Responses bridge to `gpt-6-astra` |
| `codex-terra` | LiteLLM Responses bridge to `gpt-5.6-terra` |
| `codex-luna` | LiteLLM Responses bridge to `gpt-5.6-luna` |
| `codex-reserve` | LiteLLM Responses bridge to `gpt-reserve` |
| `codex-terra-advisor` | Terra as an Advisor-only model |
| `codex-sol-advisor` | Sol as an Advisor-only model |
| `codex-astra-advisor` | Astra as an Advisor-only model |
| `gemini-subscription` | In-process LiteLLM `CustomLLM` provider for `agy` |

Edit the model IDs in `config/litellm.yaml` to match the models available to
the account. Add another `model_list` deployment when a channel needs another
key. Do not add a Python adapter for a provider LiteLLM already supports.

## Install and run

### Docker Compose (Recommended)

Run both LiteLLM and PostgreSQL together via Docker Compose:

```powershell
docker compose up -d
```

To view logs or restart:
```powershell
docker compose logs -f gateway
docker compose restart gateway
```

To shut down:
```powershell
docker compose down
```

### Local Host Process (Optional)

```powershell
./scripts/setup.ps1

docker compose up -d postgres

$env:OPENAI_API_KEY = "..."
$env:GEMINI_API_KEY = "..."
$env:MINIMAX_API_KEY = "..."
$env:OPENROUTER_API_KEY = "..."
./scripts/start-gateway.ps1
```

The setup script creates an isolated Python 3.11 `.venv`, installs both the
proxy and database extras, and generates the Prisma client. Python 3.14 is not
used because `prisma-client-py` is not compatible with its Pydantic v1 layer.

The official Admin UI at `http://127.0.0.1:4000/ui/` requires PostgreSQL.
The Compose service binds PostgreSQL only to `127.0.0.1:5433` and retains its
data in a named Docker volume. The launcher defaults `DATABASE_URL` to that
local service. Override `DATABASE_URL` before launch to use another PostgreSQL
instance.

The local gateway does not require a client bearer token by default. This is
safe only because both Compose and the PowerShell launcher bind it to
`127.0.0.1`. To require client authentication, set `GATEWAY_MASTER_KEY` before
starting the gateway. Set `UI_USERNAME` and `UI_PASSWORD` if you need separate
dashboard credentials. SSO is unnecessary for this loopback-only, single-user
deployment.

Select model alias `openrouter` from the client to use OpenRouter. To use a
different OpenRouter model, replace `minimax/minimax-m3` in
`config/litellm.yaml` with its OpenRouter model slug while keeping the
`openrouter/` prefix.

FreeToken defaults to `http://127.0.0.1:1919/v1` with a placeholder local key.
The desktop alias uses LiteLLM's native `ollama/qwen2.5-coder` provider and
defaults to `http://127.0.0.1:11434`. Override `OLLAMA_API_BASE` when needed.

Point production OpenAI-compatible tools at `http://127.0.0.1:4000/v1`.
Use `./scripts/start-gateway.ps1 -Port 4005` for an isolated staging process.
LiteLLM Proxy also owns its Anthropic Messages and Responses endpoints. The client chooses
one of the aliases above as its model. No client API key is required by default;
set `GATEWAY_MASTER_KEY` before launch to require one.

The router has no retries and no fallbacks. A selected channel succeeds or
fails visibly.

## Local model switcher

The official LiteLLM dashboard remains unchanged at `/ui`. Open
`http://127.0.0.1:4000/m` or its equivalent `/s` alias for the small local
routing control plane. It reads selectable physical models from
`config/litellm.yaml`, displays their declared capability tags, and applies a
change only to requests that begin after the policy update.

The routing modes are:

- `alias`, the safe default, resolves `current`, `default`, and `auto`
- `force`, explicitly replaces the model on every new inference request
- `off`, bypasses dynamic routing and preserves LiteLLM's normal behavior

State is validated against the selectable model list, kept as an in-memory
snapshot for the data path, and atomically persisted to
`config/active_model.json`. This local runtime file is ignored by Git and is
created automatically on first launch. The process records the requested
model, resolved model, mode, and policy version in request metadata whenever
it rewrites a request. This launcher uses one LiteLLM process. A future
multi-worker launch must replace the process-local snapshot with coordinated
shared state.

The control routes accept only loopback clients. State changes require a
same-origin JSON `POST` to `/api/active-model`. Command-line callers may omit
the browser `Origin` header:

```powershell
curl.exe -X POST http://127.0.0.1:4000/api/active-model `
  -H "Content-Type: application/json" `
  -d '{"model":"minimax","mode":"alias"}'
```

Capability tags are decision support, not a compatibility guarantee.
`drop_params` removes known unsupported top-level parameters, but silently
deleting parameters cannot make tool, vision, context-window, reasoning, or
streaming semantics interchangeable across providers.

## Advisor mode

Requests sent to the Anthropic Messages endpoint with model
`minimax-guided` or `openai-guided` receive LiteLLM's `advisor_20260301` tool
automatically. LiteLLM's built-in `AdvisorOrchestrationHandler` owns the loop:
it lets the executor request advice, calls `gemini-subscription`, injects the
result, and continues the executor. Existing client tools are preserved.

Configure the opt-in behavior before launch with:

```powershell
$env:ADVISOR_MODEL = "gemini-subscription"
$env:ADVISOR_TARGET_MODELS = "minimax-guided,openai-guided"
$env:ADVISOR_MAX_USES = "3"  # accepted range: 1..5
```

This automatic orchestration is currently enabled only on LiteLLM's
`/v1/messages` path. Chat Completions and Responses requests are deliberately
left unchanged because LiteLLM 1.100.1 does not run its built-in Advisor
interceptor on those paths.

## Subscription boundary

ChatGPT/Codex subscription authentication and the local Antigravity `agy`
session are not standard hosted providers. A deployment hook reloads
`.codex/auth.json` immediately before each Codex dispatch, while LiteLLM's
Responses bridge owns the protocol conversion. Antigravity is registered as
an in-process `CustomLLM`, so the gateway runs as one process and exposes only
port 4005.

The Antigravity provider deliberately rejects streaming, tools, and multimodal
content until those paths are supported and certified. It can serve simple text
requests through LiteLLM, but that alias is not yet a Codex or Claude Code
compatible channel. Only LiteLLM exposes the public protocol on port 4005.

## Verification

```powershell
py -3.14 -m pytest -q
```

These tests verify the ownership boundary and fail-closed routing config. They
do not claim provider dispatch or coding-tool compatibility. That requires the
existing LeanRouter fixture suite plus live isolated Codex and Claude Code runs
against staging port 4005.
