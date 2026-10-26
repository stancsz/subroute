# Model catalog alignment, 2026-10-23

Reference: the parent fetched the public `https://openrouter.ai/api/v1/models` catalog into `.runs/model-catalog-alignment/openrouter-models-2026-10-23.json` (469 entries). CLI workers used that supplied snapshot after their sandbox denied a direct download. No inference-provider probes were made for this naming task. This is a title alignment against exact IDs in that snapshot, not a claim of equal weights, service quality, provider limits, or capabilities across routes.

Exact counterpart display names applied to existing real cards (including advisor cards):

- OpenRouter `minimax/minimax-m3.1-flash`: `MiniMax: MiniMax M3.1 Flash`; canonical selectable ID is `minimax/minimax-m3.1-flash`. Legacy `openrouter`, `openrouter-flash`, and `minimax-flash` remain accepted against the same OpenRouter target and hidden from duplicate ordinary choices.
- MiniMax native `minimax/MiniMax-M3`: `MiniMax: MiniMax M3`.
- Gemini subscription cards: `google/gemini-3.8-flash` -> `Google: Gemini 3.8 Flash`; `google/gemini-4-argon` -> `Google: Gemini 4 Argon`; `google/gemini-3.7-flash` -> `Google: Gemini 3.7 Flash`; `google/gemini-3.6-flash` -> `Google: Gemini 3.6 Flash`.
- Codex subscription/advisor cards: `openai/gpt-6-sol` -> `OpenAI: GPT-6 Sol`; `openai/gpt-6-astra` -> `OpenAI: GPT-6 Astra`; `openai/gpt-6.1-sol` -> `OpenAI: GPT-6.1 Sol`; `openai/gpt-6-luna` -> `OpenAI: GPT-6 Luna`.
- Xiaomi token-plan cards: `xiaomi/mimo-v2.6-pro` -> `Xiaomi: MiMo-V2.6-Pro`; `xiaomi/mimo-v2.6-flash` -> `Xiaomi: MiMo-V2.6-Flash`; `xiaomi/mimo-v2.5-pro` -> `Xiaomi: MiMo-V2.5-Pro`; `xiaomi/mimo-v2.5` -> `Xiaomi: MiMo-V2.5`.
- Local Qwen card: `qwen/qwen3.8-27b` -> `Qwen: Qwen3.8 27B`; its existing Local provider/access labels remain and do not assert equivalence to catalog service.
- Direct MiniMax M3.1 Flash title matches `MiniMax: MiniMax M3.1 Flash`, while its provider/source/access labels continue to identify the distinct native API route.

The verified counterpart count is 26 model-list entries across these mappings (including duplicate compatibility aliases and advisor entries); excluding the generic `auto` and `default` policy aliases, 20 selectable/advisor-visible entries are checked by the independent parent fixture. Excluded from exact mapping: Gemini 3.1 Pro (no exact snapshot ID), Qwen 2.5 Coder (unspecified size/route), MiniMax and MiMo speech variants (no matching catalog IDs), FreeToken endpoint default, and generic auto/default aliases. Internal `experts/<alias>` transport names remain unchanged. UI source summaries now use exact matched names and do not add speculative Terra/Reserve entries.

Fallback: canonical MiniMax Flash now has an explicit chain `[mimo-v2.6-pro, codex-luna]`, matching the existing OpenRouter/Flash chain and avoiding a second attempt through the same OpenRouter deployment.

Request identity: legacy IDs are normalized for internal routing, while `metadata.routing.requested_model` is set from the original request before rewriting and overwritten server-side. Response stamping can therefore retain the caller's ID without trusting client metadata; message/tool/token/temperature fields are not translated here.

The first independent parent check caught incomplete card titles, the duplicate OpenRouter fallback, and legacy request identity loss. Its initial expert transport-base and policy-version assertions were subsequently corrected by the parent to accommodate concurrent independent edits/migrations. The bounded repair addresses the narrowed findings. Two existing configuration-test expectations were updated to require the exact OpenAI title and the canonical alias's explicit fallback chain; list equality and budgets remain enforced. Unrelated concurrent Advisor import/UI and global-temp sandbox issues were not changed.

## Parent acceptance and integration handoff

Independent parent acceptance passed **79 focused checks, with 10 historical proxy/replay checks deselected**, on LiteLLM 1.106.2. The command and final output are recorded in `.runs/model-catalog-alignment/parent-accepted.txt`; the parent-authored cases are in `test_parent_catalog.py` alongside that receipt, and include saved legacy selections, all three routing modes, Responses with client metadata, the previously retired guided alias, native-provider separation, unchanged provider parameters and capacity, and the canonical fallback boundary. The source/config regression command was:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
tmp/gateway-availability/venv-newest/Scripts/python.exe -m pytest -q .runs/model-catalog-alignment/test_parent_catalog.py .runs/minimax-reliability/test_direct_flash_acceptance.py tests/test_litellm_config.py tests/test_minimax_direct_provider.py tests/test_minimax_reliability.py -k 'not pinned_proxy and not provider_replay' -p no:cacheprovider --basetemp=.runs/model-catalog-alignment/parent-accepted --tb=short
```

The 10 excluded checks and the broader baseline failures involve concurrent Advisor integration. The earlier parent baseline across dynamic routing/config/control UI had 48 passes and 10 failures before naming changes. The builder's later broader dynamic/config run had 11 failures, including the two stale expectations subsequently repaired. This focused acceptance is not whole-suite or live gateway acceptance. A scoped `git diff --check` passed.

The parent inspected the actual control assets in an isolated read-only preview at `http://127.0.0.1:4817/control`, desktop viewport approximately 1265 by 712. Provider configuration flags were fixture data, usage was unavailable, and POST routes were absent. Rendered connection summaries wrapped without overlapping other columns. The OpenRouter selector exposed exactly one actual model option, `minimax/minimax-m3.1-flash`, with title `MiniMax: MiniMax M3.1 Flash`. The separate MiniMax selector retained native M3, native M3.1 Flash and Speech 2.6 HD under their original provider aliases. The Local Qwen summary displayed `Qwen: Qwen3.8 27B (local weights)`. These observations establish scoped rendered presentation only; no live selection was changed.

The owner authorized coordination with "Find simplifications in Subroute and". The source/test handoff and source hashes are in `.runs/model-catalog-alignment/parent-acceptance.json`. This task performed no shared gateway/staging restarts or explicit saved-route writes. Deployment remains pending the coordinated integration and live verification.

Skill learning: provide a captured public catalog to sandboxed CLI workers when their network access is unavailable. Review alias targets, response identity and fallback chains alongside titles; label-only checks missed concrete compatibility and duplicate-provider failures in the first pass. Keep names separate from transport identity and provider-specific capacity. The parent applied these lessons in the final acceptance and handoff.

## Integration follow-up: native Messages stream identity

The integration owner reproduced HTTP 500 when identity restoration assigned to LiteLLM 1.106.2's read-only `AnthropicSSEStream.model`. The original failure is `.runs/goal-simplicity-20261023/advisor-only/thinking-first-failure.txt`. The parent independently reproduced three native SSE identity cases and a missing nested `message_start.message.model` restoration case, all failing before the repair (`.runs/model-catalog-alignment/stream-repair/parent-before.txt`).

The repair updates the existing mutable `AnthropicStreamWrapper` reached through `AnthropicSSEStream._anthropic_wrapper`, before LiteLLM's own serialization. Existing iterator identity, cancellation/close behavior, response/chunk stamping and native wrapper handling are preserved. Unknown read-only model properties are skipped, while mutable setter failures still surface. A draft that decoded and reserialized SSE was rejected and removed. No new stream wrapper, codec, dependency, retry or provider call is introduced. Retire the private adapter access when the pinned LiteLLM exposes a supported caller-model restoration mechanism or preserves the requested alias natively; recheck emitted model names and cleanup at that upgrade.

The final independent parent run in the clean final-lock environment passed **79 checks and failed two**, with no deselections, across `tests/test_thinking_format.py`, `tests/test_codex_proxy_http.py`, new `tests/test_response_model_identity.py`, the parent native-stream probe, and the parent catalog/alias probe. This is a different suite from the earlier 79-pass naming acceptance above. Both originally reported Codex thinking-format cases now pass. The two remaining Gemini subscription-format fixtures fail controller validation before streaming: their saved Advisor is `None` and their minimal model list has no selectable expert matching the default. The integration owner has the exact failures; no Advisor policy was changed in this repair.

Final output: `.runs/model-catalog-alignment/stream-repair/parent-final.txt`. Source identity, the four-case failing baseline, excluded parallel scope and the reviewed patch are recorded alongside it in `parent-acceptance.json` and `reviewed.patch`. The checked router SHA-256 is `3e23f09f1defc6920ab871ea98fd99d2dc65373eb942b8102f6e2f948ca76ae8`; AST comparison confirms code outside the two targeted functions is unchanged from the repair snapshot. Scoped whitespace checks passed. The test command was:

```powershell
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
$env:TIKTOKEN_CACHE_DIR=Join-Path (Get-Location) '.runs/goal-simplicity-20261023/tokenizer-cache'
$env:CUSTOM_TIKTOKEN_CACHE_DIR=$env:TIKTOKEN_CACHE_DIR
$env:ACTIVE_MODEL_STATE_PATH=Join-Path (Get-Location) '.runs/model-catalog-alignment/stream-repair/parent-import-state.json'
.runs/goal-simplicity-20261023/venv-final/Scripts/python.exe -m pytest -q .runs/model-catalog-alignment/stream-repair/test_parent_stream_identity.py tests/test_response_model_identity.py tests/test_thinking_format.py tests/test_codex_proxy_http.py .runs/model-catalog-alignment/test_parent_catalog.py -p no:cacheprovider --basetemp=.runs/model-catalog-alignment/stream-repair/parent-final --tb=short
```

An earlier broader run omitted the tokenizer-cache environment and unit-test guards rejected encoding-download attempts; that failed harness result remains in `parent-after.txt`. The corrected run used existing local tokenizer files. CLI worker receipts preserve the rejected draft, two nonexistent-interpreter launch mistakes in the final worker, and its cache-related test failures; those are not substituted for parent acceptance.

The stable repair was handed to "Find simplifications in Subroute and" for its final combined checks. This repair performed no shared-service restarts or shared saved-route writes. Production deployment and live provider compatibility remain pending that integration. Learning applied: inspect and mutate the native serializer's model owner instead of creating a second protocol codec; test the emitted identity and stream cleanup in addition to status codes.
