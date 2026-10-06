# Claude Code thinking, answer and tool formatting

Date: October 6, 2026 (America/Denver).

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
