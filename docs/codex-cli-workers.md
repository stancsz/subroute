# Codex CLI workers in this project

This guide covers how the parent session delegates real implementation work to a
Codex CLI worker and how the parent proves that the work actually happened.

## The worker

- Profile: `openrouter-minimax`, layered from `$CODEX_HOME/openrouter-minimax.config.toml`.
- Model: `minimax/minimax-m3.1-flash` served by OpenRouter (`model_provider = "openrouter"`), with `OPENROUTER_API_KEY` set in the calling shell.
- Sandbox: `workspace-write`, so the worker can edit inside the workspace and run commands. It is not `read-only` and it is not `danger-full-access`.
- Output: `--json` JSONL events on stdout, so the parent can inspect native tool activity rather than only the final prose.

The profile is local to the operator's machine and is not committed with this repository. Create `$CODEX_HOME/openrouter-minimax.config.toml` (normally `~/.codex/openrouter-minimax.config.toml`) with the following settings, and provide `OPENROUTER_API_KEY` through the environment. Do not put credentials in the profile or repository. Existing processes may need to restart to inherit a newly saved user environment variable.

```toml
model = "minimax/minimax-m3.1-flash"
model_provider = "openrouter"
model_reasoning_effort = "low"
model_reasoning_summary = "none"
web_search = "disabled"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
wire_api = "responses"
```

Typical invocation shape:

```powershell
Get-Content -Raw -LiteralPath task.md | codex exec --profile openrouter-minimax --sandbox workspace-write --json -
```

Keep the prompt in a file or a here-string so the task, scope, and acceptance are auditable and repeatable.

## Bounded task and write scope

Every delegation states, before the worker starts:

1. One objective, small enough to finish in a single run.
2. The exact files the worker is allowed to create or modify. Everything else is out of scope.
3. The commands the worker is expected to run, including the specific test command.
4. What the worker must report back: files changed, commands run, test output, and anything it could not complete.

The worker is instructed to stop and report rather than widen its own scope. If a needed file is outside the declared scope, that is a finding, not a licence to edit it.

## Real implementation, not smoke checks

A delegated worker that only reads files and prints a summary has not done the task. A useful run actually:

- edits the declared files;
- runs the relevant commands, including the project's test suite or the focused test file;
- reports the real command output, including failures.

A read-only result is a legitimate outcome when the task was blocked, but it must be reported as a blocked read-only run, never as a completed implementation.

## Reviewing native command and file-change events

Read the JSONL stream, not just the final agent message. The events that carry proof are the native item events:

- `command_execution` items show the command line, the exit code, and the aggregated output.
- `file_change` items show which paths were changed and how.
- `mcp_tool_call` items show MCP tool use, including failures.
- `error` items and non-zero exit codes mean the run did not complete cleanly.

A worker may write through a native shell command or a `file_change` event. Inspect the relevant event and independently inspect the resulting file and diff; the final message alone does not prove an edit. A worker that claims a test passed but produced no `command_execution` event with the test command and a zero exit code has not been shown to have run the test.

## Text response versus proven execution

Keep these layers separate in the handoff:

| Layer | Evidence required |
| --- | --- |
| Worker text | The agent's own final message. A claim, not proof. |
| Native events | `command_execution` or `file_change` items in the JSONL stream, as applicable. |
| Process outcome | Exit code and completion of the `codex exec` process. |
| Independent verification | The parent re-reading the diff and re-running the tests itself. |

Exit code 0 means the process finished. It does not mean the task succeeded, that a file was written, or that a test asserted anything.

## Independent verification by the parent

The parent does not take the worker's word for it:

- Inspect `git status --short` and `git diff` to confirm the change set matches the declared write scope and nothing else moved.
- Re-run the relevant test command locally and read the real result.
- Read the changed code and judge whether it does what the task asked, not whether the worker described it that way.
- Treat a test that was not re-run by the parent as unverified, even if the worker reported it passing.

## Commit and push stay with the parent

Workers do not commit and do not push. The parent reviews the working tree, makes the commit, and pushes only when that is authorized. When a delegation is scoped to a single file, a commit should contain that file and nothing else.

## Policy denials are not to be bypassed

If the sandbox, an approval policy, a tool, or a host rule denies an action, stop and report the exact denial. Do not retry with an alternative tool, do not switch to a weaker sandbox, do not use `--dangerously-bypass-approvals-and-sandbox`, and do not route around a block by rephrasing the request until it passes. A denial is a result the parent needs to see. Whether to escalate, adjust the scope, or accept the limitation is the parent's call and may need the user.

## Limitations

- On 2026-11-17, a MiniMax worker using this profile and `workspace-write` executed native shell commands and created this guide in an isolated Subroute worktree. Its structured stream recorded successful command executions and `turn.completed`, and its process exited 0. The parent inspected the document. This verifies a documentation task, not a code implementation or a project test suite.
- Worker tool availability depends on the effective `CODEX_HOME` and project trust, as recorded in `docs/misc/web-search-cli.md`. A missing MCP server or a missing trust record can make a run look like a model failure.
- The upstream CLI version in use here is `codex-cli 0.154.0`. Re-check `codex exec --help` before relying on flag names.
