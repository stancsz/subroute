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

## Optional LAN agent coordination

[Subroute Relay](docs/operations/relay.md) adds a separate Docker-hosted MCP workboard and outbound runners for Claude Code sessions across machines. It tracks bounded goals, assignments, leases, cancellation and separate review evidence. An optional event-driven headless supervisor advances active goals using the Northstar contract, with explicit cycle and Advisor call budgets. See the [device A server and device B Docker client setup](docs/operations/relay-devices.md) and [pilot verification limits](docs/evals/relay-2026-10-23.md). It is separate from the gateway's routing/runtime state. Device B needs Docker, a private workspace, runner config and credentials; the default leaf client does not require a Northstar skill bundle. Reproduce the provider-free fault checks with `python scripts/verify_relay_docker.py`.


## Codex Advisor skill

The former **Luna Advisor Escalation** skill has moved to the central [Northstar package](https://github.com/stancsz/northstar/tree/main/skills) and is now called **Codex Advisor**. Install the four-skill Northstar package for `northstar`, `codex-qa`, `codex-subagents` and `codex-advisor`. See the [installation guide](https://github.com/stancsz/northstar/blob/main/docs/misc/install.md) and [Codex Advisor instructions](https://github.com/stancsz/northstar/blob/main/skills/codex-advisor/SKILL.md).

Subroute still owns the `experts` service, provider authentication and gateway runtime. Moving the skill does not move or deploy those services. Historical advisor evaluations below describe the Subroute revisions at which they were recorded.

The dedicated `experts` endpoint exposes the configured GPT-6 and Gemini expert aliases through the optional Advisor tool. Northstar can make explicit expert calls; those are separate from ordinary target requests. The service has no retry or model fallback. Text requests are compacted by Sift on 4040; supported image payloads pass through unchanged. See [image-input verification and limits](docs/evals/advisor-image-input-2026-09-30.md).

## The problem

Your coding tools remember an endpoint and model. Your AI provider, account, or preferred route can change. Updating every client each time is tedious and makes it harder to know which connection will handle the next request.

**Subroute gives compatible clients one local endpoint. Set the model to `auto` for prioritized automatic routing.**

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

The [durable usage guide](docs/operations/usage-accounting.md) describes provider receipts, customer attribution, protected Cloudflare transfer, recovery and remaining limits. Its [acceptance record](docs/goal/usage-accounting.md) covers the 2026-10-11 serving release, real API calls and durable transfer into both Cloudflare environments.

Observed streams settle usage through their parser and close observer. LiteLLM's duplicate completion callbacks do not write those receipts again, including the native Responses callback that runs in a separate event loop. A version-bounded formatter repair also lets LiteLLM count tool arrays whose JSON Schema omits `items`, without changing the schema sent to the provider. Context checks and Sift share that repaired counter. See the [staging verification](docs/evals/counting-and-receipts-2026-10-15.md).

Normal model requests use Subroute routing and LiteLLM/provider admission without Sift compaction or long-history instruction injection. Input, tools and requested output limits remain caller-owned apart from documented provider translations. Requests must fit the selected provider's configured capacity; use client-side compaction before sending larger histories. Sift runs only on the dedicated 4040 experts service after the model chooses the optional Advisor tool. It compacts expert text input toward 25,600 estimated tokens and leaves multimodal payloads unchanged for native capacity checks. Every expert has a 32,000-token input ceiling. Generation is separate from that input budget; Gemini is retained without an output-token cap. Sift response headers label token counts as estimates and expose whether the private request was compacted. See the [follow-up evidence and limits](docs/evals/simplicity-followup-2026-10-23.md).

## Get started

Make credentials available as environment variables for the providers you plan to use, then start the production gateway from the repository root:

```powershell
docker compose build gateway
docker compose up -d gateway experts
```

Set `EXPERTS_API_KEY` in the local `.env` file before running Compose. The gateway uses it to authenticate its private calls to port 4040; the experts listener also requires it.

Open the control desk at [http://127.0.0.1:4000/control](http://127.0.0.1:4000/control), choose a configured route, then point a compatible coding tool to:

```text
Base URL: http://127.0.0.1:4000/v1
Model:    auto
```

Set a strong `GATEWAY_MASTER_KEY` in the local `.env` file before starting the pinned LiteLLM runtime. API clients then send `Authorization: Bearer <GATEWAY_MASTER_KEY>`. This also enables the LiteLLM Admin UI, using `UI_USERNAME` and `UI_PASSWORD` (Compose defaults: `admin` and `password`). For deliberate local development without a master key, LiteLLM requires the explicit native setting `LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY=true`; Compose passes it through without enabling it by default. That mode has no bearer authentication or Admin UI login.

For eligible text-only Chat Completions, Responses, and Anthropic Messages requests, any provider failure uses the same catch-all chain: OpenRouter's MiniMax M3.1 Flash, Xiaomi MiMo V2.6 Pro, then GPT-6 Luna. The client-visible `model` remains the originally requested target, and fallback does not change the saved routing selection. LiteLLM reports fallback attempts in its headers and internal request metadata. Media, image generation, speech, Advisor calls, and other specialized operations keep their own routes. A failed provider call may still incur cost. The fallback is a different model/provider, so identical output behavior is not guaranteed. Available models are listed in this checkout's [`config/litellm.yaml`](config/litellm.yaml).

For direct MiniMax M3.1 Flash, select `minimax-flash-direct`; it uses `MINIMAX_API_KEY` and the same medium default / caller effort / saved-desk precedence as the OpenRouter Flash route, without joining the fallback chain. Provider compatibility has not been live-verified.

Gemini 4 Argon is available as the selectable `gemini-subscription-argon` target and Advisor through the authenticated Antigravity CLI, in production and staging. Expert calls have a configured 32,000-token input ceiling and Sift targets 25,600 estimated input tokens. The owner-defined 32K limit applies to input only, across every expert; output is separate and Gemini remains available. Target requests retain Subroute's 256K context budget. Expert calls default to low reasoning effort and do not join target fallback chains. Provider availability can still vary.

Speech routes are available through LiteLLM. MiniMax TTS uses LiteLLM's standard `POST /v1/audio/speech` endpoint with model `minimax-tts` and `MINIMAX_API_KEY`:

```json
{"model":"minimax-tts","input":"Hello from Subroute.","voice":"alloy"}
```

MiMo ASR uses its documented Chat Completions format, not LiteLLM's standard `/v1/audio/transcriptions` format. Send `POST /v1/chat/completions` with model `mimo-v2.5-asr`, and put an OpenAI `input_audio` block (`format` and base64 `data`) in a user message. `asr_options` can be passed as an additional request parameter. Subroute preserves this explicit ASR model and audio payload, even when the control desk forces another model, and skips the saved advisor for that speech request. Set `XIAOMI_TOKENPLAN_API_KEY` to enable MiMo routes. MiMo TTS models continue to use Xiaomi's Chat Completions format at `/v1/chat/completions`; put the spoken text in an `assistant` message and specify output options in `audio`.

### Use the Electron app

With the gateway running, launch the desktop control desk:

```powershell
cd desktop
npm install
npm start
```

On first launch, connect it to a responding loopback Subroute gateway. Desktop stores the gateway address locally, not provider API keys. Its optional agent launcher starts supported coding CLIs with session-only Subroute settings. See [desktop/README.md](desktop/README.md).

### Keep production and staging separate

Compose exposes production on `127.0.0.1:4000` and staging on `127.0.0.1:4005`. Staging has its own database and routing policy. Gateway, staging and experts use the same locked image recipe, containing application code and the three static runtime configuration files. Source edits take effect only after rebuilding and recreating the selected service; running containers retain their own image revision. Production still saves policy to the existing host `config/active_model.json`, mounted at `/app/legacy-config`; staging saves policy in its dedicated state volume. Credentials remain external to the image.

```powershell
docker compose build gateway-staging
docker compose up -d gateway-staging
```

## Routing, without surprises

Codex subscription targets request provider reasoning summaries and preserve them separately from answer text and tool calls. Claude-compatible clients receive standard `thinking` blocks and `thinking_delta` events when the provider returns a summary. The selected reasoning effort is retained. A client's output limit still covers thinking and answer output together.

Ordinary Codex subscription requests preserve output ceilings through LiteLLM's native Responses mapping. If more than one output-limit field is supplied, the smallest applies. This bridge requires integer limits of at least 16 tokens; it rejects smaller or invalid limits with HTTP 400 before calling Codex instead of increasing or discarding them. Normal configured fallbacks still apply. Omitted limits retain provider defaults.

Claude Code requests to Xiaomi and other OpenAI-compatible backends use LiteLLM's Chat Completions bridge. Native Anthropic/MiniMax Messages and subscription handlers retain their existing transports. A narrow streaming callback fixes LiteLLM 1.101.0/1.103.0 duplicating an unsigned thinking opener into the first delta, only when those two values match exactly. Signed thinking, redacted blocks, tool arguments, actual newlines and literal code escapes remain intact. The provider-adapter regression matrix is in `tests/test_thinking_format.py`; local wire fixtures verify protocol behavior without provider spending. Provider-generated literal `\n` text is preserved, since decoding it indiscriminately would corrupt code and signed thinking.

Those versions also duplicate signed thinking signatures and collapse multiple/redacted reasoning blocks in Chat-to-Messages conversion. The existing adapter receives separate thought/signature fragments with explicit boundaries; buffered assembly preserves each block and trailing signature fragments. MiniMax Chat uses its native `reasoning_split: true` option to keep thinking out of visible answer text. Native Messages frames remain unchanged.

For Codex clients using Responses with a Chat backend, a separate version-bounded patch repairs LiteLLM's item and part lifecycle. Reasoning and message items have unique indices, complete openers (including the required reasoning `summary` array), and matching terminal snapshots; reasoning summaries remain separate from answer content. It keeps LiteLLM's transport, typed events, usage and opaque replay data. Native Responses handlers retain their own lifecycle. Remove these workarounds when the upgraded dependency passes `tests/test_client_stream_contracts.py`; `tests/test_live_coding_clients.py` checks actual Claude Code and Codex tool turns directly through standalone Subroute on staging port 4005, with an optional local-forwarder run. See the [verification report](docs/evals/claude-code-thinking-format-2026-10-06.md) for fixture versus live coverage.

Those LiteLLM versions also mix the argument fragments of interleaved parallel Chat tool calls when converting them to Messages. Subroute applies a version-bounded shim to the existing async Chat chunk splitter: reasoning/text before tools remains live, and the suffix beginning at the first tool is held until the provider finishes. Tool calls are assembled by their original index, all inputs are validated before exposure, and the suffix's content order is preserved. LiteLLM still emits the public protocol. No extra provider calls occur. The shim leaves native Messages and public Chat/Responses streams alone; remove it when an upgraded LiteLLM passes the interleaved-tool regression. See `src/subroute/thinking.py` for the verified gap and retirement condition.

Actual Claude Code, Codex, OpenCode and Cline CLI formatting tests run directly against standalone Subroute. Across MiniMax, OpenRouter, MiMo Pro, Codex subscription and Gemini subscription, they verify real file-tool execution and replay, separate thinking, complete stream lifecycles, Unicode, literal code escapes, Markdown and unchanged files. The live tests require `SUBROUTE_RUN_LIVE_TESTS=1`, use isolated client settings and restore staging policy. Non-live Python socket connections to cloud addresses and known deployed gateway ports are rejected; provider fallback fixtures mock both lanes. See the [20-combination receipt](docs/evals/subroute-standalone-format-2026-10-06.json). Recorded upstream overload and generated-content failures remain visible alongside bounded recovery checks; these samples do not guarantee provider availability or flawless output. Native terminal folding and Cline VS Code/Desktop rendering are unverified.

All OpenAI target and Advisor routes use GPT-6 or newer: Luna, Sol, Astra and GPT-6.1 Sol. GPT-6.1 Sol is available as the target alias `codex-gpt-6.1-sol`. GPT-5.6 Terra and the GPT-5.6 Luna reserve route have been removed; saved selections migrate to GPT-6 Sol and GPT-6 Luna respectively. These are selectable models, not a model lock. New routing state defaults to GPT-6 Luna and GPT-6.1 Sol/low. Clients that request model `default` follow the saved routing-desk target in either alias, force or off mode; if no state file exists, GPT-6 Luna is the default target. `ACTIVE_MODEL` and `ADVISOR_MODEL` override initialization; persisted state takes precedence on subsequent starts. Production retains its saved `force` mode; staging retains its own policy.

The routing desk offers **Speed: Fast** when the target is an OpenAI subscription model. It saves `fast_mode` in the target policy and sends `service_tier: "fast"` only for requests routed to that target. The Codex subscription endpoint currently rejects that spelling, so the narrow subscription transport translates it to the equivalent `priority` tier. The advisor, explicit model requests in alias/off mode, `auto`, and image/audio routes do not inherit the saved target speed. Fast mode uses more OpenAI subscription allowance. Existing saved policies default to Standard, and changing the target clears Fast mode. Remove the transport translation when the subscription endpoint accepts `fast` directly.

- **`alias`** is the default. It resolves `default` using the saved route. `auto` always uses the prioritized provider chain.
- **`force`** applies the selected route to known model requests except explicit `auto` requests and image generation.
- **`off`** leaves model dispatch to LiteLLM.
- **Advisor** is optional and model-selected on Anthropic Messages. The gateway advertises one native Advisor tool using the independently saved expert choice; it makes no provider call or guidance injection unless the target model chooses that tool. Chat Completions and Responses do not receive the tool. A selected consultation is limited to one use and fails visibly without retry or fallback.

The selected Advisor model is saved independently in each gateway environment: production uses `config/active_model.json`, while staging uses its dedicated `gateway-staging-state` volume. Choose a configured Gemini or OpenAI expert in that environment's control desk; startup loads the saved choice, so production and staging can differ. Tool consultations default to low reasoning effort. For Codex experts, 4040 passes a 1,024-token `max_output_tokens` request to the subscription endpoint; a bounded live probe verified enforcement at 16 tokens for GPT-6 Luna only. Other Codex expert models remain unverified. Antigravity exposes no corresponding output limit. The native Messages tool handler does not forward saved per-call reasoning metadata. The service has no retries or fallback chain, so a failed consultation does not trigger a second provider call.

The pinned LiteLLM Router can replay the whole tool loop after an expert failure or a final target failure following successful advice. A narrow guard observes native Advisor selection using request-local context and propagates subsequent errors without parent fallback, keeping the consultation budget within one attempt. Initial target failures before selection still use their configured fallback chain. The separate lifecycle error attribute preserves native target deployment-health classification. Remove the guard when LiteLLM's native handler and Router jointly enforce consultation budgets across failures.

The expert request contains only the target conversation already available to the model-selected native tool, and 4040 compacts its text input before provider dispatch. The tool may still receive a long target history because native orchestration sends the current Messages conversation; Sift targets 25,600 estimated tokens and may relax to the configured 32,000-token input ceiling. Codex receives a requested 1,024-token output limit, with enforcement verified at 16 tokens on the current GPT-6 Luna route only; other Codex models remain unverified, and Antigravity exposes no output limit. The 32K requirement applies only to expert input, not combined input and output. LiteLLM's native Messages Advisor handler buffers the target call before returning a synthetic stream, even when the caller requests streaming. This affects Messages requests advertising the tool; Chat and Responses retain their ordinary streaming paths. Token estimates and provider context accounting can differ; native LiteLLM/provider admission remains the final check.

Subroute configures no automatic retries. `auto` and the listed MiniMax/OpenRouter/Xiaomi routes use explicit LiteLLM-managed fallback chains. Advisor tool calls use the dedicated 4040 endpoint with an explicit empty fallback chain. Failed provider calls and their costs remain possible and are not hidden retries.

### Generate an image

Clear image-generation intent in Chat Completions, Responses, or Anthropic Messages routes to **GPT-6 Luna through the Codex subscription** and exposes its hosted `image_generation` tool. The Images generation endpoint follows the same rule. This takes precedence over `force`, `alias`, `off`, and the request's model (including `auto`). It does not change the saved text/advisor policy and never falls back to another provider or consults the saved advisor.

For example, send `{"model":"auto","messages":[{"role":"user","content":"Generate an image of a blue robot"}]}` to `/v1/chat/completions`. The latest user turn is checked for direct English/Chinese requests such as “generate an image”, “create a logo”, “draw a cat”, or “帮我生成一张图片”. Code, quoted examples, prior turns, image analysis, and explanatory questions do not trigger it. This is a conservative local pattern matcher, not a paid semantic classifier. For unrecognized wording, explicitly supply `tools: [{"type":"image_generation"}]` in Chat/Responses. Selecting an image tool through `tool_choice` also triggers the exception; the exact function names `image_generation`, `generate_image`, and `create_image` are translated to the hosted tool. Merely listing one of those custom functions does not reroute an unrelated request.

With direct intent and no caller tool choice, the image tool is selected. The whole-message `<user_input mode="act|plan|yolo">...</user_input>` envelope used by Cline Desktop is recognized for intent detection; the original message is still forwarded unchanged. Arbitrary XML or embedded examples are not unwrapped. Explicit `auto` permits Luna to decide whether to generate or request another tool first; `none` conflicting with image intent is rejected. Conversation history, instructions, and other function tools are preserved through LiteLLM's translations. Returned function calls remain the client's responsibility.

Chat returns generated images in `choices[0].message.images` (or `delta.images` for SSE) as data URLs. Responses returns `image_generation_call.result` as base64 in its output, including the final SSE snapshot. Messages carries a Markdown image data URL in a text block, whose rendering depends on the client. Buffered and SSE conversation requests both wait for complete generation before returning image content. Captions are retained. The shared limit is one image per response, two concurrent image requests per gateway process, and 180 seconds; usage reports Luna tokens, not total image-model usage or cost.

#### Cline Desktop

Register the MCP server as `subroute-image-generation`, so Cline advertises `subroute-image-generation__generate_image`. When that function is advertised, direct image intent stays on the client tool path: the chat model requests the MCP tool and Subroute does not eagerly return an image in `delta.images`. The MCP tool's separate Images request always uses GPT-6 Luna, regardless of the selected chat model or saved routing policy. Explicit hosted `image_generation` tools still use the existing Luna route. Add a global Cline rule requiring the MCP tool for direct creation requests and a server-level `timeout: 240` in Cline's MCP settings. The longer timeout covers the image endpoint's 180-second generation deadline and the MCP HTTP client's 190-second deadline; the default Cline timeout is 60 seconds.

Cline Desktop's OpenAI-compatible chat renderer does not display Subroute's `delta.images` field as an inline picture. Configure its MCP settings to launch `src/subroute/image_mcp.py` with this checkout's Python environment to add the `generate_image` tool. The tool calls the existing local `/v1/images/generations` endpoint and returns the PNG/JPEG/WebP through FastMCP's native image content, which Cline can render inline. After generation, expand Cline's collapsed `Worked for … and made … tool calls` activity to view the image result; the assistant's final text may only acknowledge the image. Keep the gateway available on `127.0.0.1:4000` and restart Cline Desktop after changing the MCP settings. Cline may ask to approve `generate_image` when it is first used. This is a client-specific bridge; the OpenAI and Anthropic API paths stay unchanged. See the [Cline Desktop image delivery evaluation](docs/evals/cline-image-delivery-2026-09-30.md).

For an Images API response, use `POST /v1/images/generations`:

```json
{
  "model": "codex-luna",
  "prompt": "A small blue circle on a white background",
  "n": 1,
  "size": "auto",
  "quality": "low",
  "response_format": "b64_json"
}
```

Decode `data[0].b64_json` to obtain the image. This endpoint returns a complete image, with at most two concurrent image requests per gateway process and a 180-second deadline. It rejects multiple images, URL responses, streaming, and legacy `style`/`user` parameters. Use `size: "auto"` or omit it: the subscription tool does not honor exact pixel dimensions, so fixed sizes are rejected before dispatch. For PNG outputs, `size` reports the dimensions read from the actual image bytes. Provider options are forwarded to the tool; provider support may vary. `luna_usage` is the reported Responses usage, and `image_usage` is unavailable unless supplied separately by the provider; neither is an estimate of total image-generation cost.

The gateway does not implement the Images edit/variation endpoints. Fixed dimensions are also rejected for explicit hosted image tools. See the [intent-routing verification](docs/evals/luna-image-intent-2026-09-29.md) for protocol evidence and the narrow LiteLLM compatibility workarounds.

## Know the limits

Normal Chat Completions, Anthropic Messages and Responses requests are not compacted by Sift. LiteLLM and the actual provider enforce configured input, output and context limits. Client-side compaction must keep longer histories within those limits. Sift runs only on port 4040 after the target model explicitly chooses the optional Advisor tool.

The experts service compacts text input toward **25,600 estimated tokens** and may relax once up to the configured **32,000-token input ceiling**. Codex experts receive a requested 1,024-token output limit; Antigravity has no output-token control. A live 16-token probe verified enforcement for the current GPT-6 Luna route only; other Codex models remain unverified. All experts share this input-only ceiling; output is budgeted separately. Oversized or irreducible text input fails at 4040; the ordinary target request is not rewritten. Multimodal expert payloads pass through unchanged and use native expert capacity checks.

The Desktop launcher uses a configured **256,000-token context window** for its `auto` profile and starts client-side compaction at **204,800 tokens** (80%). Relaunch existing terminals to pick up the new profiles. These are the current automatic route's configured budgets, not a universal provider-capacity guarantee. The gateway does not compact normal target history.

### Local Qwen runtimes

**Gaming trial status (2026-10-21):** Qwen is currently stopped and unloaded. The base profile failed its 30 combined tokens/s target and gaming coexistence. The lower-load `docker-compose.gaming.yml` is retained for reproducing a failed experiment, not as an accepted gaming default: microbatch 32 breached 60 FPS, while microbatch 16 made prompt processing impractically slow. The override uses `unless-stopped` to preserve a manual gaming stop; the base profile still uses `always`. See [the measured trials and current disposition](docs/operations/qwen-gaming-profile.md) before restarting inference alongside a game.

The repo supports two local Qwen routes: the Compose-managed CUDA/llama.cpp `qwen3.8-27b` service, and the host-managed Ollama `desktop` route (`qwen2.5-coder`). They use different models and runtimes. See the [local Qwen setup guide](docs/operations/local-qwen.md) for GPU prerequisites, startup commands, routing, verification, and troubleshooting.

The default `docker compose up -d` stack starts the text-only Unsloth Dynamic 3.0 `UD-IQ2_XXS` GGUF server. It downloads about 7.27 GB of weights into the persistent `qwen-local-models` volume on first start and listens on `127.0.0.1:8088`. The startup script pins and verifies the revision/SHA-256; Compose pins the llama.cpp CUDA image digest. The owner-approved profile uses two 40,960-token request slots (81,920 total), preserving the 32,768-token Sift/input target and 4,096-token output cap with headroom. All model layers and the input embedding are placed on GPU, with GPU Q4_0 KV, fitting disabled, reasoning disabled, six CPU threads and 128/32 batch/microbatch sizes. Compose caps container RAM at 12 GiB without swap and disables host prompt cache/context checkpoints. The 10 GiB VRAM target requires live monitoring; Docker provides no hard consumer-GPU quota or guaranteed compute share. Standalone allocation measured about 9.25 GiB, but both native terminal trials failed overall. Shared generation measured about 30 to 34 tokens/s; this excludes input loading and does not establish completed end-to-end throughput or coding quality. Gaming checks are explicitly deferred. See [the current budget and trial evidence](docs/operations/qwen-gpu-only-budget.md).

The optional Qwen route retains its configured 40,960-token request window, 32,768-token input cap and 4,096-token output cap. It no longer uses gateway Sift to admit larger histories. Native capacity checks and the configured fallback policy decide whether an unchanged request can proceed. The Qwen Anthropic system-prefix translation and version-bounded LiteLLM tool-schema counting repair remain separate provider-compatibility boundaries. Earlier compaction/cache measurements are historical evidence, not current normal-path guarantees; see [the historical evaluation](docs/evals/qwen-claude-domains-and-sift-2026-10-15.md).

Earlier Sift target-compaction and cache evaluations are retained in [the archived goal](docs/goal/archive/GOAL-2026-10-23-before-simplicity.md). They do not establish behavior for the 4040 expert boundary.

For Claude Code, set `ANTHROPIC_BASE_URL` to `http://127.0.0.1:4000`, set `ANTHROPIC_API_KEY` to the gateway's configured master key, and launch `claude --model qwen3.8-27b`. Use routing mode `alias` or `off` so the explicit model reaches the local service. The repeatable staging tasks are in `tests/acceptance/qwen_claude/`.

A stable endpoint does not make providers interchangeable. Tool use, streaming, vision, context size, reasoning options, and subscription access depend on each route. The Antigravity Gemini subscription integration supports text, PCM WAV/MP3 audio input and schema-constrained tool calls (audio and client tools cannot be combined). The sidecar sends prompts through the CLI's NDJSON stdin protocol, so long prompts do not consume OS command-line argument space. The client executes returned tool calls; the subscription sidecar does not execute them. The agent's internal finish control ends one runtime turn. In tool mode, Subroute reads AGY's native `structured_output` and checks its echoed schema; it never extracts decisions from prose. Automatic choice with parallel calls permitted can return one explicit JSON batch of up to eight calls. Every declared name and input is validated before exposing any tool. Mixed answers/tools, malformed results, undeclared calls, oversized batches, parallel opt-out violations, additional runtime turns and unexpected local tool execution fail closed. Provider usage is preserved. It does not support vision. Stream requests are returned as SSE after the CLI has completed the response, so tokens are not progressively streamed.

The normal Codex target adapter currently omits `max_tokens` and `max_output_tokens` under its existing compatibility policy, leaving its output limit to the backend. The separate Codex expert adapter forwards `max_output_tokens`; a bounded live GPT-6 Luna probe verified an exact 16-token stop with an incomplete response. This finding does not establish support for every subscription model or change the normal target adapter. Gemini Subscription also uses backend-managed output limits: its CLI exposes no output-token cap, so client caps are not enforced. This applies to buffered and SSE responses, including Anthropic Messages. Subroute preserves the completed response and actual provider usage rather than truncating output to imply that a cap was honored. Tool schemas must be self-contained; remote schema references are rejected before provider dispatch.

The gateway binds to loopback by default. Keep it on your machine unless you intentionally configure and secure a different network boundary.

Audio requests use Chat `input_audio`, route explicitly to Gemini Subscription and disable the saved advisor for that request, including in Force mode. The saved policy stays intact. At most two files and 20 MiB combined are accepted. Messages/Responses audio is rejected rather than dropped. The read-only [music MCP](docs/misc/music-listening-mcp.md) sends original audio to Google through this route; it does not perform DSP or certify mixing quality.

MCP refusals and transport failures return structured `isError` results, never successful listening reports. An explicit audio filter refusal may be retried by LiteLLM up to twice with the same question and files; persistent refusal remains visible, with no word deletion or model fallback. MCP reports the retry count; recovered responses label usage as final-attempt-only, while earlier failed-attempt usage remains in gateway/sidecar logs. The fixed 60-task sample reached at least 58/60 under this bound, which is not a guarantee for future traffic. See [filter candidates and evidence](docs/misc/gemini-content-filter-candidates.md); there is no verified complete keyword blacklist or zero-error guarantee.

The optional music client pins Mutagen 1.47.0 to inspect MP3 stream metadata from the uploaded byte snapshot; header-derived durations are labelled estimates. Verified refusal usage is preserved when available. These checks do not certify full decoding or perceptual accuracy.

## Project notes

- [Architecture and ownership](docs/misc/architecture.md)
- [Gemini music-listening MCP: two working tools and remaining mixing limits](docs/misc/music-listening-mcp.md)
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

**Subroute 为兼容的客户端提供一个本地固定入口。使用 `auto` 按优先级自动路由。**

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

启动当前固定版本的 LiteLLM 前，请在本地 `.env` 设置足够强的 `GATEWAY_MASTER_KEY`，API 客户端使用 `Authorization: Bearer <GATEWAY_MASTER_KEY>`。该设置也启用 LiteLLM 管理界面，使用 `UI_USERNAME` 和 `UI_PASSWORD`（Compose 默认值为 `admin` 和 `password`）。若明确需要本地无主密钥开发模式，需显式设置 LiteLLM 原生变量 `LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY=true`；Compose 仅透传，不默认开启。该模式没有 bearer 认证，也无法登录管理界面。

对于支持的纯文本 Chat Completions、Responses 和 Anthropic Messages 请求，无论哪个目标路由或哪类上游服务故障，都会使用同一回退链：OpenRouter 上的 MiniMax M3.1 Flash、Xiaomi MiMo V2.6 Pro，最后是 GPT-6 Luna。返回给客户端的 `model` 保持为最初请求的目标，回退也不会更改已保存的路由选择。LiteLLM 响应头和内部请求元数据仍会记录回退尝试。媒体、图片生成、语音、Advisor 和其他专用操作继续使用各自的路由。失败的上游请求仍可能产生费用。回退会切换实际模型/服务，不能保证输出行为完全相同。当前模型见本仓库的 [`config/litellm.yaml`](config/litellm.yaml)。

语音路由通过 LiteLLM 提供。MiniMax TTS 使用 LiteLLM 标准 `POST /v1/audio/speech`，模型为 `minimax-tts`，凭据使用 `MINIMAX_API_KEY`：

```json
{"model":"minimax-tts","input":"Hello from Subroute.","voice":"alloy"}
```

MiMo ASR 使用其 Chat Completions 格式，不是 LiteLLM 标准 `/v1/audio/transcriptions` 格式。调用 `POST /v1/chat/completions`，模型设为 `mimo-v2.5-asr`，并在 user 消息中传入 OpenAI `input_audio` block（`format` 和 base64 `data`）。也可附加 `asr_options`。Subroute 会保留显式 ASR 模型与音频，即使控制台强制了其他模型也不会改路由；本次语音请求不会调用保存的 advisor。设置 `XIAOMI_TOKENPLAN_API_KEY` 即可启用 MiMo 路由。MiMo TTS 仍使用 Xiaomi 的 `/v1/chat/completions` 格式：将待播文本放入 `assistant` 消息，并通过 `audio` 指定输出选项。

### 使用 Electron 桌面端

先启动网关，再运行桌面控制台：

```powershell
cd desktop
npm install
npm start
```

首次启动时，将桌面端连接到有响应的本地 Subroute 网关。桌面端只在本地保存网关地址，不保存服务 API 密钥。可选的 Agent 启动器会使用仅对本次会话生效的 Subroute 配置来启动受支持的编码 CLI。详见 [desktop/README.md](desktop/README.md)。

### 隔离生产与预发布环境

Compose 将生产环境绑定到 `127.0.0.1:4000`，预发布环境绑定到 `127.0.0.1:4005`，数据库和路由策略各自独立。网关、预发布和 experts 使用相同的锁定镜像配方，代码与三份静态配置随镜像发布。修改源码后，需要重新构建并重建指定服务；运行中的容器保留自身的镜像版本。生产策略仍保存于原来的宿主机 `config/active_model.json`，容器内挂载位置改为 `/app/legacy-config`；预发布继续使用独立状态卷。凭据保留在镜像外部。

```powershell
docker compose build gateway-staging
docker compose up -d gateway-staging
```

### 路由行为

OpenAI 的 Target 和 Advisor 均使用 GPT-6 或更新版本：Luna、Sol、Astra 和 GPT-6.1 Sol。Target 可通过 `codex-gpt-6.1-sol` 选择 GPT-6.1 Sol。GPT-5.6 Terra 与 GPT-5.6 Luna reserve 路由已移除；已保存的旧选择分别迁移到 GPT-6 Sol 与 GPT-6 Luna。

普通 Codex 订阅请求的输出上限会通过 LiteLLM 原生 Responses 转换传到上游；同时提供多个上限时采用最小值。当前桥接层支持至少 16 tokens 的整数上限，更小或无效的值会在调用 Codex 前被该适配器以 HTTP 400 拒绝，不再静默放大或丢弃限制。既有路由回退策略继续生效。未提供上限时沿用服务默认值。

Gemini 4 Argon 可通过已认证的 Antigravity CLI 作为 `gemini-subscription-argon` Target 使用，支持 low、medium、high 三档 reasoning effort。Target 路由沿用 256K context budget，不加入 `auto` fallback；CLI 已确认模型可用，完整响应兼容性仍需 live request 验证。

当 Target 选择 OpenAI Subscription 模型时，路由控制台可选择 **Speed: Fast**。该设置只作用于按当前策略路由到 Target 的请求，不作用于 Advisor、显式模型请求（alias/off 模式）、`auto` 或图片/音频特殊路由。Fast 会更快消耗订阅额度；旧策略默认为 Standard，更换 Target 会清除 Fast 设置。Codex 订阅端点目前拒绝 `service_tier: "fast"`，网关仅在该传输层将其转换为等价的 `priority`，待端点直接支持 `fast` 后移除。

- **`alias`** 是默认模式，根据已保存的路由解析 `default`。`auto` 始终使用按优先级排列的服务链。
- **`force`** 将所选路由应用到已知模型的推理请求，显式使用 `auto` 和图片生成请求除外。
- **`off`** 将模型分发交由 LiteLLM 处理。
- **Advisor（顾问模型）** 可由 Anthropic Messages 中的模型按需调用。网关仅提供一个绑定到当前保存专家的工具；普通请求不自动调用，也不注入建议。每次最多调用一次；4040 失败会直接返回，不重试或切换到普通执行模型。

Advisor 模型分别保存在每个网关环境中：production 使用 `config/active_model.json`，staging 使用独立的 `gateway-staging-state` 卷。可在对应环境的控制台选择已配置的 Gemini 或 OpenAI 顾问；启动时会加载该环境保存的选择，因此两个环境可以不同。工具调用默认使用 low reasoning effort。Codex 专家会收到 1,024-token 输出上限参数，当前 GPT-6 Luna 路由已通过 16-token 实测验证强制截停，其他 Codex 模型尚未验证；Antigravity CLI 没有输出 token 上限设置。LiteLLM 原生 Messages Advisor handler 会缓冲目标响应后再返回合成流，因此即使请求设置 `stream=true`，该路径也不会逐 token 实时返回；普通 Chat 和 Responses 流不受此限制。

Subroute 默认关闭通用自动重试。Gemini 音频请求仅在 Antigravity 明确返回 `provider_content_filter` 时由 LiteLLM 最多重试两次；普通 502、超时、文本请求和其他模型仍不重试。`auto` 以及文档列出的 MiniMax/OpenRouter/Xiaomi 路由由 LiteLLM 按顺序切换服务；可选 Advisor 专家调用没有 retry 或 fallback。MCP 会报告音频重试次数；恢复响应中的 usage 仅统计最后一次 provider 调用，早先失败调用的 usage 保留在 gateway/sidecar 日志。

### 上下文与 Sift

普通 Chat Completions、Anthropic Messages 和 Responses 请求不再经过 Sift 压缩，也不会添加 Ask / Acceptance / Remaining 指令。服务商和 LiteLLM 按实际配置的输入、输出及上下文容量检查请求；较长历史应先由客户端压缩。桌面 auto 配置使用 256,000-token 窗口，并在 204,800 tokens 时触发客户端压缩；已有终端需重新启动。此前的普通路径 4M 输入与 32K 优化目标不再适用。

Sift 只在 4040 专家服务压缩已被模型选中的 Advisor 请求，不会改写 Target 会话。专家文本请求目标为 25,600 个估算输入 tokens，最多放宽到配置的 32,000 输入上限。Codex 专家会收到 1,024-token 输出上限参数，当前 GPT-6 Luna 路由已通过 16-token 实测验证强制截停，其他 Codex 模型尚未验证；Antigravity CLI 没有输出 token 上限。所有专家统一采用 32,000-token 输入上限，输出另计，保留 Gemini 专家。不可压缩的文本会明确失败；图片和音频等多模态输入保持原样并由专家的原生容量检查处理。可选 Qwen 路由仍保留 40,960-token 请求窗口、32,768-token 输入上限和 4,096-token 输出上限；超出容量的普通请求交由原生容量检查和已配置的回退策略处理。

### 使用边界

Chat、Responses 和 Messages 中明确的图片生成意图，以及 Images generation endpoint，均优先于 Force/alias/off 和 `auto`，使用 Codex Subscription GPT-6 Luna 的 `image_generation` 工具。只检查最新用户消息中的直接中英文请求，例如 “generate an image”“draw a cat”“帮我生成一张图片”；不扫描历史消息、代码、引用示例或图片分析。这是本地保守规则，不调用额外分类模型。Chat/Responses 可显式声明 hosted `image_generation` 工具；显式选择 `image_generation`、`generate_image`、`create_image` 函数也会转换到 hosted 工具。仅列出自定义函数不会改变无关请求的路由。此例外不修改已保存策略，也不调用顾问或 fallback。

Chat 通过 `message.images`/`delta.images` 返回图片 data URL；Responses 通过 `image_generation_call.result` 返回 base64；Messages 通过文本块中的 Markdown data URL 返回，能否显示取决于客户端。会保留上下文、说明文字和其他函数工具。会话接口支持完整响应和 SSE，但均等待生成完成后发送图片。Images endpoint 返回 `data[0].b64_json`，不支持流式或 URL 返回。共享限制为单张图片、每进程两个并发和 180 秒截止时间；请省略 `size` 或设为 `auto`，固定像素尺寸会被拒绝。Usage 仅为 Luna tokens，不代表完整图片费用。未实现 Images 编辑/变体端点。证据见 [图片意图路由验证](docs/evals/luna-image-intent-2026-09-29.md)。

统一入口不代表各服务能力相同。工具调用、流式传输、图像输入、上下文长度、推理选项和订阅访问能力都取决于具体路由。Antigravity Gemini 订阅集成支持文本、PCM WAV/MP3 音频输入和受 schema 约束的工具调用（音频和客户端工具不能合用）。Sidecar 通过 CLI 的 NDJSON stdin 协议传递 prompt，避免长 prompt 占用操作系统命令行参数空间。工具调用由客户端执行，订阅 sidecar 不会代为执行。内部 finish 控制结束单次 runtime turn；工具模式读取 AGY 原生 structured_output 并核对返回 schema，不从自然语言提取决定。自动选择且允许并行时，可返回单个 JSON 批次，最多八个调用，全部名称和参数验证通过后才对客户端公开。额外 runtime turns、未授权的本地工具执行及无效结果会明确失败，provider usage 保持原样。该路由不支持图像。流式请求会在 CLI 完成响应后通过 SSE 返回，不会逐 token 输出。普通 Codex Target adapter 会通过原生 Responses 转换保留调用者的输出上限；同时提供多个上限时采用最小值。独立的 Codex Expert adapter 会转发 `max_output_tokens`，当前 GPT-6 Luna 路由已实测在 16 个输出 tokens 时以 incomplete 状态停止；该实测结果不代表所有订阅模型。请根据实际选择的路由确认其能力。

Gemini 订阅同样由后端管理输出上限：CLI 不提供输出 token 上限参数，因此不会执行调用方设置的上限。这适用于普通响应、SSE 和 Anthropic Messages。Subroute 保留完整响应和真实 provider usage，不通过截断输出伪装成遵守了上限。工具 schema 必须自包含；远程 schema 引用会在 provider 调用前被拒绝。

音频请求使用 Chat `input_audio`，明确路由到 Gemini Subscription，并关闭本次请求的 Advisor，Force 模式同样适用；保存的策略不变。最多两个附件，合计 20 MiB。Messages/Responses 音频会明确拒绝，防止静默丢失。[音乐 MCP](docs/misc/music-listening-mcp.md) 经此路径将原始音频发送到 Google，不执行 DSP 或认证混音质量。

MCP 的拒绝和传输失败会返回结构化 `isError`，不会伪造成功听觉报告。明确的音频过滤拒绝可由 LiteLLM 对同一问题和文件最多重试两次；持续拒绝仍可见，不改词、不删词、不切模型。MCP 报告重试次数和最后一次调用的 usage 范围。该策略在固定 60 项抽样中达到至少 58/60，但不保证未来成功率。见 [过滤候选语境与实测](docs/misc/gemini-content-filter-candidates.md)；没有已验证的完整禁词表或零错误保证。

可选 music client 固定 Mutagen 1.47.0，从待发送字节快照检查 MP3 流元数据，头部推导时长注明估计；拒绝时有有效 provider usage 就保留。这些检查不能认证完整解码或听觉判断准确。

网关默认只监听本机回环地址。除非你主动配置并保护其他网络边界，否则请将其保留在本机使用。

### 项目文档

- [架构与职责边界](docs/misc/architecture.md)
- [Gemini 音乐听觉 MCP：两个可用入口与混音验收边界](docs/misc/music-listening-mcp.md)
- [产品方向](docs/northstar/README.md)
- [网关配置](config/litellm.yaml)
- [桌面端说明](desktop/README.md)
