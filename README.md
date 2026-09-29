<div align="center">
  <h1>Subroute</h1>
  <h3>Keep your coding tools steady. Choose your AI route in one place.</h3>
  <p>A local AI gateway and desktop control desk for developers who work across AI subscriptions, API providers, and local models.</p>
  <p><a href="#english">English</a> · <a href="#简体中文">简体中文</a></p>
</div>

<p align="center">
  <a href="artifacts/subroute-electron.png"><img src="artifacts/subroute-electron.png" alt="Subroute Electron desktop control desk showing model routing and coding-agent launch" width="100%"></a>
</p>
<p align="center"><sub>Electron desktop · model routing and agent launch / Electron 桌面端 · 模型路由与 Agent 启动</sub></p>

<a id="english"></a>

## The problem

Your coding tools remember an endpoint and model. Your AI provider, account, or preferred route can change. Updating every client each time is tedious and makes it harder to know which connection will handle the next request.

**Subroute gives compatible clients one local endpoint. Set the model to `auto` for prioritized automatic routing, or `current` to use the route selected centrally.**

## What you get

| | Value in your workflow |
| --- | --- |
| **One steady endpoint** | Keep compatible coding clients pointed at `http://127.0.0.1:4000/v1` while you change the selected route. |
| **A route you can see and change** | Choose a configured provider and model from the shared browser or desktop control desk. |
| **Clear connection status** | See which sources are configured, need setup, or report usage. Missing quota data stays unavailable instead of being guessed. |
| **Local-first control** | Run the gateway on your machine with Docker Compose. Electron adds a native shell and optional coding-agent launch helpers. |

## How it works

```text
Your coding tools
       │  OpenAI-compatible API · model: auto
       ▼
Subroute on 127.0.0.1:4000
       │  MiniMax M3 → Gemini 3.8 Flash → Codex Luna
       ▼
First provider to complete the request
```

LiteLLM handles the public API protocols, streaming, and standard provider adapters. Subroute adds the local routing policy, configured subscription integrations, provider status, and control desk. Electron uses the same control desk as `/control`; it does not run the gateway.

## Get started

Make credentials available as environment variables for the providers you plan to use, then start the production gateway from the repository root:

```powershell
docker compose up -d gateway
```

Open the control desk at [http://127.0.0.1:4000/control](http://127.0.0.1:4000/control), choose a configured route, then point a compatible coding tool to:

```text
Base URL: http://127.0.0.1:4000/v1
Model:    auto
```

`auto` tries MiniMax M3 first, then Gemini 3.8 Flash through the subscription integration, then Codex Luna through the subscription integration if the preceding provider returns an error. This is request-time fallback, so a failed upstream attempt may still incur provider cost. Use `current` to follow the route selected in the control desk, or select a specific model alias to keep a fixed route. Available models are listed in this checkout's [`config/litellm.yaml`](config/litellm.yaml).

### Use the Electron app

With the gateway running, launch the desktop control desk:

```powershell
cd desktop
npm install
npm start
```

On first launch, connect it to a responding loopback Subroute gateway. Desktop stores the gateway address locally, not provider API keys. Its optional agent launcher starts supported coding CLIs with session-only Subroute settings. See [desktop/README.md](desktop/README.md).

### Keep production and staging separate

Compose exposes production on `127.0.0.1:4000` and staging on `127.0.0.1:4005`. Staging has its own database and routing policy, while model/provider configuration is shared:

```powershell
docker compose up -d gateway-staging
```

## Routing, without surprises

- **`alias`** is the default. It resolves `current` and `default` using the saved route. `auto` always uses the prioritized provider chain.
- **`force`** applies the selected route to known model requests except explicit `auto` requests.
- **`off`** leaves model dispatch to LiteLLM.
- **Advisor** is selected independently. It enables consultation on the Anthropic Messages API; GPT-6 Luna is available alongside the other configured advisors. `No advisor` disables it.

Subroute configures no automatic retries. Only requests explicitly using `auto` fall back across providers, in the documented order. Requests using `current` or a specific model fail visibly if that route fails.

## Know the limits

A stable endpoint does not make providers interchangeable. Tool use, streaming, vision, context size, reasoning options, and subscription access depend on each route. The Antigravity Gemini subscription integration supports text and schema-constrained tool calls. The sidecar sends prompts through the CLI's NDJSON stdin protocol, so long prompts do not consume OS command-line argument space. The client executes returned tool calls; the subscription sidecar does not execute them. In tool mode, bounded plain-text commentary AGY appends after its schema result is ignored, while additional structured results are rejected. It does not support vision. Stream requests are returned as SSE after the CLI has completed the response, so tokens are not progressively streamed.

The Codex subscription endpoint rejects client output-token caps. Subroute omits `max_tokens` and `max_output_tokens` on that provider route, so the Codex backend chooses its own output limit. Gemini Subscription also uses backend-managed output limits: its CLI exposes no output-token cap, so client caps are not enforced. This applies to buffered and SSE responses, including Anthropic Messages. Subroute preserves the completed response and actual provider usage rather than truncating output to imply that a cap was honored. Tool schemas must be self-contained; remote schema references are rejected before provider dispatch.

The gateway binds to loopback by default. Keep it on your machine unless you intentionally configure and secure a different network boundary.

## Project notes

- [Architecture and ownership](docs/misc/architecture.md)
- [Product direction](docs/northstar/README.md)
- [Gateway configuration](config/litellm.yaml)
- [Desktop app details](desktop/README.md)

---

<a id="简体中文"></a>

## 简体中文

<div align="center">
  <h2>让编码工具保持不变，在一个地方切换 AI 路由。</h2>
  <p>Subroute 是面向开发者的本地 AI 网关与桌面控制台，支持将已配置的 AI 订阅、API 服务和本地模型接入同一套编码工作流。</p>
</div>

### 要解决的问题

编码工具会记住 API 地址和模型，而你使用的 AI 服务、账号或首选模型可能会变化。每次切换都要逐个修改客户端，既麻烦，也不容易确认下一次请求会发给哪个服务。

**Subroute 为兼容的客户端提供一个本地固定入口。使用 `auto` 按优先级自动路由，或使用 `current` 跟随控制台中选择的路由。**

### 它能带来什么

| | 工作中的实际价值 |
| --- | --- |
| **一个稳定入口** | 兼容的编码工具始终连接 `http://127.0.0.1:4000/v1`，切换服务时无需逐个修改客户端。 |
| **路由清晰可控** | 在浏览器或桌面控制台中，选择已经配置好的服务和模型。 |
| **连接状态可见** | 查看哪些服务已配置、需要设置，或能提供用量数据。服务未提供的额度不会被猜测或伪装成可用。 |
| **本地优先** | 使用 Docker Compose 在本机运行网关。Electron 桌面端提供原生界面和可选的编码 Agent 启动功能。 |

### 工作方式

```text
你的编码工具
       │  OpenAI 兼容 API · 模型：auto
       ▼
Subroute（127.0.0.1:4000）
       │  MiniMax M3 → Gemini 3.8 Flash → Codex Luna
       ▼
第一个成功完成请求的服务
```

LiteLLM 负责公开 API 协议、流式传输和标准服务适配。Subroute 负责本地路由策略、已配置的订阅集成、服务状态和控制台。Electron 与网页 `/control` 使用同一套控制界面；网关本身由独立的 Compose 服务运行。

### 快速开始

先为要使用的服务准备环境变量凭据，再在仓库根目录启动生产网关：

```powershell
docker compose up -d gateway
```

打开[路由控制台](http://127.0.0.1:4000/control)，选择已经配置好的路由，再将兼容的编码工具指向：

```text
API 地址： http://127.0.0.1:4000/v1
模型：     auto
```

`auto` 首先尝试 MiniMax M3；若服务返回错误，则依次尝试 Gemini 3.8 Flash 订阅和 Codex Luna 订阅。失败的上游尝试仍可能产生费用。使用 `current` 跟随控制台选择的路由，或选择具体模型别名以固定路由。当前模型见本仓库的 [`config/litellm.yaml`](config/litellm.yaml)。

### 使用 Electron 桌面端

先启动网关，再运行桌面控制台：

```powershell
cd desktop
npm install
npm start
```

首次启动时，将桌面端连接到有响应的本地 Subroute 网关。桌面端只在本地保存网关地址，不保存服务 API 密钥。可选的 Agent 启动器会使用仅对本次会话生效的 Subroute 配置来启动受支持的编码 CLI。详见 [desktop/README.md](desktop/README.md)。

### 隔离生产与预发布环境

Compose 将生产环境绑定到 `127.0.0.1:4000`，预发布环境绑定到 `127.0.0.1:4005`。两者使用独立的数据库和路由策略，但共享模型与服务配置：

```powershell
docker compose up -d gateway-staging
```

### 路由行为

- **`alias`** 是默认模式，根据已保存的路由解析 `current` 和 `default`。`auto` 始终使用按优先级排列的服务链。
- **`force`** 将所选路由应用到已知模型的推理请求，显式使用 `auto` 的请求除外。
- **`off`** 将模型分发交由 LiteLLM 处理。
- **Advisor（顾问模型）** 独立选择。选择顾问后，Anthropic Messages API 请求会启用咨询；GPT-6 Luna 可与其他已配置顾问一样选择，选择 `No advisor` 则关闭。

Subroute 不配置自动重试。只有明确使用 `auto` 的请求才会按文档顺序尝试备用服务。使用 `current` 或具体模型的请求在该路由失败时会明确报错。

### 使用边界

统一入口不代表各服务能力相同。工具调用、流式传输、图像输入、上下文长度、推理选项和订阅访问能力都取决于具体路由。Antigravity Gemini 订阅集成支持文本和受 schema 约束的工具调用。Sidecar 通过 CLI 的 NDJSON stdin 协议传递 prompt，避免长 prompt 占用操作系统命令行参数空间。工具调用由客户端执行，订阅 sidecar 不会代为执行；工具模式下 AGY 在 schema 结果后追加的有限纯文本说明会被忽略，额外的结构化结果会被拒绝；该路由不支持图像。流式请求会在 CLI 完成响应后通过 SSE 返回，不会逐 token 输出。Codex 订阅端点不接受调用方的输出 token 上限，Subroute 会在该 provider 路由中省略 `max_tokens` 和 `max_output_tokens`，由 Codex 后端选择输出上限。请根据实际选择的路由确认其能力。

Gemini 订阅同样由后端管理输出上限：CLI 不提供输出 token 上限参数，因此不会执行调用方设置的上限。这适用于普通响应、SSE 和 Anthropic Messages。Subroute 保留完整响应和真实 provider usage，不通过截断输出伪装成遵守了上限。工具 schema 必须自包含；远程 schema 引用会在 provider 调用前被拒绝。

网关默认只监听本机回环地址。除非你主动配置并保护其他网络边界，否则请将其保留在本机使用。

### 项目文档

- [架构与职责边界](docs/misc/architecture.md)
- [产品方向](docs/northstar/README.md)
- [网关配置](config/litellm.yaml)
- [桌面端说明](desktop/README.md)
