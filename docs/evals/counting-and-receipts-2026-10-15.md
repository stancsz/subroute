# Token counting and receipt warnings, 2026-10-15

Verified on the Docker Compose staging gateway at port 4005, using the pinned LiteLLM 1.103.0 image and its generated Prisma client. Production was not restarted for this change.

## Causes and changes

LiteLLM's tool-schema formatter indexes array `items` unconditionally. JSON Schema allows an array to omit `items`, so these requests raised `KeyError('items')`. The router then skipped context filtering. Sift's standalone counting-copy repair did not cover the router's later count. Subroute now installs a version-bounded repair at the shared formatter: an array without `items` formats as `any[]`. It changes no request schema and adds no dependency. Retire this repair when the deployed formatter accepts omitted `items`. The vendored Sift fallback remains available for standalone Sift installations.

The recurring receipt warnings were `RuntimeError` from Prisma's async HTTP connection being used on another event loop. LiteLLM's native Responses `_run_post_success_hooks` invokes `run_async_function` synchronously, blocking the serving loop while the deployment callback runs in a worker loop. A diagnostic staging request confirmed the loop mismatch; an isolated Prisma reproduction produced the same failure. Sending SQL back to the blocked loop instead produces a timeout, so that approach was discarded.

The existing raw stream parser and close observer already own provider usage, terminal outcomes, EOF, and cancellation. Observed streams now skip duplicate writes in the deployment success callback and aggregate success logger. Buffered and cache paths still run. This removes redundant persistence without a new client, queue, or retry. The SQL deadline stays at three seconds. Remaining write failures log their exception type without exception bodies or SQL parameters.

Before removing the aggregate duplicate writer, a separate three-second SQL timeout appeared on a large MiniMax request. Its receipt was already complete with 232,680 input and 143 output tokens. The final probe window below had no receipt warnings; it does not establish that a future database outage or deadline cannot occur.

## Verification

- The full local LiteLLM 1.101.0 suite passed 732 tests with 39 skipped. After the final aggregate-writer change, all 31 focused counting, accounting, native/translated protocol, and Chat lifecycle checks passed.
- Protocol regression fixtures now reject any receipt write on a foreign event loop while still checking complete, incomplete, missing-usage, zero-cost, and translated outcomes.
- A fresh process in the serving 1.103.0 image used a disposable PostgreSQL database and temporary schema. It passed the actual blocking worker-hook reproduction, revision/acknowledgement/environment checks, and framework streams with known, missing, and explicit-zero usage. The schema and disposable database were removed.
- The deployed Router counted nested arrays with omitted `items` in Chat, Anthropic, and Responses tool shapes, returning 51 estimated input tokens. A 20-token test limit correctly rejected each request before provider work. Tool definitions were unchanged.
- Live staging Qwen Chat Completions and Anthropic Messages requests with the previously failing schemas each returned the exact requested marker and normal completion. Sift reported 65 estimated input tokens on each. The temporary alias policy was restored, preserving all policy fields except the expected revision increment.
- Final live GPT-6.1 Sol probes ran with Fast enabled from **19:18:58 to 19:19:23 UTC** (**13:18:58 to 13:19:23 America/Denver**). Buffered and streaming Chat Completions, Anthropic Messages, and Responses all returned HTTP 200, exact reconstructed text, and their normal terminal outcome. Saved policy was unchanged during these six requests.
- PostgreSQL contained two complete leaf provider receipts per protocol for that final window, all with complete provider usage. Logs since the final staging restart at 19:18:05 UTC contained zero token-count errors and zero receipt/checkpoint write warnings. Readiness reported healthy with the database connected.
