# Gemini Subscription 音频与 MCP 验证

日期：2026-09-30。状态：只读听觉 MCP 和网关传输 READY；专业混音质量 UNVERIFIED。单执行者做单独复查，未做独立评审。

用户要求直接复用现有 Gemini，修好网关，并让 MCP 经它听音频。质量门槛是音频内容确实进入模型，不以 HTTP 200 为通过标准。范围是两个只读 MCP 工具与现有附件通路，不包含自动混音、DAW 操作或专业音乐审美验收；没有可供评测的用户歌曲。本增量没有界面变更，视觉验收不适用。

## 失败基线与已查明的原因

[初始实测收据](gemini-audio-2026-09-30.json) 保留原始失败。Staging 的 Chat `input_audio` 返回 400；Messages 的非标准 `audio` 块返回 200，但模型说没有收到音频。文本对照正常。LiteLLM 1.103.0 的 Anthropic 转换器只识别其已实现的块类型，未知音频被遗漏。Antigravity 1.2.11 的 NDJSON 用户消息只接受文本；`@file` 在 headless 请求里没有自动成为附件。

在同一个 Antigravity 订阅中，`view_file` 能将 WAV 内容交给模型。盲探针提示词仅要求转写文件；随机口令和数字只存在音频中，模型准确返回 `velvet orange` 和 `7492`。这是传输与听觉输入证据，不能证明混音诊断准确。四个短正弦音的初次描述错误地称为打响指，保留这一失败，禁止以口令转写成功推导音乐鉴赏可靠。

## 本增量的实现与维护成本

保留 LiteLLM 公开 Chat 协议和 buffered SSE；增加 PCM WAV/MP3 `input_audio` 到 Gemini 的窄翻译。明确音频输入优先于保存的 Force/alias/off/auto，并关闭该请求的 Advisor；保存策略不被修改。Messages/Responses 的音频输入不做自定义协议转换，必须明确拒绝，防止静默遗漏。

现有 sidecar 在一次请求的临时工作目录写入解码后的原始字节，通过订阅的 `view_file` 读取后收回答案。最多两个附件、合计 20 MiB；文字请求仍限 1,000,000 bytes。复用现有四个并发槽、125 秒 CLI 上限和断连回收，没有网关自定义重试或新容器。CLI 自身属于黑盒，可能按其内部策略做模型请求重试，不能把一次 CLI 调用声称为一次底层模型请求。文件读取是原生工具步骤，会增加模型往返与 usage。

MCP 使用已随 LiteLLM 安装的标准 Python MCP SDK，只提供 stdio 的 `analyze_audio`、`compare_audio`。文件根目录由启动参数指定，拒绝越界路径；模型参数固定 Gemini Subscription。没有新数据库、下载外部音频、重采样、分轨或处理链。对比暂未匹配响度，不能用于认证哪版混音更好。

AGY 的 primary-agent init 工具列表比 profile 声明宽，不能把 `tools: [view_file]` 当作安全边界。Sidecar 启动给现有设置补充命令、写文件、浏览、MCP 和 `/root` 读取 deny；保留其他已有设置，deny 按官方规则优先。每个音频请求用独立 workspace，不赋予免确认权限；返回成功前要求每个指定文件都有完成的读取步骤，任何其他工具/文件步骤使本次请求失败。CLI 原生历史可能保留音频和对话，本地临时工作目录删除并不等于 Google 或 CLI 历史已删除。

退役条件：当所部署 CLI 支持已验证的二进制 headless 输入或 LiteLLM 原生订阅音频 adapter 时，移除文件读取兼容层；当部署版本原生保留 Messages 音频且该协议正式支持它时，再评估解除拒绝。不要维护另一套公开协议。

## 验证记录

部署镜像 digest 保持 `bd07ceb1fc7c4505f116c4eb2767956a8accba3119548dd8ae55e5356a381d56`，LiteLLM 1.103.0、MCP 1.28.1、AGY 1.2.11。在该镜像的临时 QA 容器安装 pytest 后，六个受影响测试文件共 212 passed / 3 warnings。涵盖原字节/顺序、音频路由与 Advisor 禁用、非法/截断 WAV、嵌套和 Responses 音频拒绝、目录/符号链接越界、缺少读取、意外工具、拒绝、超时、断连、临时目录回收，以及已有进程回收、容量和图片意图回归。CLI 在音频测试中用受控 stub；音频内容实际到 Gemini 的证据另来自以下 live MCP 请求，不能以 mocked 回归替代。

失败 QA 保留：第一次挂载整个 `/app` 遮住镜像 venv，测试不能导入依赖；改为独立只读挂载 source/tests/config。第二次音频临时目录落在只读挂载导致六项失败；测试将临时目录指向 pytest tmp_path 后通过，运行 sidecar 仍使用自身可写目录。尝试用 PyPI 的 LiteLLM 1.103.0 和 MCP 1.28.1 重建本机环境时，索引元数据要求 MCP 2.x，与实际部署镜像内的 1.x 约束不同，未改动 LiteLLM 或生产依赖；最终用原始固定镜像验证。music extra 与实际使用的 MCP 1.28.1 对齐；这是已发现的外部依赖漂移，不能把从索引重装描述成等价环境。

真实 stdio MCP 握手、工具发现、单文件与双文件调用都完成。Staging 盲转写识别 `velvet orange / 7492`；最初双文件识别 `silver river / 5821`，后续双文件请求也暴露上游过滤拒绝，不声称重复调用全成功。[Staging 收据](gemini-mcp-staging-2026-09-30.json) 保留 structuredContent 修复后的拒绝误报，促成本次失败分类修复。

[Production 失败收据](gemini-mcp-production-2026-09-30-1790790163.json) 显示单文件成功、双文件拒绝，此时 MCP 已返回 isError，未自动重试。[真实音乐载荷收据](gemini-mcp-production-2026-09-30-1790790342.json) 和 [最终 MCP 收据](gemini-mcp-production-2026-09-30-1790790767.json) 显示两入口通过同一 production Gemini route 完成，固定文件哈希、48 kHz/stereo/15 秒元数据、provider usage 和原观察均保存，production policy 完整未变，readiness 200。样本是自制合成鼓/贝斯/旋律 loop，B 加入噪声和失真，不是艺人参考、用户歌曲或专业混音对照。模型识别出 B 的噪声、失真和清晰度损失，但曾把 15 秒猜成 7–10 秒，把 96 BPM 猜成 85–90 或 120–128，编造 swing、混响和声场。MCP 后来提供已知时长并禁止估算精确 BPM/声场宽度，最终回应仍有乐器入场顺序与空间描述问题，不能宣称音乐诊断完全正确。

两个 gateway 和原 sidecar 已重启，原 production/staging 策略保持。`subroute-music` 已通过 `codex mcp add` 注册，本机 Python 的 MCP SDK 固定 1.28.1，root 为 `D:\github\subroute`，工具 timeout 180 秒。当前会话不自动获得新 tool menu；新会话可加载该服务器。MP3 通过明确格式校验和同一传输实现，但本轮 live 样本都是 PCM WAV，MP3 live compatibility 未验证。音频 buffered SSE 复用现有实现，本轮未另做 live 音频 SSE 验证。

## 偶发失败的根因与可观测性

[原始生成记录的只读审计](gemini-audio-cause-2026-09-30.json) 对照两次拒绝与一次双文件成功：三个会话最后一轮 generation metadata 都含两个完整 WAV，哈希分别为 `57bf8a39...e23128e3` 和 `df9d795e...00cec89`，与输入一致。两次拒绝会话为 `eb8becf2-5c01-4b2e-a006-2c8b41f9bb9b` 和 `e231eae2-63c7-4627-b045-bf090b0042f4`，读取步骤已完成，后续生成记录含 CLI 的过滤拒绝文本；成功会话 `8d47165b-3f84-4f06-9fd6-6b3b8a2f41bd` 同样携带完整音频。审计只导出哈希、大小、索引和拒绝布尔值，没有导出原始音频、凭据或内部推理。

确定的故障链是 **完整音频已进入订阅生成请求 → 订阅层拒绝 → CLI terminal 仍为 SUCCESS → 旧网关只检查非空正文，MCP 误标 complete**。附件读取、解码、内容丢失与 timeout 不是这两次拒绝的根因。CLI startup 日志的 `not logged into Antigravity` 也出现在成功请求启动阶段，随后 login/模型请求完成，不能据它认定过期凭据造成这次故障。

初次修复：拒绝返回 HTTP 502 / MCP isError；原收据使用 `audio_analysis_refused`。后续统一为 `provider_content_filter`，保留 before/after attachment reads 阶段、conversation_id、网关 bridge request_id、模型、已完成读取数和真实失败 provider usage 到 sidecar 日志。没有静默删附件、改问法或自动重试。两个文件读取都完成是传输条件，仍不等于听觉判断准确。

Google 原始安全分类、blockedReason/finishReason 和触发特征没有在当前 CLI 协议/所存记录中暴露。**已定位到订阅层过滤拒绝和网关假成功缺陷；具体过滤触发原因未知**。没有足够证据断言是某个词、两段音频、版权或网络问题，也不能保证该上游拒绝从此不会发生。协议若公开原始拒绝原因，应直接保留它，而不是增加猜测和特殊绕过逻辑。

## 用户要求继续修复后的终态校验补丁

检查 `8f57e88` 的实际代码并回放 CLI 已观察到的过滤诊断，发现上一轮识别只位于音频路径，普通文本仍将该诊断返回为答案；开头多一个空白即可绕过音频拒绝识别，纯空白正文也被当作完成。基线回放确认普通文本拒绝与纯空白均被接受。这是确定的网关缺陷，和未知的 Google 过滤触发条件分别处理。

拒绝识别现在由 bridge 的共享终态正文校验负责；音频调用同一函数并提供读取阶段。只识别当前 CLI 的已观察诊断前缀，忽略其前导空白，不按“不能”“无法”等普通措辞猜测拒绝；正常答案中引用诊断文字仍保留。空白正文明确失败。私有错误体增加 `code=provider_content_filter`、phase 和 conversation_id，公开协议继续由 LiteLLM 处理，现有 handler 保留诊断标记并将 HTTP 502 传至 MCP。没有新增服务、依赖、重试或 fallback，也没有修改输入或隐藏提供方拒绝。

使用原固定部署镜像的临时 QA 容器，六个受影响文件合计 **220 passed / 3 warnings**。新增回放覆盖普通文本、Advisor、schema、带空白诊断、音频读取前/后拒绝、正文为空白、正常引用诊断，以及 MCP 的 ToolError 与只调用一次。基线原代码回放和新代码回归都使用 fixture，不能称为 Google live 过滤测试。Sidecar 已按 Compose 重建并更新，部署与源码 bridge SHA-256 均为 `cae7c2739c57bd29b7fce36464d29c17ce4b131140e73764cad17ac78cf1d124`。本轮无 UI 变更；主执行者另做复查，未做独立评审。

真实 MCP 使用与先前拒绝一致的两个口令 WAV 和同一问题复验。[本轮 production 收据](gemini-mcp-production-2026-09-30-1790791667.json)：单文件正确转写，11.877 秒、5,325 provider tokens；双文件 11.434 秒返回 HTTP 502 / MCP isError，正文保留 `provider_content_filter phase=after_attachment_reads` 和 conversation_id `bde4773d-93b9-48e1-9d73-2ba7f8abc40f`。Sidecar 日志确认 completed_reads=2、失败 provider usage=4,012 tokens、bridge request_id `1759182a92cb4798ab26ada38b96c52c`，未额外调用或改写请求。保存策略未变、readiness 200。LiteLLM 错误附带其“未找到 fallback”说明，但该 Gemini 音频组没有配置 fallback，不能把这段说明当作实际调用了其他模型。

复查结论：**共享终态的假成功缺陷已修复并部署；Google 的双文件过滤拒绝仍实际发生，未解决**。这是部分修复，不能宣称偶发错误全部消失。CLI 帮助、当前 headless 协议和官方 changelog 未提供本通路可用的原始分类或过滤控制；继续解决上游拒绝需要提供方暴露原始 stop reason/修复拒绝行为，或经用户决定改变提供方策略。本轮保留订阅与输入合同，没有用重试、切模型或删附件掩盖它。单次复验通过只能证明该次通路正常，不能推断提供方以后不会过滤。

## Skill learning

Northstar / Northstar QA 当前本地版本，版本号 unknown。预期 native Gemini 能听音频，观察到模型有能力但 gateway 文本运输没有附件，且 Messages 的 200 隐藏了丢失。盲口令比泛泛询问“听到什么”更能验证传输；真实 MCP 的两入口已复查，并进一步暴露 CLI SUCCESS 包装过滤拒绝的问题。验收应同时检查输入字节、读取完成、终态内容与 provider usage，保留音乐描述失败可防止把转写能力当作专业混音能力。本轮继续修复表明终态校验应由共享协议边界拥有，不能只加在一个输入分支；真实双文件仍拒绝，所以必须区分网关修复与上游问题解决。该教训可用于 [Northstar Issues](https://github.com/stancsz/northstar/issues)，未发送外部消息。

官方依据：[Gemini 音频输入](https://ai.google.dev/gemini-api/docs/audio)、[AGY headless 协议与权限](https://antigravity.google/docs/cli/headless/)、[CLI 音频附件更新](https://antigravity.google/docs/changelog?tab=cli)、[权限 deny 规则](https://antigravity.google/docs/cli/permissions/)。模型/API 文档不能代替本仓库 Subscription 的运行证据。
