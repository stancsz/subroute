# Subroute

> Route the AI subscriptions you already have to the coding tools you already use.

Subroute is a local subscription router for coding tools. Configure Cursor,
Claude Code, Aider, Windsurf, or another compatible client once with a local
endpoint, then select the model and routing policy locally instead of
reconfiguring every client.

Subroute is deliberately built on LiteLLM Proxy, not as a replacement for it.
LiteLLM owns the public OpenAI and Anthropic protocols, streaming, tool events,
request translation, and standard provider adapters. Subroute adds the local
control plane around subscription-backed and other personal AI channels:

- refresh local Codex subscription credentials immediately before dispatch;
- expose a stable `current` model alias to every coding client;
- persist and audit explicit model-routing policy; and
- keep production and staging policy state isolated.

It does not promise that all providers or subscriptions have identical tool,
vision, context, reasoning, or streaming behavior. Unsupported inputs fail
visibly rather than being silently rewritten into a different request.

## Runtime shape

```text
Codex / Claude Code / other coding tools
                  |
                  v
              Subroute on 127.0.0.1:4000
                  |
     LiteLLM protocol and provider layer
                  |
 OpenAI / Gemini / MiniMax / OpenRouter / Ollama / in-process Antigravity
```

## What problem it solves

An AI subscription normally lives inside its own desktop app or CLI, while
coding tools expect a stable API endpoint and model identifier. Subroute is
the small local layer that bridges that mismatch. A client can always request
`current`; you choose which configured channel receives new requests from the
local switcher at `/control`.

The checked-in configuration exposes one virtual alias and eighteen physical model
aliases:

| Alias | LiteLLM transport |
| --- | --- |
| `current` | Virtual entry resolved by the dynamic routing callback |
| `openai` | Native `openai/` provider |
| `openai-guided` | OpenAI executor with automatic Advisor injection |
| `gemini-api` | Native `gemini/` provider |
| `openrouter` | Native `openrouter/` provider, defaulting to `minimax/minimax-m3` |
| `openrouter-guided` | OpenRouter MiniMax M3 with automatic Advisor injection |
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
Use `docker compose up -d gateway-staging` for isolated staging on port 4005.
Changing only the host launcher's port does not isolate its state or database;
host staging also requires separate `ACTIVE_MODEL_STATE_PATH` and `DATABASE_URL`.
LiteLLM Proxy also owns its Anthropic Messages and Responses endpoints. The client chooses
one of the aliases above as its model. No client API key is required by default;
set `GATEWAY_MASTER_KEY` before launch to require one.

## Expert consultation API

`experts` is a separate, loopback-only LiteLLM service for compact advisory
questions. Start it independently when a Luna worker needs a second opinion:

```powershell
docker compose up -d experts
```

It listens at `http://127.0.0.1:4040/v1` and exposes only these non-streaming,
text-only aliases: `codex-sol-advisor` and `codex-astra-advisor`. It deliberately
does not expose `current`, Luna, a control UI, dynamic routing, fallbacks, or
automatic advisor injection. It shares the existing read-only Codex subscription
credential mount because the two advisor aliases use the subscription bridge;
it has no database or mutable policy state.

Keep port 4000 for normal worker execution. Use port 4040 only for a compact
diagnostic packet and treat its answer as advice for the worker to verify. Set
`EXPERTS_API_KEY` before starting the service if a local bearer token is needed;
otherwise its loopback binding is the access boundary.

The router has no retries and no fallbacks. A selected channel succeeds or
fails visibly.

The packaged [Luna advisor skill](skills/luna-advisor-escalation/SKILL.md) can
optionally let the expert request independent Pi source reading. Pi uses the
existing worker gateway at `http://localhost:4000/v1`, not direct provider
credentials. It summarizes approved, read-only source evidence with validated
citations. The task budget is at most 3 expert calls and 3 Pi tasks, with early
stopping and separate usage receipts. See the [reader setup and limits](skills/luna-advisor-escalation/references/reader.md).

## Local model switcher

The official LiteLLM dashboard remains unchanged at `/ui`. Open
`http://127.0.0.1:4000/control` for the small local
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
`minimax-guided`, `openrouter-guided`, or `openai-guided` receive LiteLLM's `advisor_20260301` tool
automatically. LiteLLM's built-in `AdvisorOrchestrationHandler` owns the loop:
it lets the executor request advice, calls the selected advisor, injects the
result, and continues the executor. Existing client tools are preserved.

Codex advisors translate prior Anthropic `tool_use`/`tool_result` blocks and
OpenAI Chat `tool_calls`/`tool` messages into Responses `function_call` and
`function_call_output` items. Malformed, unmatched, or non-text tool history
does not fail the executor request: advisor injection is skipped and the reason
is recorded in request metadata. The original messages and client tools are
never rewritten by this compatibility check.

Configure the opt-in behavior before launch with:

```powershell
$env:ADVISOR_MODEL = "gemini-subscription"
$env:ADVISOR_TARGET_MODELS = "minimax-guided,openrouter-guided,openai-guided"
$env:ADVISOR_MAX_USES = "3"  # accepted range: 1..5
```

This automatic orchestration is currently enabled only on LiteLLM's
`/v1/messages` path. Chat Completions and Responses requests are deliberately
left unchanged; this deployment enables the built-in Advisor interceptor only
on the Messages path.

The persisted advisor selection is authoritative. `ADVISOR_MODEL` supplies the
initial value only when no selection has been saved. Each request captures one
policy snapshot for both executor resolution and advisor injection.

Docker uses the pinned image digest in Compose (LiteLLM 1.103.0, Python 3.13).
The optional Windows environment uses Python 3.11 and pinned LiteLLM 1.101.0,
because the Docker build's 1.103.0 package is unavailable from the configured
package index. Both Python versions are within the supported 3.11 through 3.13
range. Run integration tests against the pinned Docker image before release;
local tests alone do not establish deployed behavior.

Staging has a separate PostgreSQL service, database volume, and policy volume.
Its policy is stored at `/app/state/active_model.json`; production retains
`/app/config/active_model.json` to preserve the existing selection. Staging mounts
the shared configuration read-only. Source and static configuration are still
shared, so changes to them must be reviewed before restarting production.

## Subscription boundary

ChatGPT/Codex subscription authentication and the local Antigravity `agy`
session are not standard hosted providers. A deployment hook reloads
`.codex/auth.json` immediately before each Codex dispatch, while LiteLLM's
Responses bridge owns the protocol conversion. Antigravity is registered as
an in-process `CustomLLM` handler that sends each request to the private
Antigravity service. That service runs the `agy` subprocess with a bounded
timeout.

The Antigravity provider deliberately rejects streaming, tools, and multimodal
content until those paths are supported and certified. It can serve simple text
requests through LiteLLM, but that alias is not yet a Codex or Claude Code
compatible channel. The CLI and authenticated session must be available inside
the selected runtime.

Compose runs Antigravity in its own internal `antigravity` service. The pinned
LiteLLM gateway image does not contain the CLI or its credentials; it sends
bounded text-only requests to that service on the private Compose network. The
service keeps its Linux configuration and secure-session data in named Docker
volumes and publishes no host port.

On Windows, an existing host Antigravity session can be migrated once into the
container's file-backed credential store. Read the raw UTF-8 credential blob
from Windows Credential Manager and stream it to
`/root/.config/agy-host-auth` over stdin. On the next service start, the
entrypoint moves it into the private `antigravity-gemini` volume with mode 600.
The credential is never written to the repository, Compose environment, image,
or command line. If migration is not available, use the interactive OAuth flow
below.

After the first `docker compose up -d --build`, authenticate that container
once. The forced SSH environment makes the CLI print a browser authorization
URL and accept the returned code without attempting to launch a browser inside
Docker:

```powershell
docker compose exec -e SSH_CONNECTION=container -it antigravity agy
```

Use the prompted URL and code with the approved Google account. The session is
container-local and survives a restart through the named volumes. Confirm it
without sending a model request:

```powershell
docker compose exec antigravity agy models
```

Only after that succeeds should `gemini-subscription` be selected as the
advisor in `/control`.

The Codex advisor retains a narrow text collector because the standard
`openai/responses/` bridge preserves the advisor's `stream=False` request,
whereas this subscription transport requires `stream=True`. The collector
returns only after a validated completed event and provider usage; failed,
incomplete, truncated, or malformed streams fail visibly. Unsupported non-text
and tool inputs are rejected. Retire this collector when the standard bridge
can force upstream streaming while returning a validated completed advisor
response. Credential reload is scoped to the Codex deployment URL, and system
instructions map to developer messages only at that boundary, never to user
text. LiteLLM
also maps Claude Code's required `max_tokens` field to `max_output_tokens`.
The subscription backend rejects that parameter, so the boundary removes it
for Codex subscription dispatches and lets the provider choose its output
length. Other Responses deployments keep their requested output limit.

When a Codex subscription target is paired with a Codex advisor in the routing
desk, the gateway first collects a completed streaming advisor response, then
adds it as developer guidance for the selected target. This is a narrow
compatibility path: LiteLLM's native advisor orchestration uses a non-streaming
internal base call, while the Codex subscription backend requires streaming.
The request fails closed if the advisor cannot complete, and the gateway log
records an `advice_injected` consultation ID with provider usage.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest -q
```

These tests verify the ownership boundary and fail-closed routing config. They
do not claim provider dispatch or coding-tool compatibility. That requires the
existing LeanRouter fixture suite plus live isolated Codex and Claude Code runs
against staging port 4005.
