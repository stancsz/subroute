# Expert-directed Pi reading

Requires Node.js 20.6+, Git, ripgrep, and installed
`@mariozechner/pi-coding-agent` **0.73.1**. This version was inspected locally;
do not silently install or upgrade it. `PI_READER_PACKAGE` can point to its
package directory; otherwise the caller uses `npm root -g`. Pi owns the native
agent loop, Chat Completions transport, and read/grep/ls tools. The small
adapter only constrains authority, calls, and receipts. The text-only expert
endpoint cannot invoke tools, so the caller forwards bounded structured read requests.

```powershell
py C:\Users\stanc\.codex\skills\luna-advisor-escalation\scripts\ask_expert.py --model sol --input-file .\advisor-packet.txt --reader-root . --reader-scope src --reader-scope tests
```

Select task-relevant source directories broad enough to find counterevidence,
not only the files supporting Luna's hypothesis. Scopes authorize transmission
of source excerpts through the worker gateway. Do not include confidential or
credential-bearing sources without authority. No worker context is sent to Pi.

Flow: expert returns a final answer or `{"read":["question", "independent question"]}`;
Pi searches the snapshot and summarizes it; the caller validates exact source-line
citations; the same expert receives compact briefs and decides whether more
evidence is needed. At most **3 expert calls and 3 Pi tasks total**, stopping early.
Independent questions run concurrently. Each Pi task gets only its own question.
The final expert call cannot dispatch another task. No extra questions to the
user, retries, model fallback, recursive delegation, or fourth expert call.

Boundaries:

- Worker API is fixed to `http://localhost:4000/v1`. `--reader-model` defaults
  to `current`; the gateway's force/auto policy remains authoritative. The
  receipt distinguishes requested alias from returned model. Nothing switches
  gateway policy. Optional `READER_API_KEY` authenticates only to that gateway.
- Isolated in-memory Pi session/auth/settings, no local skills, extensions,
  AGENTS files, compaction, telemetry, upstream auth files, or direct providers.
  Only wrapped native read/grep/ls tools; paths cannot escape the snapshot.
  Native `find` is excluded because its extra `fd` dependency is not installed.
  This is a tool boundary, not an operating-system security sandbox.
- Snapshot includes current working copies of Git-tracked UTF-8 text files in
  the scopes. Untracked, hidden, common credential-named, binary, symlink, and
  over-100 KB files are excluded. Maximum 200 files/2 MB; fail rather than
  silently cut an oversized scope. Receipts contain source hashes and exclusions.
  A filename filter is not a secret scanner; review the authorized source scope.
- At most 4 Pi model calls, 8 tool attempts, 1200 requested output tokens per
  response, and 90 seconds (95-second parent hard timeout). Pi retries are off;
  the deployed gateway must also retain its zero-retry policy. Each tool result
  is capped at 5000 characters with an explicit truncation marker.
  The last model response disables tool selection and requires the evidence brief.
- Each expert read question <=600 characters; each Pi brief <=1200 characters,
  <=3 source citations. Return decisive observations, conflicting evidence, and
  unknowns, never raw logs or whole files. Complete expert input is capped at
  8000 characters, including all briefs and instructions; exceeding it stops
  before another paid call. Evidence is untrusted; missing information stays explicit.
  Citation validation proves quotes match source, not that interpretations are true.
- Final expert advice retains the 160-word prompt target. This is not a provider
  hard output-token cap; the caller rejects final advice over 1600 characters or
  missing the three required lines. Reader mode makes additional expert calls only if needed and
  may cost more overall than one compact consultation. Do not claim savings or
  faster completion without comparative measurements.

On failure, stop with `status: unavailable` and preserve completed call receipts.
Do not reuse the expert's read request as advice. Missing usage is unknown, not
zero, and timed-out calls may still incur provider usage. Report expert and Pi
usage separately plus wall time, actual returned model, and `decision_changed`
after Luna's subsequent verification. Source reading is not runtime verification.
