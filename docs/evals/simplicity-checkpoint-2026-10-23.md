# Gateway simplicity checkpoint

This checkpoint publishes the reviewed simplification while follow-up work continues. It is not a claim that the whole goal or production rollout is complete.

## Implemented contract

- Ordinary model dispatch has no Sift callback, compaction recovery or long-history instruction injection. LiteLLM owns public protocols, streaming, provider adapters and normal admission.
- Optional Advisor orchestration uses LiteLLM's native Messages tool. Sift operates only on the private expert request at port 4040. Ranker, frequency, analytics and ONNX paths are removed.
- Every configured expert, including all Gemini and Codex aliases, has a 32,000-token input ceiling. The owner explicitly selected input-only and retained Gemini. Sift targets 25,600 input tokens with bounded relaxation. Codex's separate 1,024 output request remains; Gemini has no invented output cap.
- The locked image packages source and three static configs. Saved policy and credentials remain external. Staging has separate database and state. Production's existing host policy file is preserved through the legacy mount.
- Narrow compatibility repairs preserve normal output limits, stream identity, citation offsets and the selected LiteLLM 1.106.2 interfaces. LiteLLM requires MCP 2.2 or newer; the MCP entry points and optional Relay dependency lock therefore include their required compatibility updates.

## Evidence and limits

The [portable receipts](receipts/simplicity-2026-10-23.json) contain historical source-reduction hashes, native input-admission checks and sanitized observed inference records. Historical detailed reports refer to task-local `.runs` artifacts; those working artifacts are not part of this publication.

| Check | Evidence | Limit |
| --- | --- | --- |
| Expert budget | 30 focused tests; 18 native admission cases over nine aliases | Synthetic counts: input 32,000 plus output 1,024 admitted; input 32,001 rejected. No provider tokenizer-boundary claim. |
| Ordinary Chat and Responses | One bounded live MiMo call per protocol; terminal success; zero retries/fallbacks; two completed provider records; no Advisor records | Short requests only, on recovered staging. |
| Ordinary Messages | One live MiMo call with explicit tool choice none; complete public SSE; exactly one completed provider record and no Advisor record | An additional orchestration bridge remained pending. Response compatibility passes; accounting closure does not. |
| Staging recovery | Locked image, database/liveness 200, auth 401/200, saved policy bytes and existing migration/receipt counts preserved; independent review | Production and experts were not restarted. |

The reduction ledger reports 1,693 fewer maintained source lines for its historical coordinated snapshot. Its raw hashes are not asserted to identify the final publication tree.

The earlier complete **working-tree** run passed 979 non-live tests with two environmental skips and 35 live tests deselected. Both skipped checks passed separately on owned PostgreSQL/Linux fixtures. That tree included unrelated local work; it is not substituted for the exact publication-tree check recorded below.

## Publication scope

The source worktree's unrelated durable SQLite outbox, Relay orchestration/campaign changes, Electron upgrade and Antigravity CLI upgrade remain local. The published accounting module retains the existing HEAD behavior plus only the interfaces/capability checks required by LiteLLM 1.106.2. The unfinished orphan-receipt repair is deliberately excluded from this requested checkpoint. The original dirty worktree is preserved.

Continue with truthful Messages accounting closure, expert-service deployment and live Sift integration, remaining context/failure acceptance, then the full goal audit. Do not fabricate usage, rewrite old receipts or treat this checkpoint as production acceptance.

## Exact publication-tree checks

With Python 3.13.11 and the locked LiteLLM 1.106.2/MCP 2.3.0 environment, the exact selected source tree passed **972 tests**, with two environmental skips and 35 live tests deselected (96.90 seconds). Command: `python -m pytest -q -m "not live" -p no:cacheprovider --basetemp=<task-owned-temp> --tb=short --disable-warnings tests`. Offline LiteLLM cost metadata and pre-cached tokenizers were used; `PYTHONPATH` selected this checkout's `src` directory. [Full console result](receipts/simplicity-checkpoint-tests-2026-10-23.txt).

The first exact-tree run had 971 passes and one failure because an existing project-config assertion depended on an ignored local `.codex/config.toml`. This checkpoint includes that credential-free project registration. The formerly failing assertion independently passed, and the complete suite then passed as recorded above. Desktop launcher checks passed all five tests; JavaScript syntax check passed. Independent review accepted the bounded publication corrections and dependency/accounting scope; the full-goal and live deployment limitations above remain open.
