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
       LiteLLM Proxy on 127.0.0.1:4005
                  |
        explicit model_name selection
                  |
 OpenAI / Gemini / MiniMax / Ollama / in-process Antigravity
```

The checked-in configuration exposes nine explicit model aliases:

| Alias | LiteLLM transport |
| --- | --- |
| `openai` | Native `openai/` provider |
| `openai-guided` | OpenAI executor with automatic Advisor injection |
| `gemini-api` | Native `gemini/` provider |
| `minimax` | Native `minimax/` provider |
| `minimax-guided` | MiniMax executor with automatic Advisor injection |
| `freetoken` | Native OpenAI-compatible transport |
| `desktop` | Native `ollama/` provider |
| `codex-subscription` | LiteLLM Responses bridge plus per-request Codex credential refresh |
| `gemini-subscription` | In-process LiteLLM `CustomLLM` provider for `agy` |

Edit the model IDs in `config/litellm.yaml` to match the models available to
the account. Add another `model_list` deployment when a channel needs another
key. Do not add a Python adapter for a provider LiteLLM already supports.

## Install and run

```powershell
py -3.14 -m pip install -e ".[test]"

$env:OPENAI_API_KEY = "..."
$env:GEMINI_API_KEY = "..."
$env:MINIMAX_API_KEY = "..."
./scripts/start-gateway.ps1
```

FreeToken defaults to `http://127.0.0.1:1919/v1` with a placeholder local key.
The desktop alias uses LiteLLM's native `ollama/qwen2.5-coder` provider and
defaults to `http://127.0.0.1:11434`. Override `OLLAMA_API_BASE` when needed.

Point OpenAI-compatible tools at `http://127.0.0.1:4005/v1`. LiteLLM Proxy
also owns its Anthropic Messages and Responses endpoints. The client chooses
one of the aliases above as its model. The local default API key is
`sk-gateway-local-dev`; set `GATEWAY_MASTER_KEY` before launch to override it.

The router has no retries and no fallbacks. A selected channel succeeds or
fails visibly.

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
