# Subroute 核心目标与技术规格 (GOAL.md)

本文档定义了 Subroute 的核心目标、架构设计边界、功能规范及验收标准。

---

## 一、 核心目标 (Core Goals)

1. **统一接入 OpenAI Subscription (ChatGPT / Codex 订阅)**：
   - 深度复用本地个人订阅凭证（读取 `~/.codex/auth.json` 中的 `access_token` 与 `account_id`）。
   - 请求发往 `https://chatgpt.com/backend-api/codex`，通过 LiteLLM 原生 Responses-to-Chat bridge 转换。
   - 支持凭据请求前热加载与自动刷新（防止 JWT 过期导致 401 掉线），无需手动配置官方付费 `OPENAI_API_KEY`。

2. **规范化网关端口：4000 作为生产环境 (Production)**：
   - **4000 (Prod 生产端口)**：对外统一服务端口，服务根路径 `http://127.0.0.1:4000`，API 入口 `http://127.0.0.1:4000/v1`。所有日常开发的主力 IDE 与工具（Cursor, Claude Code, Aider, Windsurf 等）统一且仅配置该端口。
   - **4005 (Staging 测试端口)**：作为预发布与集成回归演练环境，保障新 Provider、插件升级或配置变更时具备独立的隔离沙箱，不影响 4000 生产流量。
   - 单进程架构：废弃独立外部 Sidecar 进程，所有定制 Provider（如 Antigravity）与路由扩展全部运行在同一网关进程内。

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
- [ ] 将网关启动脚本 `scripts/start-gateway.ps1` 默认端口由 4005 调整为 **`4005 -> 4000`**。
- [ ] 确保测试套件 `tests/test_litellm_config.py` 对端口 4000 与单进程 loopback 规范进行断言。
- [ ] 更新 `README.md` 与 `docs/misc/architecture.md` 中对应的端口与架构说明。

### 阶段 2：OpenAI Subscription 通道与凭据守护
- [ ] 验证 `config/litellm.yaml` 中 `codex-subscription` 部署声明与响应桥接配置。
- [ ] 确保 `codex_credentials.py` 钩子在每次请求前准实时读取 `~/.codex/auth.json`，保证 Token 刷新无缝继承。
- [ ] 编写针对 Codex 凭据热加载逻辑的自动化单元测试。

### 阶段 3：动态路由与控制面实现 (Control Plane & Web UI)
- [ ] 编写 `src/subroute/plugins/dynamic_router.py`：
  - 挂载 `/control` 极简控制台页面（包含现代轻量下拉菜单与生效状态提示）。
  - 实现原子状态存储管理（支持 `alias`、`force` 模式切换）。
  - 实现 `async_pre_call_hook` 路由改写与元数据打标。
- [ ] 在 `config/litellm.yaml` 中增加 `current` 虚拟别名声明并挂载该回调插件。
- [ ] 编写全面的单元测试（覆盖别名映射、强制模式、原子读写、白名单拦截等）。

### 阶段 4：集成验证与文档交付
- [ ] 运行完整测试套件（`pytest -q` 全绿）。
- [ ] 验证本地打开 `http://127.0.0.1:4000/control` 下拉切换后，请求 `http://127.0.0.1:4000/v1` 的动态生效表现。
- [ ] 清理仓库内所有历史僵尸代码与废弃未提交变更。

---

## 四、 验收标准 (Acceptance Criteria)

1. **端口与环境验收**：
   - 默认运行 `start-gateway.ps1` 成功在 **Prod 生产端口 `127.0.0.1:4000`** 监听，所有生产开发工具仅连接该端口。
   - 运行 `start-gateway.ps1 -Port 4005` 可启动独立的 **Staging 测试沙箱**，用于验证新配置与测试套件。
2. **订阅通道验收**：在存在 `~/.codex/auth.json` 的环境下，请求 `codex-subscription` 能够顺利通过 Responses 桥接完成响应，并在 Token 更新时无需重启网关。
3. **控制面体验验收**：
   - 浏览器打开生产控制面 `http://127.0.0.1:4000/control` 呈现直观的下拉菜单。
   - 切换下拉项后，本地向 `http://127.0.0.1:4000/v1/chat/completions` 发送 `model: "current"` 请求，返回内容与元数据确认由所选模型生成。
4. **代码纯净度**：无外部独立 sidecar 进程，所有测试均通过。

## 补充范围：模型与推理强度选择

控制台分别保存目标模型与 Advisor 的推理强度，默认不覆盖客户端或提供商的设置。
候选值由 `config/litellm.yaml` 中各模型的 `reasoning_efforts` 定义，非法组合必须拒绝。
Gemini 订阅提供 Flash 3.8、3.7、3.6 和 Pro 3.1；Flash 支持 low/medium/high，
Pro 支持 low/high，并映射到现有 Antigravity 模型变体。该通道仍仅支持文本，
不新增流式或工具调用支持。目标强度仅随 alias/force 路由生效，Advisor 强度独立应用
于 Messages API 咨询。旧策略文件兼容默认值，单次请求使用同一策略快照。
验收需覆盖持久化、非法组合、真实 LiteLLM 转换、界面保存与订阅调用。

## 补充范围：专家独立阅读

Advisor skill 可按专家提出的证据问题启动短生命周期 Pi 阅读任务，不新增常驻
gateway 服务或直接 OpenAI 连接。Pi 使用 `http://localhost:4000/v1`，专家仍走
4040。每个任务最多 3 次专家调用和 3 个 Pi 任务，证据充分即提前结束。Pi 在
授权源码快照中只读检索，返回简短摘要、可核对的源码引用与未知项；实现和运行
验证仍由主 worker 负责。分别记录专家与 Pi 的用量，不把独立阅读当作无偏保证、
运行验证或已证明的 token 节省。

Routing desk: Target and Advisor use Provider > Model > Reasoning effort. Show configured connections, distinguish subscription from API access, retain saved unavailable choices with a warning, and save provider changes only after model selection.

Advisor selection is the sole switch for automatic Messages API consultation: a selected advisor enables it, and No advisor disables it. Target models have no separate advisor-enabled variants. Preserve the saved advisor choice when migrating retired guided target aliases.
