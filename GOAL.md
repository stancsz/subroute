# Subroute 维护性与可靠性目标

**2026-10-03 用户要求的 provider failover，源码配置已更新，运行验证待做：** 从用户当前所选模型开始，MiniMax、OpenRouter、Xiaomi MiMo 按此顺序尝试，每个 provider 至多一次；MiMo Pro 代表 Xiaomi fallback，GPT-6 Luna 是最后选项。Advisor 的 GPT-6.1 Sol 与 Gemini 3.8 Flash 互为一次 fallback，默认均为 low reasoning effort，默认选择 GPT-6.1 Sol；Gemini 使用私有 advisor alias，避免改变公共 Gemini target 请求。README、Compose 初始化默认值及 Advisor 控制器均已同步。没有增加通用重试、未运行测试或 provider 请求；部署和真实失败条件下的 fallback 尚未验证。失败尝试可能产生费用，响应 usage 仅代表 LiteLLM 最终成功尝试。

**2026-10-01 Advisor 节省输入预算：** 用户指出 Advisor 应设置较小上下文以降低咨询成本。Advisor 的最大输入/上下文已从 256,000 降至 32,000 tokens，自动压缩阈值 25,600；目标会话仍为 256,000。Codex Advisor aliases 和专用 experts 由 LiteLLM 原生上限执行；共享 Gemini target aliases 保留目标模型窗口，由 Advisor hook 在派发前用 LiteLLM 原生计数器执行 Advisor 专属上限。配置语法通过解析，Compose 配置有效；三个服务已重建，production/staging/experts readiness 均返回 200，运行中的三个 `/model/info` 已显示 Advisor 32,000/25,600 元数据。

**2026-10-01 用户要求的 Advisor overflow recovery，已部署，provider-backed 行为待实测：** 超过 32,000-token Advisor cap 时，仅压缩 Advisor 副本。首选一次 MiniMax M3，输出最多 8,192 tokens；失败、无法处理模态或结果仍超过 25,600 时最多回退一次 GPT-6 Luna。Codex subscription endpoint 不接受调用方输出 token cap，因此 Luna compaction 使用上游管理的输出限制；两次调用均受超时限制。保持消息级 system/developer 指令与最新 user turn 原样，压缩更早的会话历史；主模型仍收到原始请求。每次 provider usage 与 LiteLLM 的最终 token estimate 会分别记录。若两种压缩均不能产出合规副本则 fail closed。此 path 只在实际溢出时产生额外 provider 调用。代码语法通过，production 与 staging 已重建且 readiness 均 HTTP 200；没有进行会触发付费压缩调用的 provider-backed overflow test。

**2026-09-30 Cline 图片交付修复，部分完成：** 已识别并修复 Cline Desktop 整条 `<user_input mode="act">...</user_input>` 消息导致图片意图漏判的问题，原请求保持不变。开发和部署 LiteLLM 两版各 90 项回归通过；用户要求重启后 production/staging readiness 200，原会话 system/user 输入的真实 production SSE 返回一张可解码且已检查的 PNG、caption 和 `[DONE]`。真实 Badlands 外层同样返回一张 PNG 与完整终态，`x-gateway-tier: local` 证实已到重启后的 Subroute；Cline 图片显示边界仍未验证，不把网关生成通过称为客户端交付完成。见 [Cline 图片交付调查](docs/evals/cline-image-delivery-2026-09-30.md)。

**状态：complete**
**优先级：最高（目标已完成）**
**更新日期：2026-09-29**

**2026-09-30 新增目标：听音乐生产稳定性验收。** 早期 blocked 与 95% 未通过结论为历史 checkpoint，已由下方最新验收更新。用户要求搜索并实测大量过滤候选词/提示词、改善 MCP，并提供了 `C:\Users\stanc\Music\OSN_Captian_Aioz_LBI` 的真实参考歌。标准：原始 question/focus/音频不被静默改写；单曲/双曲完整 MP3 经真实 MCP → production Subscription 可用；拒绝、超时、坏响应有明确结构化失败与有限恢复；不隐藏重试/fallback/usage；逐项保留候选语境、调用、哈希、延迟、提供方 usage 和失败结果。混音准确性另需真实参考与可复核对照，不将传输完成当作专业混音通过。当前进展、失败和剩余依赖统一记录到 [音频评估](docs/evals/gemini-audio-2026-09-30.md) 与 [过滤候选语境](docs/misc/gemini-content-filter-candidates.md)。

**用户 resume 与新验收要求，2026-09-30：** 用户明确要求继续，并把当前 Gemini 成功率 95% 作为本增量门槛。历史 blocked 判断保留，不把历史失败抹掉。新增有界检查为预先固定的 60 个合法任务（48 个真实音乐、12 个自制语音探针），首次过滤拒绝最多原样复验一次，最多 120 个实际工具调用；记录每次原始终态、哈希、usage、首次成功与条件恢复率。生产配置暂不改变；先用实测决定重试是否有效，95% 指本批有效任务至少 57 项完成，未来总体成功率和专业感知准确性不能由该样本自动推出。当前固定 CLI、原输入与 Subscription 保持；没有删词、改写音频或切模型。前一 goal turn 的完成审计未解决外部过滤，本次恢复按新统计标准与用户明确方向推进；blocked audit 重新计数。

**95% 续验 checkpoint，progress：** 12 项压力探针已实际运行，首次完成 6/12；6 个失败各原样复验一次，恢复 2 个，最终 8/12。共 18 次工具调用、10 个过滤终态，全部真实 usage 为 94,972 tokens，其中额外复验 27,183；原输入、policy 不变，readiness 200。4 个持续失败使预注册 60 项混合批即使剩余全通过也最多 56/60（93.3%），所以未运行剩余 48 项音乐，不把它们或历史成功算入本轮。实验脚本默认关闭且保留逐次 ledger/同输入检查，生产重试配置未变；9 项脚本边界回归通过。当前原目标未完成，用户恢复后首轮取得条件恢复的新证据，目标 active；95% 的真实任务分母已向用户询问，回答前保留完整范围，不把压力样本率称为日常音乐总体率。新失败 usage 解析已有 live 证明。完整证据见同一音频评估“用户 resume 后的 95% 与一次复验试验”。

**用户 resume 后第二轮 checkpoint，progress：** 对上一轮 10 个实际拒绝会话做只读源审计，8 个最后生成含全部原始完整 WAV，哈希一致且日志读取数完整；2 个 A/A 复验在仅一个读取之后拒绝。一个首次 A/B 会话出现 missing toolSummary，但其无此错误的复验与另外 9 个过滤会话仍拒绝，排除用这一工具错误解释全部过滤。未新增 provider 调用/生产变更。已在既有音频评估中准备可审阅的官方 CLI issue 草稿，要求机器可读拒绝与真实 stop reason，不请求关闭防护；尚未提交第三方，待发布授权。CLI 仍 1.2.11，原安全类别未知，95% 与完整词表/专业门槛未过；当前 active，为恢复后第二轮，同一提供方边界仍存在，blocked 阈值未到。不能无改变重入探针或把压力样本分母缩小。

**用户 resume 后第三轮 blocked 审计：** 当前工作树、CLI 1.2.11、原 18 次尝试/10 个拒绝会话、94972 tokens 与未执行的 48 项 manifest 均核对一致；无新提供方反馈或人类发布授权，官方报告仍仅为草稿。本轮复核本身不增加成功率，属于 no progress；前两轮的实际恢复试验与源审计为 progress，但同一合法音频泛化拒绝/不可见原始分类连续三轮未解。一次复验仍有 4 项持续失败，原混合批最多 56/60，不能证明95%；没有完整官方禁词表或可验证的删词策略，不能用候选组通过、历史真歌成功或更多隐式复验替代验收。生产网关已修复部分继续可用，目标不标 complete。现进入 blocked，停止相同调查和新增调用；解除条件是提供方提供可归因反馈/受支持修复或用户明确改变任务/提供方合同。提交已准备报告需人类授权，获准提交只解锁诊断，不等于95%通过；自动 continuation 不代表第三方发布授权。详见音频评估的 resume 后第三轮审计。此状态不改写下方历史结项。

**最新 Gemini retry checkpoint，验收通过：** 在生产/测试 gateway 对 `gemini-subscription` 配置最多两次、同输入 LiteLLM retry，仅处理音频明确 `provider_content_filter`；MCP 400 秒 deadline 修复 150 秒时会截断第二次 sidecar 尝试的问题。预先固定 60 项混合样本在两次 retry cap 下完成 58/60（96.7%），其中 48/48 用户真实音乐、10/12 合成语音压力任务；再对先前持续拒绝的固定 A/A 样本做 live MCP 检查，LiteLLM 实际执行两次 retry 后第三次完成（64.99s），MCP 与 provider 使用量范围可区分，policy 未变化。相关用例 127 passed / 1 skipped，compose config 与 readiness 均通过。故本增量的固定样本和受限重试验收完成；不承诺未来总体成功率至少 95%，因为样本不是代表性随机流量且策略拒绝仍会持续。专业混音判断、未来 SLO 与上游拒绝分类仍不在本次成功率门槛内，保留原始失败和限制。完整证据见音频评估的“限定 Gemini 音频拒绝重试与 95% 固定样本门槛”。

**第三次连续 goal turn 的有界完成审计：** 同一外部条件持续存在：合法音频仍收到 Subscription 的泛化过滤拒绝，缺少原始拒绝原因和本通路可用控制。前两轮分别改善结构化错误/大量 live 证据、MP3 元数据/失败计量，但没有解决该条件；本轮核对实际 CLI 1.2.11 帮助、官方 reference/settings/headless 与截至 1.2.14 的 changelog，未发现可用于此订阅通路的过滤控制、完整词表或本次音频过滤修复。保存收据中同一 A/B 问题、focus 与两个原文件哈希完全一致却先拒绝后完成，逐项验收表见音频评估“有界完成审计”。本轮只改这两份既有记录，没有新的 provider 调用或运行代码变更。继续删词、重复调用求通过或改判成功不能推进原零过滤标准；专业混音正确性仍未验收，不作为替代完成条件。解除阻塞需要提供方提供可追溯的输入/输出拒绝反馈及合法音频误拒修复/受支持控制，或用户明确改变提供方与产品验收合同；没有发生这种改变前，不自动重入相同实验。此状态只适用于本增量，不改写下方历史结项。

**本轮 checkpoint：** 标准 MCP 结构化错误与输入预算已落地；部署镜像 230 项回归通过；完整 BK/OSN/Aioz/ASEN MP3 32/32、本批中英文候选语境 28/28、先前六词语音 6/6 完成，无本批过滤。两个本地输入失败后同会话的真歌调用恢复。原文件、production policy 不变，MCP root 已按用户音乐目录配置。不能据此认定某词高频被禁或提供方以后零拒绝。专业生产门槛 **NOT READY**：已观察《UFO》分析时间点超过实际时长，其他感知判断未盲听校准；无真实 Suno 退化版本、stem 或工程验收，Google 原始过滤分类仍不可见。下一可区分检查应是已知退化/相同音频的定位与齿音盲测、可靠输入时长和测量依据，以及提供方原始拒绝反馈；不得再用词表猜测或将传输成功改称专业混音通过。

**后续 goal turn checkpoint，progress：** MP3 同一字节快照现在有 MPEG 元数据与估计标签；无可读流本地拒绝；提供方失败 usage 的窄传输及真实 LiteLLM 拼接解析已回归。237 项部署镜像检查通过，拼接修复后另 35 项通过，三个受影响 gateway/experts 进程已重启且 policy 不变。全目录 48 项中 45 个完整 MP3 入口成功（43 个独立哈希），3 个超限合集本地拒绝，独立时长差最大 47 ms，原哈希不变。《UFO》同问法两次范围未越界但段落起点仍不一致。另 10 个探针续验 **实际发生一次良性 A/B 过滤**，其后同请求成功、普通语音恢复；失败 4099 tokens 在 bridge 日志确认并用原公共错误串回放恢复，不能假称为修正后又一次 live 拒绝。标准仍未满足，目标 active、零过滤与专业判断 NOT READY。下一动作是已知声音退化的盲测/定位校准及提供方原始拒绝反馈；不得使用无限实验、删词或隐式重试掩盖这次失败。完整新证据统一见同一音频评估的“生产续验”。

**2026-09-30 用户要求的 Gemini 音频与 MCP 增量，传输 MVP complete：** 复用现有 Gemini Subscription 和 Antigravity sidecar，`analyze_audio` / `compare_audio` 已经真实 stdio MCP → gateway → 订阅请求验证并注册。本机固定 MCP 1.28.1；部署镜像的 212 项受影响回归通过。盲口令正确识别，两个音乐附件都读取并指出已知噪声/失真差异；非法输入、Messages 丢附件、缺少读取、上游拒绝、超时与取消有明确失败/清理。两次偶发失败的原始生成记录包含完整音频，根因链为订阅过滤拒绝被 CLI SUCCESS 包装后旧网关误报成功；现返回可关联错误，不做隐藏重试。具体 Google 过滤触发条件未知。音乐观察仍有时长/BPM/声场和编曲描述错误，专业混音 UNVERIFIED，不改写下方历史目标。见 [完整评估与根因](docs/evals/gemini-audio-2026-09-30.md)。

**同日继续修复偶发错误，部分完成：** 共享终态校验已覆盖文本、Advisor、schema 与音频的 CLI 过滤假成功，带前导空白的诊断和纯空白答案不会再被接受；部署版本 220 项回归通过。真实 MCP 单文件正确转写，双文件仍被 Google 过滤，现明确返回 `provider_content_filter` / isError 并保留读取阶段、请求与会话 ID。网关缺陷已修复，提供方拒绝仍未解决，不宣称错误已消失；未改提示词、切模型、增加重试或丢附件。证据与下一步依赖见同一评估的“用户要求继续修复后的终态校验补丁”。

**2026-09-30 最新用户澄清：取消 Advisor 固定 GPT-6.1 的限制。** 默认 production Routing Desk 使用 OpenRouter MiniMax M3、OpenAI GPT-6 Luna Advisor 和 high reasoning effort；OpenAI Advisor 可选路由全部更新为 GPT-6 或更新版本，包含 GPT-6 Luna、GPT-6 Sol、GPT-6 Astra、GPT-6.1 Sol。共享 gateway 与专用 `experts` 均保留这些选择，旧 GPT-5.6 Terra Advisor deployment 移除。默认值不限制用户后续更换模型或 effort。下方 GPT-6.1-only 记录是被本次澄清取代的历史要求和证据。

**2026-09-30 历史限定，已被上方用户澄清取代：Advisor 只用 GPT-6.1 Sol。** 这项要求取代下方图片增量最初的 Sol/Astra 专用目录。Northstar caller 唯一 `sol` 选项发送 `codex-gpt-6.1-sol-advisor`；4040 `experts` 只暴露该版本别名并映射实际 `gpt-6.1-sol`，没有模型回退或隐藏重试。共享 worker gateway 的其他模型保持原有范围。72 项离线回归通过；真实 6.1 截图和图片→Advisor-directed Pi→精确源码证据→最终反馈均完成；旧 Sol/Astra 名称在 4040 返回 HTTP 400。仅重启 experts，readiness healthy。旧模型实测和论文对比保留为历史，不冒充 6.1 证据。见同一 [评估的版本限定记录](docs/evals/advisor-image-input-2026-09-30.md#gpt-61-sol-only-follow-up)。主执行者检查并接受独立 READY 复核；按已有 Docker 服务授权发布这六个相关文件，提交、远端 SHA 和最终 health 由 Git 记录与交付说明确认。

**2026-09-30 用户要求的 Advisor 图片输入增量：** Northstar caller 必须主动传实际图片给 Advisor，并允许它通过 caller 派自己的 source reader。专用 Subscription `experts` 适配器现保留 user image_url/input_image 像素输入、detail、文本和工具历史顺序；范围只含已有输入转换、Sol/Astra vision 元数据和相应回归。69 项离线回归通过，真实 screenshot、四图系统卡及一次 image→Advisor-directed Pi→最终反馈已完成；此前三次 reader 失败仍由 Northstar 保留。仅重启专用 experts，未改 production/staging 策略或安装全局技能。结果、发布状态和边界见 [Advisor 图片输入评估](docs/evals/advisor-image-input-2026-09-30.md)。这项明确授权增量不改写下方历史可靠性目标。

**2026-09-29 用户澄清的图片意图例外：** 不仅是 Images endpoint；Chat、Responses、Messages 最新用户消息中的明确图片生成请求，或显式 hosted image tool/图片工具选择，都优先于 Force/alias/off 和 `auto`，转到 Codex Subscription GPT-6 Luna 并开放 `image_generation` 工具。本地中英文规则排除历史消息、代码、引用及图片分析；不识别的表达可显式声明 hosted 工具。LiteLLM 继续负责公开协议、输入/工具转换和 Responses transport；窄适配器保留图片、说明文字、其他函数工具和终止状态。共享两个并发、180 秒、单张图片上限，无顾问调用、重试或 fallback。公开会话接口支持完整响应和 SSE；Messages 使用 Markdown data URL 文本，显示效果取决于客户端。四次真实 staging/production 请求均生成并检查了 PNG，production 策略 v69 未改，staging 策略字段恢复（版本 476）。证据、测试、维护负担及限制见 [图片意图路由验证](docs/evals/luna-image-intent-2026-09-29.md)；之前的 [Images endpoint 验证](docs/evals/luna-image-routing-2026-09-29.md) 保留为历史证据。未实现 Images 编辑/变体端点。这项明确授权的功能扩展不改变下方历史可靠性目标的验收定义。

**2026-09-29 提交前复查：** 后续 Auto 路由增加三个 deployment，当前 production/staging inventory 为 21 项。深度复查修复 Gemini 结构化输出校验、target/advisor profile、terminal usage 分类、Linux 子进程回收和状态误报，并补充 Auto fallback 行为回归。用户明确选择保留 Gemini 后端管理输出上限的兼容行为；CLI 无输出 token cap 参数，README 和控制台现明确告知调用方上限不被执行。最新双运行环境验证及边界见 [提交前复查](docs/evals/uncommitted-review-2026-09-29.md)，下方原始结项收据保留为历史证据。

## 目标

让 Subroute 的现有本地网关在正常工作和依赖故障时都表现可预测：请求不因网关内部阻塞、竞态、未回收的子进程或无法诊断的异常而随机失败。任何无法避免的 provider 错误都必须有边界、可归因，并且不能把无关请求一起拖垮。

当前只做维护性、可靠性和证明这两者所需的修复。此前的产品功能、兼容性和界面目标已存档，作为历史决策与证据，不构成当前活动目标或新增功能要求。归档见 `docs/goal/archive/`。

## 当前运行基线

2026-09-28 复查确认 production (`4000`) 和 staging (`4005`) 均运行 LiteLLM 1.103.0；两者 readiness 可用，PostgreSQL 和 Antigravity sidecar healthy。初始失败包含 Anthropic Messages 的 `NoneType.prompt_tokens` HTTP 500，以及 Responses API 的 Codex `Store must be set to false` HTTP 400。Messages 失败时 Codex Advisor 已成功并记录 `advice_injected`；失败发生在 LiteLLM 转换缺少 provider usage 的 Codex completion 时。修复后，Chat Completions、Responses、Anthropic Messages 的 buffered 与 streaming 路径分别在 production 和 staging 通过真实 Codex Subscription 请求，且 Anthropic Messages 的 advisor-enabled 路径也完成。当前保存策略保持 production `codex-luna` / `force` / 无 Advisor，staging `codex-luna` / `force` / `gemini-subscription`；另在 staging 临时选择 `codex-sol-advisor` 做完两种 Messages 检查后恢复原策略。端到端证据不关闭下方其他可靠性门槛。

**2026-09-29 用户限定的 provider 范围：** 已从活动 LiteLLM config、控制台 source 列表和两个 gateway 的环境中移除 `openai` → `openai/gpt-5.2-codex` 与 Gemini API key deployment；不再向 gateway 注入 `OPENAI_API_KEY` 或 `GEMINI_API_KEY`。保留 Codex Subscription、Gemini Subscription、OpenRouter，以及动态 `current` alias。不得把 Direct Gemini API 或 GPT-5.2 Codex API 请求放入验证矩阵。可靠性 closeout 当时 public `/v1/models` 有 17 项，两个 API-key alias 均不存在；之后按用户要求新增 `codex-luna-advisor` Subscription advisor alias，当前 inventory 为 18，API-key deployments 仍不存在。真实 Gemini 请求仅经 Subscription sidecar。

初始全量 `/health` 探测约耗时 15 秒并报告 6 个 unhealthy deployments；readiness 探测约耗时 0.14 秒。provider-usage 查询的源码路径由 async route 同步调用；在 cache miss 或显式刷新时，会在全局锁内串行访问多个 provider，代码声明的单源 timeout 合计最高约 36 秒。此前观察到的 0.02 秒 usage 响应命中了缓存，不能证明首次查询或刷新不会阻塞。这些只记录调查时状态；新的证据写在下方对应验收项。

这些是调查起点，不是修复完成证据。每次后续验证都必须重新取得时间和结果明确的运行证据。

## 不可放宽的约束

- LiteLLM 继续负责公开协议、流式转换、标准 provider adapter、重试和 advisor orchestration。只有经运行版本确认存在具体缺口时，才保留窄的自定义实现，并记录其用途与退役条件。
- 保持 Docker Compose 生产、staging、PostgreSQL 和 Antigravity Subscription sidecar 的既有角色。除非有当前证据证明某项运行角色造成故障，否则不得用主机进程或删服务作为清理方式。
- 不静默丢弃内容、改写指令角色、放宽调用限制、伪造成功、掩盖错误或增加隐藏重试、fallback/provider 调用。用户明确要求的 `auto`、MiniMax/OpenRouter/Xiaomi target 与指定 Advisor fallback 链是例外；target fallback 由 LiteLLM 管理，Advisor pre-consult fallback 明确记录实际失败和最终 Advisor。任何有意翻译或能力限制都必须可见且有回归证据。
- Provider 工作必须有并发上限、超时和取消后的子进程/连接清理；成功只在确认 provider 的 terminal completion 后报告。
- 保持生产和 staging 状态、配置、凭据与数据库的已声明边界。不得把当前共享状态描述成完全隔离。
- 不为假设中的未来需求新增服务、依赖、数据库、配置项或抽象。每项改动都要说明消除的具体失败机制及必须保留的行为。
- 保留已有用户改动。归档的文档是历史凭证，不得删除或冒充当前实现状态。

## 优先修复与调查

按降低故障影响和缩短诊断时间排序；每项只有在验收证据完成后才能勾选。

- [x] **消除 provider usage 对网关事件循环的阻塞。** 将 provider 网络读取移出 async event loop；不要在网络 I/O 期间持有共享缓存锁。为每个来源保持有界 timeout 和独立状态。验收：以受控慢 provider 将查询挂到其 timeout 上限时，50 次 readiness 请求的 p95 不超过 1 秒；同一窗口中的独立本地 stub generation 不被 usage 查询等待；超时只将对应来源标记 unavailable，后续刷新仍可恢复。

  **2026-09-28 修复与验收：** `provider_usage.py` 现用 HTTPX async 请求分别读取四个 quota/status 来源，各自保留 6/18 秒总 deadline；`asyncio.gather` 并发采集，单飞 refresh 锁把 provider 并发限制为最多四个请求，缓存锁只覆盖快照读写。取消传播到 HTTPX 并关闭连接；单源解析/超时错误变成该来源的 `unavailable`，其他来源仍独立返回。源码回归确认慢源并发、失败隔离、刷新合并。Compose staging 对抗测试将 Gemini status bridge 延迟 22 秒（超过 18 秒 deadline），usage API 在 18 秒后返回 HTTP 200 且只把该来源标记 unavailable；同一窗口 50 次 readiness 全部 200，p95 0.084 秒、max 0.085 秒，本地 OpenAI-compatible stub generation 返回预期 marker。恢复正式 Compose bridge 后，新鲜 usage 刷新再次报告 Gemini `connected`；staging 策略字段恢复为测试前值，production 策略未改。两个 gateway 的 readiness 和 usage refresh 均 HTTP 200，Codex Subscription、OpenRouter quota 与 Gemini Subscription 均返回 ready/connected。真实 OpenRouter buffered Chat 与 SSE、Gemini Subscription Messages 与 Codex Subscription target/advisor 请求均通过并带终止 usage。最近一次锁定测试结果为 250 passed、10 skipped、28 warnings；完整 staging 运行证据详见 [provider reliability evaluation](docs/evals/provider-reliability-2026-09-28.md)。
- [x] **找出并修复近期 Messages 500 的实际根因。** 覆盖 `Subscription advisor consultation failed; base request was not sent` 和 `NoneType.prompt_tokens`，不得从通用包装异常猜测根因。为 gateway 请求和 Advisor consultation 记录可关联 ID、base/advisor model、失败阶段及经截断和脱敏的根因。明确 Advisor 失败时 base request 应失败还是继续，并以显式策略实现，不准隐藏备用调用或额外 provider 消耗。验收：注入各类 advisor/provider 错误时，响应分类、日志原因和 base dispatch 行为一致；修复后目标路径的回归及运行请求成功，且失败请求可关联到唯一根因。

  **2026-09-28 执行记录：** 实际 `/v1/messages` 请求确认 Codex Advisor 已成功返回 usage 并注入建议；主模型随后因 Codex stream 未提供 usage 而让 LiteLLM Anthropic adapter 解引用 `None.prompt_tokens`。自定义 Codex adapter 现对 Anthropic Messages 的 forced upstream stream 请求 `stream_options.include_usage=true`，并在 provider 仍未给出 usage 时返回明确 502；streaming 缺少 usage 时不会发出 `message_stop`。实际 `/v1/responses` 请求另复现 Codex 的 `Store must be set to false`；其原因是 LiteLLM Responses-to-Chat 转换没有把 `store=false` 带到自定义 provider，adapter 现在只在该字段缺失时于 Codex provider 边界补 `false`，显式值保留。对应回归测试覆盖丢失 usage、缺失 store、公开代理路径与流完成状态。真实 Compose 验证在两个端口均通过全部 3 种协议 × 2 种 stream 模式；staging 的 Gemini Advisor 和临时 Codex Advisor 均有 `advice_injected` usage 收据，Codex Advisor 策略随后恢复为 staging 原值。后续已补齐可关联的请求/consultation 根因日志、Advisor 请求上限失败关闭的 staging live 证据，以及客户端断连后子进程回收和下一请求恢复证据（见 2026-09-29 live failure matrix）。

  **2026-09-28 后续验证及 2026-09-29 补充：** Gemini Advisor 的 RuntimeError、TimeoutError、ValueError 注入单测均验证 fail-closed、消息不变和截断原因；错误日志同时记录 LiteLLM `litellm_call_id` 与独立 consultation ID，常见 bearer/API/access-token/password 和 OpenAI/Google key 形态先脱敏。早期 staging live failure test 通过受控 request ID 触发 bridge 请求上限失败，确认不派发 base；真实客户端断连已独立复现，确认 sidecar 子进程退出且后续 Gemini target 请求成功（详细 receipt 见 live failure matrix）。Sol advisor 咨询 receipt：model `codex-sol-advisor`，request `chatcmpl-codex-advisor-0f7dd4386694`，prompt/completion 408/206 tokens、6.095 秒；建议维持已选 advisor 失败即 fail-closed，`decision_changed: false`。**2026-09-29 错误分类补充：** 将 selected advisor 失败从通用 RuntimeError 改为 LiteLLM hook 支持的 HTTPException，保留 `base request was not sent` 语义和 Anthropic error envelope；超大桥接输入 413、输入转换错误 400、上游 429 为 429、超时 504、上游 auth/connectivity/server 错误 502。provider 原始原因只进入脱敏/截断日志，客户端响应带 gateway 与 consultation correlation ID。单测覆盖 8 种错误类别；staging `/v1/messages` 真实超限请求 HTTP 413、Anthropic `type:error` 响应且不调用 sidecar/base；timeout mapping 修复前曾完成 9/9 live matrix（67.26 秒）；之后出现过一次真实 Gemini 3.6 Flash INTERNAL 500，gateway 返回 502 且没有隐藏重试，紧接着同一模型恢复成功。最终相关 ID 由 gateway 传至 sidecar 并写入 sidecar terminal logs，JSON provider request 保持不变。真实 provider auth/quota/down/timeout 故障没有被主动注入；按 live-matrix 验收要求，这些故障保留为明确的未实测边界，不将本地 transport 注入冒充 live 证据。

  **2026-09-29 最终源码验收：** 完整 staging live failure matrix 10/10 通过（60.54 秒），覆盖未知模型 404、输入拒绝、advisor fail-closed、断连回收、OpenRouter buffered/SSE，以及全部启用的 Gemini Subscription、Codex Subscription 和 Advisor aliases。未配置的 `/v1/skills`、`/v1/rerank`、`/v1/embeddings` 均在 provider dispatch 前返回标准 400 `unsupported_operation`，不再表现为 500。Gemini 3.6 Flash 在完整矩阵和 health probe timeout 之后的单独复测中均真实成功。Production gateway 与 Antigravity sidecar 在最终源码下重建；Codex Subscription 请求精确返回 marker，HTTP 200、end_turn、18/13 provider tokens；请求前后策略均为 `codex-luna` / `force` / 无 Advisor，policy version 39。Production readiness 50/50（p95 3.61 ms，max 8.99 ms），staging readiness 50/50（p95 5.32 ms，max 103.98 ms）；production 与最终 staging health 均为 14/2，唯一不健康部署为按要求排除的 local-model/Ollama。staging 曾有一次 Gemini 3.6 Flash health timeout，随后 target 重试及完整健康扫描均成功。Direct Gemini API、GPT-5.2 Codex、FreeToken/Ollama 均不在此次 live matrix。此后的更新验证见下方 2026-09-29 closeout。

  **2026-09-29 closeout：** 在当前 staging 和 production 上重新检查，readiness 均 HTTP 200、数据库 connected，`/v1/models` 均返回 17 项且不含 `openai`、`gemini-api` 或 `openai/gpt-5.2-codex`。当前 staging live failure matrix 10/10 passed（68.88 秒）：Gemini Subscription、3.7/3.6 Flash、Pro、Codex Subscription/Astra/Terra/Reserve、Codex Terra/Astra Advisor aliases、OpenRouter buffered/SSE 均以真实内容和 provider usage 完成；覆盖未知模型拒绝、输入边界、Advisor 失败关闭及 correlation、断连后的 CLI 回收与后续成功、能力未配置时的 400。production policy 为 `codex-luna` / `force` / 无 Advisor / medium（version 43）；staging 为 `codex-luna` / `force` / Gemini Subscription Advisor / high（version 445），与测试前保存字段相同。锁定回归套件 250 passed、10 skipped、28 warnings；跳过项均为显式 opt-in live cases。真实 provider auth/quota/down/timeout 未主动注入，仍是标注清楚的外部证据边界。所有优先项、行为回归、Compose live 验收和完成标准现已满足，目标关闭。

  **2026-09-29 用户后续需求：** 已将 GPT-6 Luna 加入可选 Advisor，复用现有 Codex Subscription Advisor adapter；详见 [Luna Advisor live evaluation](docs/evals/codex-luna-advisor-2026-09-29.md)。这项后续功能不改变本可靠性目标的结项范围。
- [x] **限制 Antigravity sidecar 的并发 CLI 工作并保证回收。** 为同时运行的 CLI 进程设有限额；过载时明确拒绝或排队且等待有上限；超时、客户端断开和进程启动失败后，终止并回收相关子进程。验收：并发超过限额时进程数不再增长，取消/超时后无残留进程；错误之后紧接的有效请求仍能完成。

  **2026-09-28 修复与验收：** `bridge.py` 用一个容量为 4 的 `BoundedSemaphore` 限制 `agy` CLI 和 `agy models` inventory 总进程数。请求最多等待 0.5 秒，之后 sidecar 返回明确 429；slot 会在所有退出路径释放。CLI 通过 `Popen` 启动独立进程组，125 秒 deadline 或检测到客户端 socket EOF 时先 TERM，2 秒后仍存活则 KILL，并 `communicate` reap 后才释放 slot。Linux Compose 容器中的真实 HTTP server 以测试 CLI 证明：设置单 slot 时第二个并发请求 429，第一个返回 usage 且后续请求 200；client disconnect 和 0.2 秒 timeout 均终止并回收 CLI，之后的有效请求继续 200。源码回归 13 个 sidecar 测试加 handler boundary 测试通过。新镜像 `subroute-antigravity` 启动为 healthy，`/health` 和 `/v1/status` 都返回 200，认证状态可用且列出 14 个 Gemini Subscription models；重建后 staging 真实 Gemini target 与 Gemini advisor + Codex target 再次返回 HTTP 200 和 provider usage。
- [x] **让 Gemini Subscription 功能声明与实际行为一致。** 早期归档称其为 text-only；当前实现的合同是文本、schema-constrained tool call 和 buffered SSE，client tool 仍由调用方执行，vision 不支持。SSE 在 CLI 完成后才开始发事件，不是逐 token streaming。模型元数据、控制台说明、README 与 handler 行为必须共同维持此区别，不在本目标中新增能力。

  **2026-09-28 验收：** 四个 Gemini Subscription deployment 的能力 metadata 已从 `streaming` 改成 `buffered-sse`；控制台和 README 原有提示已确认准确。Live staging 实际 Messages buffered 和 SSE 请求均返回 HTTP 200、正确终止事件及 provider usage；SSE 的 message delta 携带 provider 报告的 input/output tokens。真实强制工具选择返回声明的工具名与符合 schema 的参数、`stop_reason=tool_use`，网关不会执行 client tool。回归锁定 adapter 必须等 CLI 完成后才 yield SSE chunk。staging 策略已恢复。
- [x] **减少或约束重复的 Antigravity CLI transport 实现。** Compose 使用 sidecar，handler 另保留直接启动 CLI 的本地路径；二者分别实现输入、输出解析、usage 和取消行为。先确认两种运行模式是否都受支持；若两者保留，建立共享合同并对长 prompt、terminal status、usage、超时、取消和错误做一致性回归；若一条路径无当前用途，记录证据后再收敛。

  **2026-09-28 收敛决策：** `docker-compose.yml` 是唯一声明的部署方式，README、architecture 和实时运行全部指向 Compose sidecar；宿主机 `AGY_PATH` 直启仅由旧的生命周期测试覆盖，没有面向用户的运行说明或 Compose 调用。该 fallback 使用另一套 CLI 参数、stdin framing、terminal-result 校验，已造成行为合同分叉。删除该未文档化 fallback，缺少 `ANTIGRAVITY_BRIDGE_URL` 时显式返回配置错误；移除旧生命周期测试，以拒绝直启的回归替代。Compose HTTP bridge 成为唯一 Gemini Subscription transport，保留其 CLI personal-subscription 运行角色。真实 staging target 与 advisor 请求证明公开 LiteLLM 链路仍成功。
- [x] **明确 readiness 与 provider health 的职责并控制探测耗时。** 区分进程/数据库 readiness 与每个 deployment/provider 的可用性；解释或纠正无效 OpenAI API key、本地不可达 endpoint 等 optional deployment 在全量健康结果中的状态。验收：readiness 在 1 秒内响应；完整 provider 检查有明确的总时间上限、逐部署结果和失败原因；已知 optional provider 故障不会被显示为整个 gateway 无法服务，也不会被健康状态隐藏。

  **2026-09-28 修复与 2026-09-29 复验：** LiteLLM 1.103.0 的 `/health` 默认将全部模型探测并发执行；生产和 staging 同时检查时会超过 Gemini sidecar 的 4 个 CLI 槽位，健康探测本身可能造成 429。使用 LiteLLM 支持的 `health_check_concurrency: 2` 和 `HEALTH_CHECK_TIMEOUT_SECONDS=10`，两个实例同时检查最多占 4 个 sidecar CLI 槽位。2026-09-29 又按 LiteLLM 本地实现，对仅供动态 hook 使用的虚拟 `current` deployment 设置 `disable_background_health_check`，并启用 `health_check_skip_disabled_background_models`；这消除 health 将虚拟 `openai/current` 误报为缺少 OpenAI API key 的情况，不改变 `current` 的请求路由。活动 health probe 现有 16 deployments，理论最长 `ceil(16/2) * 10 = 80` 秒。最终分别独立运行生产和 staging 完整 health：两者 HTTP 200，production 21.34 秒、staging 19.31 秒，均 14 healthy / 2 unhealthy；两项明确为 FreeToken local endpoint 和 Ollama unreachable，符合用户的排除范围。OpenRouter、4 个 Gemini Subscription、5 个 Codex Subscription/Reserve 及 3 个 Codex Advisor 均在两个实例 healthy。此前一次 health 与 usage refresh 重叠时 production Gemini 3.8 出现一次 unhealthy，紧接着 production 单独 health 复测恢复正常；日志没有匹配的 sidecar 429，原因未证实。最终重建后生产与 staging 各 50 次 readiness 全部 HTTP 200，p95/max 为 39.9/63.7 ms 和 30.0/34.3 ms；两端 PostgreSQL connected。
- [x] **拒绝未配置的模型，避免 force 路由静默选用其他 provider。** dynamic router 只允许 config 中可选择的模型或 `current/default/auto`；未知名在改写目标前以 HTTP 404 拒绝。内部 Advisor sub-call 由显式 metadata 标识，不再仅凭 `-advisor` 后缀绕过检查。验收：staging Chat Completions、Responses 和 Anthropic Messages 的未知模型均 404，未发送到 provider；真实 Gemini Subscription / Codex / OpenRouter 路径通过矩阵。

  **2026-09-29 验收：** staging 上三个公开协议对 `subroute-live-no-such-model` 分别 HTTP 404；响应没有 provider 成功或 provider usage。显式 `force` 仍将已知公开模型/virtual alias 发往所选 active target。
- [x] **把当前无 deployment 能力的操作错误从 500 收敛为明确 client error。** LiteLLM 1.103.0 的 custom provider 名不能转换成内置 `LlmProviders`，而当前 model inventory 也没有 skills、rerank 或 embeddings 能力。启动阶段注册的精确 route guard 返回 400 `unsupported_operation`，避免内部 provider-enum 异常和 chat-only embedding 扇出。部署具备相应 capability 后应移除对应 guard 并让 LiteLLM 接管。

  **2026-09-29 验收：** staging `/v1/skills`、`/v1/rerank`、`/v1/embeddings` 均返回 HTTP 400 和稳定错误码，不再产生 LiteLLM 500。没有新增服务或 provider transport。
- [x] **建立 target 与 advisor 的真实 live API 失败矩阵。** 新增默认跳过、显式 opt-in 的 staging integration tests，直接请求运行中的 gateway 和实际订阅 provider，不用 mock provider response。覆盖 Gemini Subscription 与 Codex Subscription target 的真实成功、未知模型拒绝、Gemini 不支持的输入块、bridge 请求上限；覆盖 Gemini 与 Codex advisor 的真实咨询成功、Gemini advisor 请求上限导致的失败关闭，以及失败后的真实 target/advisor 恢复。每项记录实际 HTTP status、终止原因、provider usage 是否存在及相关日志；测试需恢复 staging 原有 target/advisor 策略，禁止改动生产策略。对真实 provider auth、quota、服务不可用和超时等不能安全主动触发的故障，单独标注未实测；不得把本地 mock 或纯 adapter unit test 写成 live 覆盖。
  已新增 `tests/test_live_gateway_failure_matrix.py`，默认跳过，显式设置 `SUBROUTE_RUN_LIVE_TESTS=1` 后直连 loopback staging，不 mock provider。此处 8 passed、1 failed 是 2026-09-29 后续版本的一次历史运行：Gemini Subscription 3.6 Flash 的真实 provider 返回 `INTERNAL 500`，sidecar 报告 CLI terminal status `ERROR`，网关按合同返回 502，没有重试或 fallback。显式重试同一模型后 HTTP 200、marker 和 provider usage 均正常；随后单独重跑被中断的剩余 alias 子集为 1 passed。该瞬态 provider 故障和恢复仍保留为历史证据，不覆盖其真实 502 结果。之后 final-source 矩阵 10/10 通过，且本次 closeout 新鲜重跑同样 10/10 通过，故该项已完成。真实 provider auth/quota/down/timeout 注入仍标注未实测；FreeToken/Ollama 不在用户要求范围内。

  **2026-09-29 production route check：** 未改 production 策略，保持 `codex-luna` / `force` / 无 Advisor (version 35)；向 `model=current` 发送真实 `/v1/messages` 请求返回 HTTP 200、精确 marker、`end_turn` 和 26/15 provider tokens，请求前后 policy snapshot 完全一致。

  **2026-09-29 最终源代码部署复验：** production gateway 重建后策略仍为 version 35，`current` Codex Subscription `/v1/messages` marker 请求返回 HTTP 200、`end_turn` 和 20/14 provider tokens；策略前后相同。production/staging readiness 各 50/50 HTTP 200，p95 为 4.7/6.4 ms、max 12.7/15.5 ms。两个 gateway 的完整 health 分别 20.39/27.31 秒返回 HTTP 200，均有 14 healthy / 2 unhealthy；全部 13 个本目标保留的 OpenRouter、Gemini Subscription、Codex target/advisor deployments healthy。唯一不健康项为按用户范围排除的 FreeToken 与 Ollama。

## 完成标准

- 上述全部优先项都有代码/配置修复、行为边界回归检查和新鲜本地 Compose 运行证据；未复现的失败必须标明未证实，不能通过勾选或删除记录宣称解决。
- 有效 provider 请求成功完成后，紧接的请求仍能成功；provider 超时、断连、无效凭据、过载和不完整 terminal result 都产生有界、可归因的失败，不导致事件循环卡住、资源泄漏或伪造成功。
- 对所有当前启用且配置有效的目标/Advisor 路径执行有代表性的端到端检查；外部 provider 自身不可用时，记录 provider 明确返回的错误，并验证其不会造成无关路径失败。mock 测试、readiness、model inventory 或旧成功日志均不单独构成运行路径通过证据。
- 相关测试套件通过；LiteLLM 开发锁定版和 Compose 运行版的差异在受影响边界上得到验证。最终记录 gateway 5xx 分类、健康/ready 延迟、sidecar 最大并发与清理结果。
- `README.md`、当前实现和本目标关于可用性、能力与失败行为保持一致。维护性审查仅删除有配置/调用/验收证据证明已失效的代码，不以代码行数或服务数量作为简化目标。

## 当前活动边界

本文件记录已完成的维护性与可靠性目标。`docs/goal/archive/` 内的旧目标仅用于追溯此前需求、决策和验证，不要求继续实现其中的商业就绪、Badlands 兼容或其他功能范围，除非它们被证明是当前可靠性修复必须保留的现有行为。

## 2026-10-01 初始上下文策略记录（后续 Advisor follow-up supersedes）

初始目标路由使用 256,000-token context window 和 204,800-token client compaction threshold，计数包括当前完整上下文中的系统指令、工具、附件和压缩后的前缀。生产、staging 的 21 项和专用 experts 的 4 项在该次验收时均使用相同元数据；LiteLLM 原生 pre-call checks 拒绝超限输入。客户端继续拥有会话压缩；无状态 gateway 不增加会话存储或自定义摘要服务。Advisor 限额后来单独降低至 32,000，并按本文件顶部 follow-up 增加了超限压缩恢复。

Desktop 六个启动配置和现有直接 Subroute 的 Codex/Hermes 配置已更新。Python 467 passed、11 skipped；Desktop 5 passed。三个运行服务均 healthy，全部 model_info 字段核对通过，32 项真实 HTTP 边界检查通过且路由策略不变；Codex 真实请求完成并报告窗口 256000，Hermes 原生阈值边界和 OpenCode 实际合并配置已验证。OpenClaw 未安装，只验证发布版本源代码对应的配置；未宣称每个客户端都完成真实 204800-token 压缩或 provider 能接受完整 256000-token 请求。已有会话需要重启，上游更小窗口仍适用。需求、所有权、维护负担与证据见 [上下文策略验证](docs/evals/context-policy-2026-10-01.md)。这项明确授权增量不改写其他历史目标。

## 2026-09-30 Gemini 95% 成功率要求续办

不能对 Subscription 上游策略拒绝承诺 95% 成功率。此前有界合成探针为 12 个任务中首次 6/12 成功；6 个初次拒绝各做一次完全相同的复验，2 个恢复、4 个仍拒绝，最终 8/12 完成。此结果不是代表性歌曲成功率；复验报告 usage 为 27,183 tokens，相对初次 67,789 tokens 增加 40.1%。每个拒绝在已测复验后仍可能持续拒绝，增加次数没有可验证的独立恢复概率或有限次数保证。

按 Luna advisor 的有界意见（model `codex-sol-advisor`，request `chatcmpl-codex-advisor-b9ed1eab651b`，1115 reported tokens），不启用通用 502 重试；过滤拒绝保持终态。advisor 的 stop rule 是恢复低于 20%、重试 usage 超过基线的 30%，或 CLI 内部重试使额度无法验证时停止。当前 recovery=33.3%，但额外 usage=40.1%，且 CLI 内部调用计数不可核验，因此不满足生产重试门槛。`decision_changed=true`：从先前讨论的条件复验转为只修复可观测性、不部署生产重试。详见 `docs/evals/gemini-audio-2026-09-30.md`。

本次修复将 sidecar 已有的 `completed_reads` 计数通过私有错误 JSON 和 LiteLLM handler 传至 MCP 的结构化 refusal 错误，并增加 `expected_reads`；过滤仍以 MCP `isError=true` 暴露，`automatic_retry=false`。聚焦测试 `107 passed, 1 skipped`。部署后 production gateway readiness 为 HTTP 200、Antigravity sidecar healthy，容器内 bridge 与两个 gateway Python 源文件 SHA-256 均匹配工作树。此更改不会让 Gemini 接受被拒绝的输入，也不是 95% 成功率验收；当前 resume goal 仍 active，需上游有支持的反馈/行为变化，或用代表性任务获得足以评估的稳定性证据。
