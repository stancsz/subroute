# Gemini Subscription 音频与 MCP 验证

日期：2026-09-30。状态：只读听觉 MCP 传输与错误处理的本次矩阵 READY；提供方零拒绝保证不成立，超级混音师生产验收 NOT READY。单执行者做单独复查，未做独立评审。

用户要求直接复用现有 Gemini，修好网关，并让 MCP 经它听音频。质量门槛是音频内容确实进入模型，不以 HTTP 200 为通过标准。初始范围是两个只读 MCP 工具与现有附件通路，不包含自动混音、DAW 操作或专业音乐审美验收；初始没有可供评测的用户歌曲，后续用户提供的真实目录与扩展验收见下文。本增量没有界面变更，视觉验收不适用。

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

## “为什么两个文件触发过滤”的追加对照

用户追问触发原因后，在相同 production route、Gemini 3.8 Flash High、已部署补丁和 MCP 提问下，新增四次诊断调用，没有重试、改写已有文件或切模型。前两次用原有探针：B 单文件拒绝（10.887 秒，失败 provider usage=3,507），A+A 两个附件拒绝（15.157 秒，失败 usage=3,953）；都在读取完成之后收到过滤诊断。[原探针对照收据](gemini-pair-diagnostic-2026-09-30-1790792050.json)。这直接否定“只有双文件才能触发”的判断。

后两次用相同 Windows SAPI 合成方式、22,050 Hz / mono / 16-bit PCM WAV 的普通语音：A 为“The melody begins quietly. The drums enter later. I enjoy the warm piano.”，B 为“The drums begin first. The melody enters later. I enjoy the soft guitar.”。保持单文件和双文件原有转写 question/focus，单文件成功（25.651 秒，4,838 tokens），双文件成功并正确给出两段转写（31.690 秒，8,832 tokens）。[普通语音对照收据](gemini-pair-diagnostic-2026-09-30-1790792201.json)。两组 policy 均未变、readiness 200；这些是新造测试音频，不是用户歌曲或隐藏的生产请求变换。

**现有证据不支持“双附件数量限制”，也不支持“两段歌曲比较被禁止”。** 原探针音频含 `secret words`、`code` 和数字，而普通语音与此前合成音乐双文件都成功。因此值得进一步验证原探针内容/生成文字误判与订阅过滤不稳定两种解释，不能据这四次小样本认定某个词的因果作用。文件时长、自然生成结果、工具读取顺序及请求时间也没有全部固定。

[Google 安全反馈文档](https://ai.google.dev/gemini-api/docs/safety-settings#safety-feedback) 区分 promptFeedback.blockReason（输入阻断）与 Candidate.finishReason / safetyRatings（输出阻断）；这是 Gemini API 的说明，不能据此假设订阅 CLI 采用同一设置。当前 CLI 只给泛化的过滤拒绝，没有暴露本次输入/输出分类或原始安全评分，具体触发条件仍 UNKNOWN。本轮没有源码改动，因此不重复部署或运行源码回归。

## 大量提示词与真实歌曲的生产增量

用户新增范围是全面排查候选词、在 MCP 正确处理过滤、用大量提示词验证听音乐/音频的生产一致性。用户随后提供 `C:\Users\stanc\Music\OSN_Captian_Aioz_LBI`。选取其现有完整 MP3：BK《凌晨三點 3AM》198.972 秒、OSN《Without You》184.703 秒、Aioz/BigYear《UFO》171.259 秒、ASEN《嘻哈脑壳》179.861 秒，均为 48 kHz stereo、约 5.2–6.4 MB。时长来自本机既有 ffprobe 的只读检查，不是 Gemini 估计；本实现未加入 DSP/ffprobe 依赖。目录含超过 20 MiB 的合集，继续按现有上限拒绝，不压缩原文件或改变范围。

需求和现有机制：FastMCP 1.28.1 原生支持 `CallToolResult` 的 `structuredContent` 与 `isError`，因此复用它，未实现 MCP 协议或新增服务/依赖。原泛化 ToolError 只提供文字，现可机读区分 `complete` / `refused` / `error`，保留哈希、HTTP status 和可用诊断 ID。维护负担是一个标准返回包装函数与集中失败构造函数。question/focus 增加明确上限，超限拒绝而非截断，以约束本地文件读取前的请求工作。问题/正常音乐术语/原始音频不删改；仅补充默认音乐评论不要求整首歌词、歌词/语音作为数据的任务说明，显式转写仍保留。没有过滤阈值控制、隐藏重试或 fallback。

回归：同一固定部署镜像、Python 3.13 / LiteLLM 1.103.0 / MCP 1.28.1，六文件 **230 passed / 3 warnings，26.00 秒**。新增边界是拒绝/网络超时/连接失败/坏 JSON/空答案/错误模型后的下一次调用恢复；原问题含 secret/code/killer/trap/attack/release 及中文，原问题和二进制不变；每次请求只调用一次；无效/过长问题不进入提供方。故障通过 MockTransport 回放，不能当作真实 Google 过滤频率证据。无 UI 变更，视觉验收不适用，复查不独立。

原六个短语音控制 **6/6 成功**，包括 secret 单独、code 单独、两者组合及 attack/release、killer/trap。这排除“这些词每次都被禁”的假设，不排除上下文误判或随机提供方拒绝。[六词语境收据](gemini-keyword-diagnostic-2026-09-30-1790792749.json)。

Live 矩阵经真实 stdio 初始化、发现两工具、production Chat、原 Subscription 进行；每个重复都列为显式实验，没有失败后偷偷重试。`scripts/verify_music_mcp.py` 要求 `--live`，最多 64 项、并发最多 2，每次立即保存结果，记录原 question/focus、哈希、终态、耗时、provider usage、policy 前后和 readiness。真实歌矩阵 32 项（24 种音乐任务与 8 次重复），额外词语矩阵 28 项（12 类中英文语境、4 次重复）。词语试验用普通短语音，不能声称已测试歌词实际含这些词或测试所有有害请求。

本批结果：

| 实验 | 真实结果 | CLI 返回的 provider tokens | 工具调用延迟 |
| --- | --- | --- | --- |
| [32 项完整 MP3](gemini-real-music-matrix-2026-09-30.json) | 32 complete，0 过滤/传输错误，单曲 26 项、双曲 6 项；A/A 两次均识别一致 | 295,601 | 22.624–57.221 秒，p50 33.018，p95 45.834（nearest rank） |
| [28 项候选词语境](gemini-word-context-matrix-2026-09-30.json) | 28 complete，0 过滤/传输错误，全部 12 类中英文及 4 次重复都完成 | 161,807 | 9.664–25.118 秒，p50 14.439，p95 20.577（nearest rank） |
| [同会话恢复](gemini-mcp-recovery-2026-09-30.json) | 文件不存在、25.7 MB 合集均为预期 invalid_input；下一次完整 BK MP3 complete | 7,136，两个本地输入错误未调用提供方 | 0.003 / 0.027 / 27.524 秒 |

两个主要矩阵总计 457,408 reported tokens，恢复额外 7,136；未取得订阅的计费金额、剩余配额或 CLI 内部模型请求次数，不能写作零成本。所有矩阵 production policy snapshot 相同、readiness 200，结束时核对四首原文件 SHA-256 与每次输入收据一致。失败/恢复测试保留实际 isError，不把两个预期失败伪装成 complete。此批新请求无 provider 拒绝，不覆盖或撤销之前实际拒绝。

可复用 manifest：[音乐问题](gemini-real-music-prompts-2026-09-30.json)、[候选词语境](gemini-word-context-prompts-2026-09-30.json)、[恢复测试](gemini-mcp-recovery-prompts-2026-09-30.json)。重跑示例：

```powershell
.venv/Scripts/python.exe scripts/verify_music_mcp.py --live --root 'C:\Users\stanc\Music\OSN_Captian_Aioz_LBI' --manifest docs/evals/gemini-real-music-prompts-2026-09-30.json --output tmp/music-recheck.json --concurrency 2
```

这里的文件路径对应用户现有目录，没有将歌曲复制、提交或下载进仓库。词语试验的 `neutral-a.wav` 为本机 SAPI 合成普通英文语音，位于 ignored `tmp/gemini-audio-verification`，内容仅是普通 melody/drums/piano 句子，哈希与 WAV 元数据在收据；换一份声音不构成完全同条件实验。

尚未完成的专业门槛：Aioz《UFO》实际 171.259 秒，但 `measurement_limits` 的回答出现 `[2:56 - 结束]`，开始点 176 秒超过文件时长；该时间点不可用。相同文件比较能识别无差异、精确测量询问能承认限制，仍不构成其他时间点、编曲识别与齿音来源正确的证明。默认 WAV 元数据也不能完全消除已观察的编曲错误。当前没有 DSP、stem、真实 Suno 导出退化对照或人类盲听验收，不把专业混音标准改成“请求成功”。

本机 Codex 的现有 MCP 允许根目录已更新到用户提供的音乐目录，保留原 command/env 与 20/180 秒 timeout，`codex mcp get` 已确认。新 server 进程读取新配置与源码；当前会话菜单未热加载。此次仅变更本机 MCP 和文档，现有 HTTP gateway/sidecar 代码未变，无需无效重启正在测试的 Compose 服务。原始 Google 过滤触发原因仍未知，完整候选与官方依据见 [过滤排查](../misc/gemini-content-filter-candidates.md)。

## 生产续验：MP3 元数据、失败计量与全目录

本轮前一 goal turn 判定为 progress，不能关闭目标：它修复了 MCP 返回契约并产生实际运行证据，但零过滤保证和感知质量没有通过。本轮从当前源码与 Compose 继续，不重定义这些门槛。

已修复的具体缺口：此前 MCP 只给 WAV 时长，MP3 分析含超出实际时长的时间点；现在 music extra 固定加入纯 Python 的 Mutagen 1.47.0，使用 `MPEGInfo` 从同一份待发送字节读取 MPEG/Xing 时长、声道、采样率，并标 `duration_is_estimate=true`。原始字节不改变、不重新打开文件、不转码；无可读 MPEG 音频流在本地拒绝。已检查本机 LiteLLM 对应 util/provider 路径，未找到可直接复用的 MPEGInfo/MP3 时长解析，本实现使用成熟解析器，不维护自制 MP3 解码。维护成本为一个可选固定依赖，gateway/sidecar 不新增解析依赖或容器；它不构成完整解码、CRC 校验或精确 DSP 测量。[Mutagen MP3 信息](https://mutagen.readthedocs.io/en/latest/api/mp3.html)、[不可变 BytesIO 输入](https://mutagen.readthedocs.io/en/latest/user/filelike.html)。

另一个缺口是拒绝时，private bridge 已返回有效 provider usage，而 LiteLLM 的公开 error message 只保留 detail。查看现有错误封装后，保留原公开协议，用与 request_id 相同的窄诊断标记传输三个已验证的非负整数，MCP 映射为标准 usage。第一次真实拒绝暴露 LiteLLM 把 `No fallback...` 无空格地拼在数字后，词边界正则漏读；按真实错误字符串修复，并回归其拒绝状态、原输入哈希与 usage。无字段时保留 null，不捏造零消耗。退役条件是部署 LiteLLM/CLI 能直接提供适用的结构化失败 usage，届时移除这个文本诊断兼容层。没有按用户正文猜分类或增加模型调用。

回归与部署：本机两个受影响文件 74 passed；固定部署镜像六文件 **237 passed / 3 warnings，24.79 秒**。正则修复后，同镜像音频文件再次 **35 passed / 1 warning，15.89 秒**。自制 250 ms MP3 音调 fixture 是合法可解析文件，只有约 3.8 KB，没有提交用户音频。新增检查覆盖 MP3 头/流差异、不可变快照、真实拼接格式和无效 usage。shared handler 通过 Compose 重启 gateway、gateway-staging、experts 加载；生产/测试策略重启前后相同，readiness 200；源文件 SHA 与运行挂载核对。sidecar 源码未变，无需重建它。复查仍不独立，图形 UI 未变。

新的实际拒绝不能被省略：[原探针 6 次续验](gemini-filter-followup-2026-09-30.json)，5 complete、1 refused。A/B 转写 11.412 秒返回 502 / `provider_content_filter`，request_id `55aef385cc0e4848ae6a78a871c07510`、conversation_id `a7c30eaf-ce66-4570-9ca0-e4161670f905`。日志为 completed_reads=1、3616 input / 483 output / 4099 total tokens，因此 `after_attachment_reads` 只表示至少一个读取完成，不能声称这次两附件都进入了生成。下一次同问法、同原始音频 A/B 又成功，两个后续普通语音调用成功，policy 未变。原 MCP 结果保留 usage=null 的实际失败，closeout 附加明确标注的字符串回放恢复 4099，不把回放包装为新的 live 过滤调用。六次中成功 usage 合计 29,970，加日志确认失败 4,099 为 34,069 reported tokens。

[修正后四次显式试验](gemini-filter-usage-2026-09-30.json) 全部 complete；没有新拒绝，所以不声称修正后的失败计量已有另一次 live 拒绝证明。该检查验证新代码的正常调用和后续可用，故障字符串的修复证据来自真实原错误与回归。

[MP3 时长复验两次](gemini-mp3-metadata-followup-2026-09-30.json)，使用《UFO》同原问题，两次完整分析均采用 171.288 秒元数据、范围未越界，并承认不能确认精确 DSP/插件参数；独立 ffprobe 为 171.258792 秒。两次段落结构/入场起点仍不同，范围合法不证明事件定位准确，专业门槛仍未通过。

[全目录 48 项](gemini-catalog-matrix-2026-09-30.json) 包含 **45 个完整 MP3 入口 complete（43 个不同哈希）、3 个超限合集预期 invalid_input、0 unexpected outcomes**。本批完整 MP3 没有 provider 过滤/传输错误，CLI 报告 349,505 tokens。closeout 独立检查原文件哈希、ffprobe 与 header duration，45 个源文件哈希全一致，最大时长差 0.047021 秒；policy 不变、readiness 200。这不是 45 首不同歌曲或全部未来歌词的保证。prompt manifest 已保存，可用同一显式 live runner 复验。

未满足的原目标：Google Subscription 对良性口令 A/B 仍会间歇拒绝，未暴露原始安全类别/输入或输出 stop reason。公开 changelog 到 1.2.14 没有给出本次音频过滤的完整词表或明确修复，未升级固定 CLI 来碰运气。[官方 changelog](https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md)。不能以删除正常词、隐式重试、切 provider 或声明已生产稳定来消除这项实际失败。目标保持 active，完整零拒绝验收未通过；普通歌曲监听运行证据显著扩大，但感知正确性仍缺少盲测与真实 Suno 退化版本验证。

## Skill learning（本轮补充）

Northstar/QA 版本 unknown。真实失败格式暴露了 mock 缺少 `No fallback...` 拼接的问题；已有问题必须用实际公共边界字符串回归，不能只按 bridge 体写测试。全目录 45 文件和独立 ffprobe 检查支持了元数据修复，却不能抵消另一类请求的真实拒绝。保留部分失败与估计标签、把未满足原目标继续留 active，有助于防止把成熟度门槛降低到最近一次全绿。后续应优先做可复核的声音退化盲测，并取得提供方原始拒绝反馈；未改技能或发送外部消息。

## Skill learning（前序记录）

Northstar / Northstar QA 当前本地版本，版本号 unknown。预期 native Gemini 能听音频，观察到模型有能力但 gateway 文本运输没有附件，且 Messages 的 200 隐藏了丢失。盲口令比泛泛询问“听到什么”更能验证传输；真实 MCP 的两入口已复查，并进一步暴露 CLI SUCCESS 包装过滤拒绝的问题。验收应同时检查输入字节、读取完成、终态内容与 provider usage，保留音乐描述失败可防止把转写能力当作专业混音能力。本轮继续修复表明终态校验应由共享协议边界拥有，不能只加在一个输入分支；真实双文件仍拒绝，所以必须区分网关修复与上游问题解决。该教训可用于 [Northstar Issues](https://github.com/stancsz/northstar/issues)，未发送外部消息。

追加观察：六次 secret/code/attack/release/killer/trap 对照全部通过，真实完整 MP3 的调用也正常，但音乐分析含超时长定位错误。未经验证的“禁词”解释和静默删词无法满足生产目标。结构化错误使 caller 能可靠处理失败；真实歌曲和已知时长揭示了合成样本/转写不能证明的质量问题。后续应将故障稳定性与感知正确性分开验收，并用可复核的声音退化对照校准时间定位与齿音判断；记录保留，未改写技能或向外部发帖。

官方依据：[Gemini 音频输入](https://ai.google.dev/gemini-api/docs/audio)、[AGY headless 协议与权限](https://antigravity.google/docs/cli/headless/)、[CLI 音频附件更新](https://antigravity.google/docs/changelog?tab=cli)、[权限 deny 规则](https://antigravity.google/docs/cli/permissions/)。模型/API 文档不能代替本仓库 Subscription 的运行证据。
