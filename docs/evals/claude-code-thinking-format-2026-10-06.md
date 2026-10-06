# Claude Code thinking, answer and tool formatting

Date: October 6, 2026 (America/Denver).

The first verification below covered API fixtures and live HTTP requests. Actual
Codex CLI testing subsequently exposed additional defects; that earlier evidence
did not establish a complete coding-client tool journey. The follow-up section
records the additional reproduction and repairs.

The reported symptom was thinking, answers and tools getting mixed together in
Claude Code through local-api-gateway. Fixes are applied to both repositories and
the running Subroute production/staging services and local gateway on port 11435.

## Confirmed defects and ownership

1. **Subroute / LiteLLM Messages conversion:** an unsigned `thinking_blocks`
   opener contains the first thinking fragment, then the first delta repeats it.
   A strict client accumulator reproduced duplicate text on 1.101.0 and 1.103.0.
   The existing streaming callback now empties that opener only when the next
   event repeats exactly the same text/index. Signed snapshots and redacted
   content remain unchanged. The callback holds at most one opener for one event.
2. **Subroute / LiteLLM parallel tools:** interleaved Chat tool indices are lost
   during conversion. Opening tool 1 closes incomplete tool 0; remaining tool 0
   arguments then go into tool 1. A client accumulator reproduced invalid JSON.
   A version-bounded shim at LiteLLM's existing async chunk splitter assembles
   tools by their original indices before conversion. The suffix from the first
   tool is held until provider finish, keeping its original thinking/text/tool
   order. Every tool identity and JSON input is validated before any pending tool
   is exposed. Truncation/provider failure raises an error, and cancellation
   closes the source. Actual argument strings are concatenated without decoding
   and reencoding their contents. LiteLLM continues to serialize public protocols.
   Public Chat/Responses and native Messages retain their existing adapters.
3. **Subroute configuration:** LiteLLM's default OpenAI Messages bridge sends
   Xiaomi and local Chat-only backends to `/responses`. Native HTTP fixture tests
   reproduced that wrong endpoint. The supported
   `use_chat_completions_url_for_anthropic_messages: true` setting selects
   `/chat/completions` for those backends. Native Messages providers and custom
   subscription handlers keep their existing lanes.
4. **Local gateway response rewriting:** raw serialized text was rewritten before
   complete JSON/SSE frames were available. This could modify signed thinking,
   redacted data, IDs and tool arguments, while model masking depended on physical
   network chunk boundaries. Rewriting now operates on decoded presentation
   fields in complete frames, preserving opaque fields. UTF-8 and LF/CRLF/CR
   boundaries are tested one byte at a time. Forward-only mode clears inherited
   free-text model substitutions and preserves ordinary prose.
5. **Local gateway optional OpenRouter sanitizer:** strict mirror subtraction left
   closing think tags behind; the fallback could remove legitimate answer text,
   move text after tools and rebuild failed/truncated streams as success.
   Verified mirrors now lose their delimiters in place. Closed redacted mirrors
   require plaintext evidence; only matching redacted metadata is removed.
   Original message identity, usage/cache details and stop sequences are retained.
   Ambiguous/malformed/incomplete/error streams pass through without fabricated
   completion or replacement tool inputs.

## Regression evidence

- Local gateway: **199 passed**, full default test suite; TypeScript typecheck
  and diff whitespace checks passed.
- Subroute: **52 passed** for thinking format, Codex reasoning and proxy HTTP
  regression tests, both on Windows with pinned LiteLLM **1.101.0** and in a fresh
  container using the exact deployed image with LiteLLM **1.103.0**.
- Provider HTTP fixtures exercise the actual LiteLLM router/transports/adapters
  for Anthropic Messages, MiniMax Messages, OpenRouter Chat, Xiaomi Chat, local
  OpenAI-compatible Chat and Ollama Chat, with streaming and buffered responses.
  Subscription tests exercise Codex and the Gemini adapter with their external
  transport stubbed. These are protocol evidence, not live provider claims.
- Client accumulation verifies block lifecycles, unique indices, exact reasoning
  and answer strings, Unicode, code escapes, signatures/redacted data, separate
  tool identities/inputs, mixed continuation chunks, fragmented tool names, usage,
  finish reasons, failure and cancellation.
- Full Subroute suite: **569 passed, 15 failed, 17 skipped** on the final source
  before the last live-only test was added. The same **15 failures** reproduce
  against an untouched `HEAD` archive (83 passed in those three baseline modules).
  They are existing advisor/default-policy/config-expectation failures. This
  change does not redefine their expectations or claim the full suite is green.

## Live evidence

`tests/test_live_thinking_format.py` completed **7/7 passed in 25.66 seconds**:

| Route | Observed blocks / result |
| --- | --- |
| MiniMax through staging | thinking, text |
| OpenRouter through staging | text |
| Xiaomi MiMo V2.6 Pro through staging | text |
| Codex Luna through staging | text |
| Gemini subscription through staging | text |
| Local gateway 11435, MiniMax target | thinking, text |
| Local gateway 11435, Codex tools | two distinct tool inputs, then replayed results and final answer |

All live streams completed with correctly typed blocks. The rendering smoke
checks actual line breaks independently of whether a model chooses the exact
requested words. The tool check verifies distinct q=1/q=2 calls, unique IDs, and
successful next-turn replay. Staging target/advisor settings are restored in a
fixture `finally`; production policy is never changed by these tests. Both
Subroute listeners and the local gateway returned readiness/health HTTP 200.
Configured fallbacks remain owned by LiteLLM; live route success alone does not
prove a particular fallback was unused. Local fixtures isolate each adapter.

## Deployment recheck

Deployment recheck after the final local image rebuild: **7/7 live checks passed
again in 37.70 seconds**. Production `current` also returned a valid completed
Messages response with its policy unchanged. Production 4000, staging 4005 and
local 11435 each passed 10/10 readiness/health requests (maximum observed latency
3.3, 8.3 and 4.8 ms respectively). SHA-256 checks of the changed runtime files
matched the host source in both Subroute containers and the local image. The
gateway's 199 tests and typecheck passed again before publishing the changes.

## Maintenance and limits

- Remove the version-bounded chunk-splitter shim when an upgraded LiteLLM passes
  the interleaved-tool tests unassisted. No dependency or service was added.
- Tool suffix buffering is the deliberate latency tradeoff for Chat's lack of
  per-tool terminal events. Thinking and text before tools continue streaming.
- Provider-generated literal `\\n` strings are preserved. Indiscriminate escape
  decoding would corrupt code samples and invalidate signed thinking.
- Providers that do not emit displayable reasoning do not receive invented
  thinking blocks. OpenRouter's existing reasoning-exclusion setting is retained.
- Live checks cover the five configured remote routes and the local forwarder.
  Anthropic and Ollama adapter behavior is established by fixtures; no new direct
  API deployment or excluded provider was enabled for verification.

The [Anthropic thinking documentation](https://platform.claude.com/docs/en/build-with-claude/thinking)
describes the thinking/signature streaming contract that these preservation
checks enforce.

## Follow-up: actual Claude Code and Codex clients

The original Codex/MiniMax CLI reproduction leaked the complete reasoning into
visible `<think>...</think>` answer text, reported `OutputTextDelta without active
item`, repeated tools/compaction and timed out. Strict wire fixtures then exposed
three additional boundaries:

- Chat-to-Messages conversion duplicated signed signatures, lost subsequent
  thinking blocks, dropped redacted blocks, and buffered assembly discarded
  unsigned thinking and signature tails. The existing adapter now receives
  separate fragments/boundaries and retains every tested replay block.
- Chat-to-Responses conversion reused output index zero for thinking and answer,
  omitted message/summary-part openers, mislabeled message parts, put reasoning
  in final answer content, and reused closed reasoning identities on later
  phases. `responses_stream.py` repairs those verified defects on LiteLLM
  1.101.0/1.103.0, retaining its existing transport, typed events, terminal latch,
  response envelope, usage and opaque replay content. Native Responses remains
  outside this bridge patch.
- The actual Codex client also rejected reasoning openers lacking required
  `summary: []`, even though the first custom accumulator accepted them. The
  regression validator now uses the official OpenAI SDK item schema and rejects
  every client `without active item` error. The required field is confirmed by
  [Codex's response item definition](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs).

MiniMax Chat's documented native
[`reasoning_split: true` option](https://platform.minimax.io/docs/api-reference/text-openai-api)
keeps its reasoning separate before protocol conversion. Indiscriminate newline
decoding or thought removal is not used. Tool-level opaque provider metadata is
preserved when assembling parallel calls. Metadata already discarded by
LiteLLM's input type is not claimed as recovered.

The local gateway's optional mirror sanitizer now matches each answer run to
its immediately preceding thinking segment. It preserves tool order and leaves
literal examples beyond a tool boundary untouched.

### Follow-up regression checks

- Local gateway: **201 tests passed**, typecheck passed.
- Subroute: **281 targeted tests passed, one POSIX cleanup test skipped** on
  pinned Windows LiteLLM **1.101.0**; **282 passed** on the exact immutable
  deployed Linux image with **1.103.0**, including the POSIX cleanup test.
  The final Unicode-escape guard then passed all **58 handler tests** on each
  version, including four additional streaming/buffered cases.
- New client-contract fixtures cover signed/unsigned/multiple/redacted thinking,
  exact signatures, buffered and streaming Messages, Responses schema/lifecycle,
  thought phases before/after answers/tools, opaque tool metadata, and native
  typed `reasoning_text` Responses content. Summary-only constraints apply to
  the repaired Chat bridge; valid native reasoning content remains supported.
- Independent review executed 28 original/transition fixtures on both versions,
  checked typed envelopes/terminal latch/usage, and confirmed cancellation closes
  the upstream exactly once. A subsequent real-client review found the missing
  summary opener and placeholder tool-test gap; both became explicit assertions.
- The final broad suite yielded **604
  passed, 15 failed, 28 skipped**. Its 15 failures match the previously reproduced
  untouched-HEAD advisor/default-policy/config failures. This is not a
  full-suite-green claim.

The Gemini tool follow-up found conflicting sidecar instructions: answering only
from supplied text discouraged requesting caller tools. The handler and target
agent now distinguish returning a schema-constrained client dispatch decision
from executing tools inside the sidecar. Local sidecar tools remain disabled;
file contents/execution results are never fabricated.

Actual Codex/Gemini then exposed adjacent distinct tool decisions in AGY's
output, which the single-decision parser rejected with HTTP 502. The bounded
decoder now retains these calls in order for automatic choice when parallel
calls are permitted, with a maximum of eight. All names and inputs validate
before any result is exposed. New streaming/buffered tests preserve separate
inputs, IDs, Unicode and escapes; failure cases reject unknown/invalid tools,
mixed answer/tool decisions, malformed tails, oversized batches, and caller
parallel opt-out violations without exposing a partial prefix. Provider input
cannot inject the internal normalized batch shape to bypass these checks.

Another real Gemini/Codex result put prose before fenced tool JSON and fabricated
file values. The early plain-text fallback previously accepted it as an answer.
It now rejects embedded client decisions, including JSON Unicode-escaped keys
and values, instead of showing the rejected tool decision as answer text. All
18 independent adversarial parser probes pass. The target agent also explicitly
requires a JSON-only decision and actual caller results before computing an
answer. These repairs introduce no provider retry or hidden sidecar execution.

### Actual client method and limits

`tests/test_live_coding_clients.py` runs Claude Code **2.1.251** and Codex CLI
**0.154.0** against local gateway **11435**, forwarding to Subroute staging
**4005**, with real provider responses. It uses process-only client settings,
temporary synthetic files, read-only tools/sandbox and header-free wire capture.
The Windows fixture grants only read/traverse access to its synthetic directory;
pytest's default private ACL otherwise caused real sandbox access failures.

Each passing journey must execute file tools and replay both actual file markers
to the provider, return the computed sum, a Markdown table/code fence with real
newlines and literal code escape, finish without rejected stream items or visible
thinking tags, and preserve the fixture files. A placeholder answer cannot pass.
Staging routing/advisor settings restore in `finally`; production policy remains
unchanged. All calls have a bounded test timeout and preserve failure evidence.

The native Responses check accepts typed `reasoning_text` content, independently
of the repaired Chat bridge's summary-only representation. Bold Markdown around
the computed sum is valid. Earlier test failures from these overly narrow
assertions were test defects, not malformed provider output.

Provider route success may include existing configured LiteLLM fallbacks; it
does not independently establish that a fallback was unused. Fixtures isolate
provider adapter behavior without spending. Terminal UI folding and screenshot
appearance are unverified; CLI event consumption and exact returned structure
are verified by these checks.

### Final real-client evidence and remaining defects

The independent reviewer validated ten final journeys: Claude Code and Codex
with each of MiniMax, OpenRouter, MiMo Pro, Codex Luna and Gemini Subscription.
Each completed its actual file-tool/result/answer flow and passed the strict
wire lifecycle, SDK reasoning schema, computation, Markdown, literal escape,
thinking separation and unchanged-file checks. Three earlier failed Gemini
attempts remain in the [durable receipt](coding-client-format-2026-10-06.json).

This establishes the main protocol flow, not flawless provider behavior. Across
the successful final journeys, **17 HTTP errors** were recorded: eleven Gemini
Claude background requests rejected unsupported reasoning `none`, four Codex
Luna Claude background requests rejected unsupported subscription parameters,
and two Gemini Codex requests returned 502 before the native client retried.
The final Gemini Codex answer repeats its formatted result four times and its
four-tool batch includes three redundant successful reads plus one failed
PowerShell read. These provider content/tool-selection defects remain; Subroute
preserves their output and replays actual results. They are not removed silently
or counted as provider reliability passes. Overall perfect-provider acceptance
is still **NOT READY**, even though the protocol repair has scoped independent
acceptance. No further paid retry loop was used to hide these findings.

Both Subroute Docker gateways loaded the final source, and the Antigravity image
was rebuilt with the final target-agent instructions. Readiness on 4000/4005 and
local-gateway health on 11435 each returned three HTTP 200 responses. Running
Subroute file hashes match host source, both use LiteLLM 1.103.0, and the installed
target agent matches its source. A production `current` request completed with
legal text/terminal fields while production policy remained unchanged. Staging
test policy was restored. Local gateway was rebuilt with its 201-test/typecheck
verified source. These are updates to local Docker services; no registry image
publication was configured or claimed.
