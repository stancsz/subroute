# Operations incident reconstruction

Investigate **INC-284** using the four supplied JSONL evidence files. Produce the incident findings as a JSON artifact. This task tests reading, joining, and interpreting operational records; it does not require implementing software.

The logs are an explicitly authored stress-intake fixture. Routine records and similar routes, policies, and incidents are distractions. Reconstruct the relevant chain using identifiers and event/effective timestamps. File order and line order do not establish which policy was active. A policy published before the incident can have an effective timestamp after it. Distinguish the original incident policy from a later mitigation.

Run `python -m unittest -q` before editing. Then read **all four files sequentially, one whole file per tool call**, before writing findings:

1. `incident_1.jsonl`
2. `incident_2.jsonl`
3. `incident_3.jsonl`
4. `incident_4.jsonl`

In Claude Code, use a full Read of each file without a line limit. In Codex, use one PowerShell `Get-Content incident_N.jsonl` call per file with `max_output_tokens=14000`. Check that the complete 160-line file was returned. Do not substitute searches or head/tail slices for this intake step. The sequence is deliberate so the context-compaction path receives the complete evidence.

Edit **only `findings.json`**. Preserve the supplied logs, README, and tests. Do not create scratch files. The final object must contain exactly these fields:

| Field | Required meaning |
|---|---|
| `incident_id` | The investigated incident |
| `request_id` | Its affected request |
| `route_id` | That request's route |
| `change_id` | The change associated with the incident's request/policy chain |
| `policy_at_incident` | The policy effective when the request started |
| `timeout_ms` | That policy's gateway timeout, as an integer |
| `upstream_elapsed_ms` | Milliseconds from request start to upstream completion, computed from event timestamps |
| `cause` | One supported cause label from the list below |
| `mitigation_policy` | The policy actually applied to mitigate this incident |
| `mitigation_timeout_ms` | Its applied timeout, as an integer |
| `evidence` | A list of exact source citations supporting the complete claim chain |

Supported cause labels are `gateway_timeout_below_upstream_duration`, `upstream_failure`, `route_misconfiguration`, `capacity_exhaustion`, and `client_cancelled`. Choose the label supported by the event chain, including the relation between gateway termination and upstream completion.

Every evidence entry has exactly `file`, `line`, and `quote`: an evidence filename, its **one-based line number**, and the **entire exact JSONL line** without its trailing newline. Cite records for incident/request/route/change linkage, the effective policy, request timing and outcome, and the applied mitigation. Include evidence from every file. Do not invent records or quote paraphrases.

Run `python -m unittest -q` after writing the artifact. The visible tests check schema and citation integrity; independent acceptance checks evaluate the investigation's actual conclusions. Report the result and any uncertainty honestly.
