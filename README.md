<p align="center"><img src="site/assets/subroute-mark.svg" alt="Subroute: one local gateway with explicit model paths" width="420"></p>

# Subroute

[Official site](https://stancsz.github.io/subroute/) · [Market position and maturity](docs/market-position.md) · [Unpublished research draft (PDF)](paper/main.pdf) · [LaTeX source](paper/main.tex) · [Product and research values](#product-and-research-values)

Subroute gives coding clients one local LiteLLM endpoint. Use `default` to
follow the model selected at [the route desk](http://127.0.0.1:4000/control).
An explicit model name uses its configured LiteLLM route.

Subroute maintains Codex and Antigravity subscription adapters. Configure
OpenAI, Gemini, and other API routes through LiteLLM's `model_list`.

## Start the gateway

```powershell
docker compose up -d --build
```

The default stack runs the gateway and PostgreSQL. Start the Antigravity
subscription bridge when a route needs it.

```powershell
docker compose --profile antigravity up -d antigravity
```

Codex uses the mounted `~/.codex/auth.json`. Use `http://127.0.0.1:4000/v1`
for OpenAI-compatible clients, or `http://127.0.0.1:4000` for Anthropic
clients that append `/v1/messages`. The keyless listeners bind to loopback.

## Configure API models

Add native LiteLLM deployments to [config/litellm.yaml](config/litellm.yaml)
then rebuild the gateway image. Compose passes `OPENAI_API_KEY` and
`GEMINI_API_KEY` for API routes. Add other provider variables to your
deployment configuration. LiteLLM owns timeouts, retries, fallbacks and
PostgreSQL spend logs. Select `auto` to use the configured cross-provider
fallback chain.

LiteLLM records attributed calls in PostgreSQL. Clients can send a key or
an end-user ID for spend attribution and read token usage from API responses.

## Start staging

```powershell
docker compose --profile staging up -d --build gateway-staging
```

Staging listens on `4005`. The gateways share one PostgreSQL server with
separate `litellm` and `litellm_staging` databases and routing files.
See [the migration procedure](docs/operations/shared-postgres.md).

## Compatibility

LiteLLM serves Chat Completions, Responses, and Messages. Subroute retains
the stream and schema repairs in the [client contract tests](tests/test_client_stream_contracts.py).
The Antigravity CLI bridge buffers its output.
Choose `codex-luna` for Codex subscription images through Images or an
explicit hosted image tool. Send Antigravity audio to `gemini-subscription`
with Chat Completions. Optional [client tools](clients/README.md) run
separately from the gateway.

Select a Codex subscription model for Codex-specific Responses tools.

## 简体中文

Subroute 提供一个本地模型入口。在 `/control` 切换模型，客户端用 `default`
跟随选中的路由。Subroute 支持 Codex 和 Antigravity 订阅。API 模型在 LiteLLM
的 `model_list` 配置。Production 与 staging 共用 PostgreSQL，各用独立数据库
和路由状态。

## Product and research values

Subroute gives coding clients one local LiteLLM endpoint. It maintains Codex and Antigravity subscription adapters; LiteLLM owns configured API routes, fallbacks, retries, and usage reporting.

- **One owner per behavior:** LiteLLM handles provider protocols; the local route desk owns saved default selection.
- **Explicit route choice:** a named model uses its configured route; the default alias follows the saved selection.
- **State separation:** production and staging mutable state are separate; separate ports alone do not prove isolation.
- **Evidence before guarantees:** no controlled performance, cost, reliability, or security evaluation is claimed.

[Unpublished research draft (PDF)](paper/main.pdf) · [LaTeX source](paper/main.tex). Not submitted to arXiv; no identifier is claimed.
