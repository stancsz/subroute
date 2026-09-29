# Subroute 核心目标与技术规格 (GOAL.md)

本文档定义了 Subroute 的核心目标、架构设计边界、功能规范及验收标准。

---

## 一、 核心目标 (Core Goals)

1. **统一接入 OpenAI Subscription (ChatGPT / Codex 订阅)**：
   - 深度复用本地个人订阅凭证（读取 `~/.codex/auth.json` 中的 `access_token` 与 `account_id`）。
   - 请求发往 `https://chatgpt.com/backend-api/codex`。窄 `CustomLLM` adapter 仅负责 Codex 上游强制流式和非流式结果组装；公开 Chat、Responses、Messages 协议仍由 LiteLLM 原生桥接处理。
   - 每次 dispatch 前热加载 Codex 凭据文件中新写入的 token/account，不需要手动配置官方付费 `OPENAI_API_KEY`。验收仍要求凭据所有者在 JWT 过期前自动刷新且后续请求不中断；重新读取文件本身不等于刷新 token。
   - Codex 订阅端点拒绝 `max_tokens` / `max_output_tokens`。经用户 2026-09-28 明确选择，适配器在此 provider 边界省略这些字段，由 Codex 后端采用自己的输出上限；这不保留调用方请求的 token cap，作为已知协议限制记录。

2. **规范化网关端口：4000 作为生产环境 (Production)**：
   - **4000 (Prod 生产端口)**：对外统一服务端口，服务根路径 `http://127.0.0.1:4000`，API 入口 `http://127.0.0.1:4000/v1`。所有日常开发的主力 IDE 与工具（Cursor, Claude Code, Aider, Windsurf 等）统一且仅配置该端口。
   - **4005 (Staging 测试端口)**：作为预发布与集成回归演练环境，保障新 Provider、插件升级或配置变更时具备独立的隔离沙箱，不影响 4000 生产流量。
   - 路由策略、控制面与 Codex 适配器由 LiteLLM 网关进程承载。Antigravity 保留个人 CLI 订阅认证；经用户 2026-09-26 明确选择，不使用 Gemini API key，并允许由 Compose 管理的独立 Antigravity bridge sidecar 承载 CLI 会话。该 provider 是单进程原则的明确例外；production/staging 仍共用当前 Antigravity sidecar 状态卷。

3. **控制面快速切模型：下拉菜单动态指定当前模型**：
   - 提供直观极简的网页控制台（生产环境 **`http://127.0.0.1:4000/control`**）。
   - 页面内置下拉菜单（Dropdown），可一键选取当前生效的目标模型。
   - 遵循架构准则：**“配置提供候选集，控制面保存路由策略，数据面在请求入口解析模型别名”**。

---

## 二、 架构设计与实现规范

### 1. 动静分离的路由模型
* **IDE 侧契约**：客户端模型名称统一固定填 **`current`**（兼容别名：`default`, `auto`）。IDE 仅配置一次，后续永远无需重新打开设置修改模型。
* **策略模式（State Modes）**：
  * **`alias` 模式（默认推荐）**：仅重写模型名为 `current` / `default` 的请求，精准转发到控制面选中的目标物理模型；客户端明确指定特定模型（如指定请求 `desktop`）时不予干预，契约清晰，易于审计。
  * **`force` 模式（显式全局覆盖）**：控制面勾选后开启，无论客户端请求何种模型，一律强制改写为当前选中的物理模型。
  * **`off` 模式（旁路直通）**：关闭动态路由，完全回退至 LiteLLM 原生静态分发。

### 2. 控制面 (Control Plane) 规范
* **候选集过滤与防环路**：
  - 控制面自动从 `config/litellm.yaml` 的 `model_list` 读取可路由的模型，自动排除 `current`、`default` 等虚拟别名，杜绝死循环递归。
* **原子写入与读写分离**：
  - 控制面修改策略后，通过 `tempfile -> flush -> os.replace` 原子写入持久化状态文件 `config/active_model.json`，防止进程崩溃或并发写入导致文件损坏。
  - 数据面运行时使用内存快照读取当前状态，杜绝每次推理请求都读磁盘 I/O。
* **最小安全边界**：
  - 严格限制本地回环监听 (`127.0.0.1`)。
  - API 接口 (`POST /api/active-model`) 实施严格的模型白名单比对，阻断非法模型名及路径注入。

### 3. 数据面 (Data Plane) 规范
* **显式路由解析与审计追踪**：
  - 在请求入口 Hook (`async_pre_call_hook`) 中执行路由解析：
    ```python
    requested_model = data.get("model")
    resolved_model = resolve_model(requested_model, routing_state)
    data["model"] = resolved_model
    ```
  - 将 `requested_model`、`resolved_model`、`routing_mode`、`policy_version` 写入请求元数据与调试日志，保证全链路可观测，杜绝“改写后查不到原始意图”的隐式黑盒。
* **异构参数与能力防御**：
  - 启用 `drop_params: true` 静默剔除非通用参数。
  - 控制面提供目标模型的能力指示（如是否支持 Tools、上下文容量），避免盲目强转导致请求失败。

---

## 三、 实施阶段与任务拆解 (Milestones)

### 阶段 1：端口与基础环境规范化 (Port 4000 Baseline)
- [x] 将网关启动脚本 `scripts/start-gateway.ps1` 默认端口由 4005 调整为 **`4005 -> 4000`**；配置测试断言默认值。
- [x] 确保测试套件 `tests/test_litellm_config.py` 断言 Compose 生产 `127.0.0.1:4000` 与 staging `127.0.0.1:4005` 端口映射；启动脚本默认 host/port 也有断言。
- [x] 明确 Antigravity 进程/auth 边界：保留个人 CLI 订阅，不配置 Gemini API key，并接受 Compose-managed sidecar 作为该 provider 的显式例外。2026-09-26 用户授权此方向；公网 SDK 没有证明可在网关进程内复用个人 CLI 会话。Compose 已提供 bridge、健康检查和三个持久状态卷；只读运行检查显示 sidecar healthy。Production 与 staging 共用这些卷，因此不能宣称 Antigravity 状态已隔离。镜像依赖固定及尚未执行 ARM64 CLI 的限制见 `sidecars/antigravity/Dockerfile` 与 `docs/evals/badlands-protocol-2026-09-25.md`。
- [x] 更新 `README.md` 与 `docs/misc/architecture.md` 中对应的端口与架构说明。

### 阶段 2：OpenAI Subscription 通道与凭据守护
- [x] 验证 `config/litellm.yaml` 中 `codex-subscription` 部署与 LiteLLM 公共协议桥接配置；CustomLLM 与 Chat/Responses/Messages HTTP fixtures 已覆盖，两种锁定/运行 LiteLLM 版本均通过。
- [x] 确保 `codex_credentials.py` 部署钩子在每次请求前重新读取 `~/.codex/auth.json`。当 Codex CLI 的有效凭据存储为文件时，文件更新无需重启网关即可继承；OAuth 刷新仍由 Codex 凭据所有者负责。
- [x] 编写针对 Codex 凭据热加载逻辑的自动化单元测试；fixture 在两次 dispatch 间替换 token/account 并确认使用新值，空值、非字符串和错误文件结构在凭据加载边界失败关闭。
- [ ] 验证当前 Codex 凭据所有者在 JWT 过期前自动刷新并写回网关读取的 `auth.json`，随后真实 dispatch 使用新 token 且不中断。对本机 `codex-cli 0.154.0` 做了隔离验证：临时 `CODEX_HOME` 使用合成凭据，近过期 JWT 通过 `CODEX_REFRESH_TOKEN_URL_OVERRIDE` 指向 `127.0.0.1` stub；`codex plugin remove` 触发恰好一次本地 refresh，CLI 将 stub 返回的新 access/refresh token 与 `last_refresh` 写回文件，Subroute 的 `read_codex_credentials()` 随后读到新 access token 和 account id。新增 LiteLLM proxy HTTP 回归用临时 `auth.json` 连续替换两组 token/account，经真实 proxy dispatch hooks 确认两次 stubbed upstream 请求分别携带新值。上述检查仅用合成凭据和 stubbed provider；没有使用真实凭据或调用模型，不证明真实 OAuth 刷新和之后真实 dispatch 连续性。另有官方过期前刷新/文件存储测试；该版本默认存储为 `file`，且检查的用户配置、`CODEX_HOME` 与 requirements 未发现覆盖。详见 `docs/evals/badlands-protocol-2026-09-25.md`。

### 阶段 3：动态路由与控制面实现 (Control Plane & Web UI)
- [x] 编写 `src/subroute/plugins/dynamic_router.py`：
  - 挂载 `/control` 极简控制台页面（包含现代轻量下拉菜单与生效状态提示）。
  - 实现原子状态存储管理（支持 `alias`、`force` 模式切换）。
  - 实现 `async_pre_call_hook` 路由改写与元数据打标。
- [x] 在 `config/litellm.yaml` 中增加 `current` 虚拟别名声明并挂载该回调插件。
- [x] 编写单元和代理层测试，覆盖别名映射、强制模式、原子读写、白名单拦截、Advisor 策略和同一策略快照；通过 `/api/active-model` 保存临时控制面中的 `codex-terra` 选择，再经 LiteLLM 公共 `/v1/chat/completions` 路由发送 `model: current`，确认上游 dispatch 与请求路由元数据一致（本地和运行镜像 LiteLLM 1.103.0 的 stub 验证，不替代真实模型生成验收）。

### 阶段 4：集成验证与文档交付
- [x] 运行完整测试套件（`uv run --extra test --locked pytest -q`：199 passed；LiteLLM 1.101.0；28 dependency/schema warnings）。
- [x] 对运行中的 4000/4005 只读检查 `/control`、脚本资源、`/api/active-model` 与 `/api/routing-options`，均返回 200；另以浏览器检查 4000 的 1278 × 910 页面并保存[截图证据](docs/evals/control-desk-live-2026-09-25.png)。该视图证明桌面浏览器布局与已保存策略可见，不证明请求生成或窄屏/Electron 验收。
- [ ] 验证在控制台切换路由后，向 `http://127.0.0.1:4000/v1` 发 `model: current` 请求能由所选模型生成，并核对路由元数据。2026-09-26 经用户授权，重启 production/staging 后，两者健康且加载 `codex-luna -> codex-subscription/gpt-6-luna`；策略仍分别为 production `force` v10、staging `force` v34。唯一获准的真实调用通过 staging Badlands API Gateway 完成：请求 `openai/gpt-6-luna`，响应 HTTP 200、`x-gateway-tier: local`、正文 `PONG`，表明路径到达本机 Subroute。该桥接调用返回的并非 `model: current`，且未从 Subroute 日志取得与外层响应 ID 对应的 `requested_model` / `resolved_model` / `policy_version` 元数据，因此不能据此勾选完整路由验收。Codex usage 未由外层响应报告。此前 `desktop` 保存检查仍是无推理检查；本次证据见 `docs/evals/badlands-protocol-2026-09-25.md`。
- [ ] 清理仓库内所有历史僵尸代码与废弃未提交变更。只有在配置、导入/调用引用和当前验收用途均证明路径已失效或被取代，且确认不属于用户保留的工作后才删除。2026-09-25 的当前引用扫描未发现满足该标准的源码候选：Codex advisor、订阅适配器、凭据插件、动态路由、Antigravity handler/bridge 都仍有配置或调用方；旧 LiteLLM bridge 测试仍记录新适配器替换前的具体协议缺口。当前活动改动与本 GOAL 的 Codex/路由/镜像工作或 control-desk 视觉验收直接相关。完整检查记录见 [评估](docs/evals/badlands-protocol-2026-09-25.md)。

---

## 四、 验收标准 (Acceptance Criteria)

1. **端口与环境验收**：
   - 默认运行 `start-gateway.ps1` 成功在 **Prod 生产端口 `127.0.0.1:4000`** 监听，所有生产开发工具仅连接该端口。
   - 运行 `start-gateway.ps1 -Port 4005` 可启动独立的 **Staging 测试沙箱**，用于验证新配置与测试套件。当前 Docker Compose 的两个 gateway 仍共用 Antigravity sidecar 与三个 CLI state volumes，因此 Antigravity 凭据、配置和缓存尚未隔离。
2. **订阅通道验收**：在存在 `~/.codex/auth.json` 的环境下，请求 `codex-subscription` 能够顺利通过 LiteLLM 协议桥接完成响应，并在凭据所有者自动刷新 Token 时无需重启网关。现有单元 fixture 证明 dispatch 重新读取 token/account 文件；合成凭据隔离实验也证明本机 Codex CLI 刷新写回的文件可由 Subroute 读取。真实账户 OAuth 刷新后持续完成 provider dispatch 尚未证明，故此验收仍未完成。
3. **控制面体验验收**：
   - 浏览器打开生产控制面 `http://127.0.0.1:4000/control` 呈现直观的下拉菜单。
   - 切换下拉项后，本地向 `http://127.0.0.1:4000/v1/chat/completions` 发送 `model: "current"` 请求，返回内容与元数据确认由所选模型生成。
4. **代码纯净度**：保留 GOAL 明确允许的 Antigravity bridge sidecar；移除经配置、调用引用及验收用途证实为废弃的代码；所有测试通过。Sidecar 属于用户授权的运行架构例外，不作为僵尸代码删除。其余清理仅在发现具体失效路径后进行；当前审计未发现可安全删除的源码候选。

## 补充范围：模型与推理强度选择

控制台分别保存目标模型与 Advisor 的推理强度，默认不覆盖客户端或提供商的设置。
候选值由 `config/litellm.yaml` 中各模型的 `reasoning_efforts` 定义，非法组合必须拒绝。
Gemini 订阅提供 Flash 3.8、3.7、3.6 和 Pro 3.1；Flash 支持 low/medium/high，
Pro 支持 low/high，并映射到现有 Antigravity 模型变体。该通道仍仅支持文本，
不新增流式或工具调用支持。目标强度仅随 alias/force 路由生效，Advisor 强度独立应用
于 Messages API 咨询。旧策略文件兼容默认值，单次请求使用同一策略快照。
验收需覆盖持久化、非法组合、真实 LiteLLM 转换、界面保存与订阅调用。

Gemini 订阅的 stream 请求以 SSE 兼容格式返回，但会先等待 CLI 完成整条回复；这不是逐步生成，
也不改变该路由不支持工具调用的限制。

## 补充范围：专家独立阅读

Advisor skill 可按专家提出的证据问题启动短生命周期 Pi 阅读任务，不新增常驻
gateway 服务或直接 OpenAI 连接。Pi 使用 `http://localhost:4000/v1`，专家仍走
4040。每个任务最多 3 次专家调用和 3 个 Pi 任务，证据充分即提前结束。Pi 在
授权源码快照中只读检索，返回简短摘要、可核对的源码引用与未知项；实现和运行
验证仍由主 worker 负责。分别记录专家与 Pi 的用量，不把独立阅读当作无偏保证、
运行验证或已证明的 token 节省。

Routing desk: Target and Advisor use Provider > Model > Reasoning effort. Show configured connections, distinguish subscription from API access, retain saved unavailable choices with a warning, and save provider changes only after model selection.

Advisor selection is the sole switch for automatic Messages API consultation: a selected advisor enables it, and No advisor disables it. Target models have no separate advisor-enabled variants. Preserve the saved advisor choice when migrating retired guided target aliases.

2026-09-26 advisor runtime check: the Minimax + Gemini 3.8 Flash pair initially failed because AGY’s headless CLI tried `run_command` (`ls -la /app`) with `request-review`, where no user could approve it. The bridge discarded the terminal `ERROR` result and returned only “no response text.” The bridge now launches a dedicated text-only agent in sandbox mode and accepts only a terminal `SUCCESS` with a non-empty response; terminal errors include their bounded reason. Minimax + Gemini Messages requests now pre-consult Gemini and inject its guidance before Minimax dispatch, since optional native advisor-tool selection did not invoke Gemini in the live Minimax trial. `uv run --extra test --locked pytest -q tests/test_advisor_plugin.py tests/test_antigravity_bridge.py` passes (55 tests). After rebuilding/restarting only `antigravity` and restarting `gateway`, a real `current` Messages request under saved policy `minimax` / `gemini-subscription` returned HTTP 200 with `stop_reason=end_turn`; correlated gateway evidence records `advice_injected`, provider usage 613/519 tokens, and `base_model=minimax`, while the bridge logged a successful Gemini 3.8 Flash Medium completion. This verifies the tested local production path, not staging or all model/advisor pairs.

2026-09-28 Gemini Subscription target check: saved policy is now `gemini-subscription` / `force` (policy version 28); `/api/routing-options` reports configured Subscription access with text, tools, and streaming, while `gemini-api` remains unconfigured. A live Anthropic Messages streaming request through the gateway selected model group `gemini-subscription` and returned HTTP 200, `stop_reason=tool_use`, tool `ping`, and input `{"text":"OK"}`, followed by `message_stop`. Two additional identical live tool requests also returned HTTP 200 and included `ping`; a separate automatic-tool request returned HTTP 200, `stop_reason=end_turn`, and `AUTO-OK`. The AGY CLI can append plain-text/schema wrapper material after its structured result; the handler now ignores bounded plain-text commentary and still rejects a distinct additional structured result. Focused bridge/handler/config tests pass (35 tests). This is a local Compose runtime check, not a staging check or proof for every Gemini model variant; streaming is buffered until AGY completes, and vision remains unsupported.

2026-09-28 Gemini long-prompt follow-up: retained logs later showed several sidecar `OSError: [Errno 7] Argument list too long` failures and one corresponding Gemini Subscription `/v1/messages` HTTP 500. The prompt had been passed as one `agy --print` argument. The bridge now uses AGY's documented `--input-format stream-json` NDJSON stdin protocol and writes tool schemas to a bounded-lifetime temporary JSON file; it returns a bounded 502 instead of dropping the connection if CLI process startup fails, and suppresses broken-pipe tracebacks when clients disconnect. After rebuilding the sidecar, an actual `current` Anthropic Messages request routed to Gemini Subscription with a 144,079-byte prompt returned HTTP 200, `stop_reason=end_turn`, and `STDIN-OK`. The original saved route (`codex-luna` / `force`) was restored after the probe. Focused bridge/handler/config tests pass (36 tests). This verifies the former OS-argument failure boundary on local Compose; older failure entries remain in retained logs, and this does not prove every Gemini model or prompt size.

## 维护性与可靠性跟进调查

以下事项来自 2026-09-28 对当前源码和本地 Compose 的审查。它们是待调查/修复项，不改变上文已经定义的产品合同或验收标准。完成每项前先确认实际失败机制和行为边界；保留用户工作，不以删功能或隐藏错误来缩小改动。

- [ ] 修复控制台 provider usage 查询可能阻塞网关事件循环的问题。`ui_control.py` 的 async route 同步调用 `read_provider_usage()`；该函数持有全局锁执行 Codex、MiniMax、OpenRouter 和 Gemini 的串行 HTTP 查询，单源 timeout 合计最高约 36 秒。调查并改为不阻塞事件循环的有界读取，避免网络 I/O 期间持有缓存锁。验收：首次查询及显式刷新期间，readiness 和独立生成请求仍能及时完成；超时来源清楚标记为 unavailable，缓存数据与各来源状态一致。
- [ ] 改善 Messages Advisor 失败的诊断，并明确失败策略。近期日志出现多次 `Subscription advisor consultation failed; base request was not sent` HTTP 500；当前包装错误没有在 gateway 日志中给出可关联的底层异常。另有 `NoneType.prompt_tokens` 的 Messages HTTP 500，根因尚未确认。为每次咨询记录 consultation ID、advisor、base model 与经过截断/脱敏的底层错误；调查两类 500 的根因。明确 Advisor 是必须成功还是可跳过，并只按经确认的产品策略处理；不得静默重试、调用备用 provider 或增加未授权的 provider 消耗。验收：故障可从一次请求日志追到根因，回归检查确认 base request 是否 dispatch 与所选策略一致。
- [ ] 为 Antigravity sidecar 的并发 CLI 工作设定明确上限，并验证取消清理。当前 `ThreadingHTTPServer` 为并发请求创建线程，每个请求都可启动一个有自身 timeout 的 CLI 进程；尚无全局并发上限，也未证明客户端断开会终止并回收子进程。增加有界并发和可靠的超时/断连清理。验收：超过上限时行为明确且不会启动额外 CLI；超时或断开后无残留子进程，正常请求仍可成功。
- [ ] 统一 Gemini Subscription 的功能合同。本文当前描述其为文本通道且不新增工具调用支持，stream 也只是等 CLI 完成后缓冲输出；配置、控制台和 handler 却声明/实现 tools 与 streaming，近期本地运行记录也包含 `tool_use` 成功响应。调查目标工具调用和 stream 行为是否为必要能力，再让 GOAL、LiteLLM capabilities、控制台提示、实现和验收保持一致。不要在调查中静默删除客户端可见能力或宣称缓冲 SSE 是逐 token streaming。
- [ ] 评估 Antigravity CLI 的双传输实现是否都属于支持范围。生产和 staging Compose 通过 sidecar 调用 CLI，handler 还保留直接启动 CLI 的本地路径；两条路径各自解析输出并处理 timeout/cancellation，且 prompt 输入协议不同。确认本地无 sidecar 使用场景后，决定收敛到一条路径或把两条路径作为正式支持模式并维持行为一致。验收覆盖大 prompt、terminal status、usage、失败和取消，不以源码重复本身作为删除依据。
- [ ] 调查 LiteLLM 全量 `/health` 的耗时和健康语义。审查时 `/health` 请求在约 15 秒后才返回，并报告六个 unhealthy deployment；`/health/readiness` 在约 0.14 秒返回 healthy 且数据库已连接。失败项包含无效 OpenAI API key 和不可连接的本地模型端点，而 Gemini Subscription status 显示已认证且有 14 个订阅模型。确认 optional deployment 的预期状态，区分进程/数据库 readiness 与 provider 可用性，并让健康结果足够快且可操作；不得把 readiness 当作每个 provider 可用的证明。

2026-09-28 审查时所有 Compose 服务处于运行状态；Gemini Subscription usage status 为 connected，sidecar 近期记录多次 Gemini completion，gateway 也记录成功的 Messages 请求。同时，同一近期日志窗口仍有 advisor 相关 HTTP 500 和上述 token usage 异常。审查时保存策略为 `codex-luna` / `force`、Advisor 关闭，因此这些健康/成功信号不表示当时普通 `current` 请求正路由到 Gemini；也不能用历史成功日志代替每个跟进项所需的复现与回归证据。

## 补充范围：Badlands API Gateway 本地协议兼容

**状态：进行中；LiteLLM SDK / 两个版本的进程内 proxy HTTP fixtures 以及外层网关确定性合同测试已通过；运行中的 4000 服务仍加载旧路由，staging 集成尚未验收。** 让当前 `4000` 本地网关稳定承接 Badlands API Gateway 的本地转发，保持
OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 的原生调用契约。网关把
本地模型设置为 `current`，保留请求协议和调用方的 stream 选择；图片请求目前由网关绕过
本地路由并发往其托管 preset。`response.model` 的 OpenRouter 目标匹配由外层网关负责。

### 已确认的失败证据

- 最近观察到失败的运行容器中，活动模型 `codex-luna` 映射到
  `openai/responses/gpt-6-luna`。工作树配置现指向新的 `codex-subscription` adapter，
  运行容器尚未重启加载该配置。
- 本地 `4000` 的生成日志返回 `Stream must be set to true`（Chat Completions / Messages）
  和 `Input must be a list`（Responses）。通过 staging 和直接访问本地端口都观察到 400。
- 既有 `tests/test_codex_bridge.py` 只证明标准 OpenAI Responses bridge 按调用方的
  `stream=false` 发起请求，不证明 Codex streaming-only 后端可完成非流式调用。新的
  `codex-subscription` adapter 有单独的成功型 LiteLLM SDK dispatch fixtures。
- Responses 的公开输入契约允许 `input` 为字符串或输入项数组。Codex 本地适配必须接受
  这两种形式，不能把特定后端的限制变成客户端要求。
- 正在运行的 Compose 网关版本为 LiteLLM `1.103.0`；Windows 开发锁定 LiteLLM `1.101.0`，
  这是已记录的部署/开发环境差异。只在开发环境通过桥接测试不能证明部署版本的行为。
- LiteLLM 内置 `chatgpt` Responses provider 会强制上游 `stream=true`，并将已完成的 SSE
  组装成 `ResponsesAPIResponse`；它的认证器读取根级 `access_token`，而 Subroute 当前凭据
  文件使用 `tokens.access_token`。它也会追加默认 Codex instructions 并按字段白名单重建请求，
  可能改变 instructions 或静默移除参数。因此它不是可直接替换当前 `openai/responses` 路由的配置变更。

### TODO

- [x] 用当前部署的 LiteLLM 版本核实内置 Responses-to-Chat bridge 对 Codex 订阅后端的
  流式与非流式能力，以及字符串和数组 `input` 的处理；记录能够证明具体缺口的 fixture
  或日志，优先复用 LiteLLM 已有协议处理。验证须覆盖部署版本 `1.103.0` 和开发锁定版
  `1.101.0`，并区分源码检查、fixture 和真实网关请求证据。
  已确认版本差异和 stream 缺口：运行容器 `1.103.0` 源码与生产失败日志、开发版 `1.101.0`
  源码与受控 fixture 均支持该结论；它们不构成三种协议的成功兼容证明。见[审计报告](docs/reports/badlands-protocol/litellm-audit.md)
  与[本轮评估](docs/evals/badlands-protocol-2026-09-25.md)。
- [x] 在精确的 Codex 部署边界把 Responses 字符串 `input` 包装成单条 user 输入项；数组
  输入项保持不变。新增回归断言覆盖 `store: false`、图片和函数调用输出字段不被静默丢弃。
  `uv run --extra test --locked pytest -q tests/test_codex_credentials.py tests/test_codex_bridge.py`
  在 Python 3.13 / LiteLLM 1.101.0 下通过（9 项）；这只验证本地边界行为，不代表当前运行
  容器已加载该源代码，也不证明 provider 请求成功。
- [x] 评估 LiteLLM 内置 `chatgpt` provider：不采用现有实现。除认证文件结构不兼容外，源码
  与本地 fixture 检查还确认它追加默认 instructions 并按字段白名单重建请求，可能改变指令
  或移除调用参数，不符合当前语义保真要求。
- [x] 评估只扩展 Codex `CustomLLM` Chat 适配器、并继续由 LiteLLM 转换公开协议的窄方案。
  自定义 provider 可要求 Codex 上游流式；非流式 Chat 在本适配器内收集 LiteLLM 原生 Chat
  chunks，流式 Chat chunks 转成 LiteLLM `GenericStreamingChunk` 再由 LiteLLM 转成公开协议。
  已核实 LiteLLM 1.101.0 原生 Responses 输入转换器会把
  roleless `function_call` / `function_call_output` 转成 assistant tool call 和 tool message；
  新增 fixture 对照转换结果验证 call id、函数名、参数和输出都保留。更早的审计认为这些项目
  会被丢弃，属于错误结论，已撤销相应 400 前置检查。工具调用组装、usage、终止状态和取消清理
  的本地 handler fixtures 通过；Chat、Responses、Messages 三种公开 SDK dispatch 的流式和非流式
  fixture 在开发版通过；同一组断言在运行容器的 LiteLLM 1.103.0 中以无 pytest 的 Python harness 通过。
  Responses fixture 还核对 instructions、工具历史、reasoning effort、输出限制、usage 和服务模型名。
  LiteLLM 自定义 provider 默认不接受 `reasoning_effort`，五个 Codex 部署显式声明
  `allowed_openai_params: [reasoning_effort]`，避免参数校验拒绝或 `drop_params` 静默丢弃。
  `uv run --extra test --locked pytest -q
  tests/test_codex_subscription.py tests/test_codex_credentials.py tests/test_codex_bridge.py
  tests/test_litellm_config.py tests/test_codex_proxy_http.py` 为 29 项通过（Python 3.13.11 / LiteLLM
  1.101.0）。SDK fixtures 与进程内 proxy HTTP routes 都使用 mock Codex stream，不等于访问当前
  `4000` 服务或 staging。最新全量本地套件 `uv run --extra test --locked pytest -q` 为 199 项通过（28 dependency/schema warnings）。
- [x] 在 Codex Chat 适配边界修复后端强制流式：内部请求固定 `stream=true`，非流式 Chat
  收集并校验终止状态，流式返回 LiteLLM generic chunks；LiteLLM 输出 Chat chunk、Responses 和
  Messages SSE。三种协议的公开 SDK dispatch fixtures 已覆盖终止序列。
- [x] 用 LiteLLM SDK fixtures 验证 instructions、roleless 工具/函数调用、reasoning effort、输出限制、
  usage、finish/status 和服务模型身份；无 provider usage 时不把本地估算 usage 报成 provider usage；
  credential/config fixtures 验证 `store: false` 保留。
  Responses 字符串和图片输入项也有边界保持断言。图片请求仍由外层路由到 hosted preset，
  该外层 hosted 路由尚未通过 HTTP 集成测试。
- [x] 保持网关的 `response.model` 目标匹配和 Subroute 实际服务模型身份彼此分离；成功响应
  不得把内部别名 `codex-luna` 当作下游 OpenRouter 目标 ID。Subroute 的进程内 LiteLLM HTTP
  fixture 断言公开 proxy 部署名与 Codex 上游物理模型不同；外层 API Gateway 的本地桥接 fixtures
  使用私有 local 响应并断言返回 canonical OpenRouter 下游目标。证据与范围见[评估](docs/evals/badlands-protocol-2026-09-25.md)。
- [x] 不把任意 HTTP 400 加入网关的 fallback 重试条件。只有 Subroute 能明确证明生成尚未
  开始的请求拒绝，才可设计端到端的机器可读 retryable 标记；网络断开、超时、已开始输出或
  dispatch 状态不明时禁止自动重放。外层 API Gateway 的 HTTP 400 regression 和现有 ambiguous
  POST failure matrix 断言 hosted route 未调用；本地 400 保持 local tier 原样返回。见[评估](docs/evals/badlands-protocol-2026-09-25.md)。
- [x] 增加基于 LiteLLM SDK dispatch 行为的回归测试：Codex 订阅模型在 Chat、Responses、Messages
  上均覆盖流式和非流式成功；Responses 同时覆盖字符串和含 roleless 函数调用历史的数组输入；
  验证工具调用、usage、目标服务模型身份及 SSE 终止序列。失败 fixture 仍保留明确错误断言。
  上述 fixtures 在 LiteLLM 1.101.0 pytest 下以及 LiteLLM 1.103.0 运行容器内的 Python harness 下通过。
- [x] 使用进程内 LiteLLM Proxy HTTP routes 加入 mocked-upstream 回归检查，覆盖 Chat、Responses、
  Messages 的流式和非流式响应、SSE 终止、配置部署名和 Codex 上游物理模型分离。另验证三种协议的
  Codex upstream 在未发送 terminal chunk 时，proxy 向客户端发出错误而不发送成功终止事件，且每个请求
  只触发一次 upstream dispatch。此测试通过 LiteLLM
  proxy HTTP 层；同一无 pytest 测试脚本在 LiteLLM 1.101.0 开发环境和运行镜像的 LiteLLM 1.103.0
  环境下均覆盖六种协议/模式组合、两次凭据热加载和三种不完整流错误检查。容器运行命令为
  `Get-Content tests/test_codex_proxy_http.py -Raw | docker exec -i subroute-gateway-1 python -`。
  上述进程内测试没有访问正在服务的 4000/4005 进程、外层 Badlands 路由或 hosted image 路径。
- [ ] deterministic 测试通过后，再经 `4000` 本地服务和 staging API Gateway 验证相同请求：
  local 路径返回完整原生结果、`response.model` 符合外层目标匹配、图片仍走 hosted preset，
  local 不可用时既有 hosted fallback 仍成立。部署或实时推理仍按各自授权执行。

  2026-09-25 只读 runtime 检查确认 `4000` 和 `4005` readiness 均返回 200，但两者
  `/model/info` 仍将 `codex-luna` 指向 `openai/responses/gpt-6-luna`；当前工作树已改为
  `codex-subscription/gpt-6-luna`。因此运行服务尚未加载修复，不能用当前 runtime 完成生成验收。

### 完成标准与停止条件

Chat、Responses、Messages 的流式/非流式合同及 Responses 两种输入形式都通过 Subroute
确定性测试，并通过网关 staging 的最小集成验证，才算完成。若 Codex 上游无法稳定提供
完整事件供 Subroute 正确收集，或无法证明 retryable 错误发生在 generation dispatch 之前，
停止本地重放方案并保留失败证据；不得用通用 400 fallback、伪造成功响应或静默丢字段来绕过。
