"""Opt-in actual OpenCode/Cline Chat client turns against standalone staging."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from test_live_coding_clients import WireRecorder, assert_formatted_answer
from test_live_thinking_format import formatting_gateway

pytestmark = pytest.mark.live


def _executable(client):
    npm = Path(os.getenv("APPDATA", "")) / "npm/node_modules"
    candidates = ([npm / "opencode-ai/bin/opencode.exe"] if client == "opencode" else
                  list(npm.glob("**/@cline/cli-windows-x64/bin/cline.exe")))
    for path in candidates:
        if path.is_file():
            return str(path)
    found = shutil.which(client)
    if not found or found.endswith((".cmd", ".ps1")):
        pytest.skip(f"install a native {client} executable for this live check")
    return found


def _chat_output(record):
    """Validate the actual client-facing Chat stream and tool JSON by index."""
    lines = [line[6:] for line in record["body"].splitlines() if line.startswith("data: ")]
    assert lines and lines[-1] == "[DONE]"
    text, reasoning, tools, finish = [], [], {}, None
    for line in lines[:-1]:
        event = json.loads(line)
        assert "error" not in event
        for choice in event.get("choices", []):
            assert choice["index"] == 0
            delta = choice.get("delta", {})
            if delta.get("content"):
                text.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for tool in delta.get("tool_calls", []):
                item = tools.setdefault(tool["index"], {"id": "", "name": "", "arguments": ""})
                if tool.get("id"):
                    assert not item["id"] or item["id"] == tool["id"]
                    item["id"] = tool["id"]
                for key in ["name", "arguments"]:
                    item[key] += tool.get("function", {}).get(key) or ""
            if choice.get("finish_reason"):
                assert finish is None
                finish = choice["finish_reason"]
    assert finish in {"stop", "tool_calls", "length"}
    assert len({item["id"] for item in tools.values()}) == len(tools)
    for item in tools.values():
        assert item["id"] and item["name"]
        assert isinstance(json.loads(item["arguments"]), dict)
    answer = "".join(text)
    assert "<think>" not in answer and "</think>" not in answer
    return answer, "".join(reasoning), tools


@pytest.mark.parametrize("provider", ["minimax", "openrouter", "mimo-v2.6-pro", "codex-luna", "gemini-subscription"])
@pytest.mark.parametrize("client_name", ["opencode", "cline"])
def test_actual_chat_client_tools_and_format(formatting_gateway, tmp_path, provider, client_name):
    executable = _executable(client_name)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    files = {"one.txt": 'FORMAT_ONE: 17\nLiteral code escape: \\n\n中文行。\n', "two.txt": "FORMAT_TWO: 25\n"}
    for name, content in files.items():
        (workspace / name).write_text(content, encoding="utf-8")
    before = {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest() for name in files}
    isolated = tmp_path / "client-state"
    isolated.mkdir()
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("OPENCODE_", "CLINE_", "ANTHROPIC_", "OPENAI_")):
            env.pop(key)
    env.update(HOME=str(isolated), USERPROFILE=str(isolated), XDG_CONFIG_HOME=str(isolated / "config"),
               XDG_DATA_HOME=str(isolated / "data"), XDG_CACHE_HOME=str(isolated / "cache"))
    prompt = ('Read one.txt and two.txt using your real file tools. Compute FORMAT_ONE + FORMAT_TWO. '
              'Return a brief Markdown answer containing FORMAT_SUM: <computed sum>, a two-row input table, '
              'and a fenced Python example containing the literal raw string r"\\n". Do not edit files.')
    formatting_gateway.set_policy(target=provider, mode="force", advisor=None)
    timeout = 240 if provider == "gemini-subscription" else 120
    with WireRecorder(allowed_tools={"read_files", "submit_and_exit"} if client_name == "cline" else None) as recorder:
        if client_name == "opencode":
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
                "$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
                "enabled_providers": ["format_test"], "model": "format_test/current", "small_model": "format_test/current",
                "provider": {"format_test": {"npm": "@ai-sdk/openai-compatible", "options": {
                    "baseURL": recorder.base_url + "/v1", "apiKey": "subroute-cli-test"}, "models": {
                    "current": {"name": "Subroute", "reasoning": True, "toolcall": True,
                                "limit": {"context": 200000, "output": 4096}}}}},
                "permission": {"*": "deny", "read": "allow"}, "mcp": {}, "plugin": [],
                "agent": {"format_test": {"mode": "primary", "prompt": "Use read to inspect both files, then answer concisely.",
                                           "permission": {"*": "deny", "read": "allow"}}},
            })
            command = [executable, "run", "--pure", "--format", "json", "--agent", "format_test", "--model", "format_test/current", prompt]
        else:
            state = isolated / "data/settings"
            state.mkdir(parents=True)
            disabled = ["search_codebase", "run_commands", "fetch_web_content", "apply_patch", "editor", "skills", "ask_question",
                        "spawn_agent", "team_spawn_teammate", "team_shutdown_teammate", "team_status", "team_task", "team_run_task",
                        "team_cancel_run", "team_list_runs", "team_await_runs", "team_send_message", "team_broadcast", "team_read_mailbox",
                        "team_mission_log", "team_cleanup", "team_create_outcome", "team_attach_outcome_fragment",
                        "team_review_outcome_fragment", "team_finalize_outcome", "team_list_outcomes"]
            (state / "global-settings.json").write_text(json.dumps({"autoUpdateEnabled": False, "telemetryOptOut": True,
                "toolAutoApprove": True, "disabledTools": disabled, "tools": {"web_search": {"enabled": False}}}), encoding="utf-8")
            (state / "providers.json").write_text(json.dumps({"version": 1, "lastUsedProvider": "openai-compatible", "modes": {},
                "providers": {"openai-compatible": {"settings": {"provider": "openai-compatible", "protocol": "openai-chat",
                    "client": "openai-compatible", "baseUrl": recorder.base_url + "/v1", "apiKey": "subroute-cli-test", "model": "current",
                    "modelCatalog": {"loadLatestOnInit": False, "loadPrivateOnAuth": False}},
                    "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "tokenSource": "manual"}}}), encoding="utf-8")
            env.update(CLINE_DATA_DIR=str(isolated / "data"), CLINE_PROVIDER_SETTINGS_PATH=str(state / "providers.json"),
                       CLINE_GLOBAL_SETTINGS_PATH=str(state / "global-settings.json"))
            # Use the isolated saved provider selection, including base URL.
            command = [executable, "--json", "--config", str(isolated / "config"), "--data-dir", str(isolated / "data"),
                       "--cwd", str(workspace), "--auto-approve", "true",
                       "--compaction", "off", "--thinking", "low", "--retries", "1", "--timeout", str(timeout),
                       "--system", "Use read_files to read only the two supplied files, then return the requested answer. Do not run commands, edit, browse or delegate.",
                       prompt + f' The absolute file paths are {workspace / "one.txt"} and {workspace / "two.txt"}.']
        try:
            result = subprocess.run(command, cwd=workspace, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            def decoded(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            result = subprocess.CompletedProcess(command, 124, decoded(exc.stdout), decoded(exc.stderr))
    for name, value in [("client.jsonl", result.stdout), ("client.stderr", result.stderr), ("wire.json", json.dumps(recorder.records, ensure_ascii=False, indent=2))]:
        (tmp_path / name).write_text(value.replace("subroute-cli-test", "[REDACTED]"), encoding="utf-8")
    (tmp_path / "connection.json").write_text(json.dumps({"upstream": recorder.upstream, "direct_subroute": True}), encoding="utf-8")
    assert result.returncode == 0, result.stderr[-1200:]
    assert recorder.records and all(r["status"] is not None and r["status"] < 400 for r in recorder.records)
    wire = [r for r in recorder.records if r.get("method") == "POST" and "text/event-stream" in r["content_type"] and not r.get("client_disconnected")]
    assert 2 <= len(wire) <= 5
    replay = json.dumps([m for r in wire for m in r["request"].get("messages", []) if m.get("role") == "tool"], ensure_ascii=False)
    assert "FORMAT_ONE: 17" in replay and "FORMAT_TWO: 25" in replay
    structured = [_chat_output(r) for r in wire]
    answers = [item[0] for item in structured]
    assert all(not thought or len(thought) <= 30 or thought not in text for text, thought, _ in structured), "Private thinking was mirrored into visible text"
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert not any(e.get("type") == "error" for e in events)
    if client_name == "opencode":
        answer = "".join(e["part"]["text"] for e in events if e.get("type") == "text")
        assert any(e.get("type") == "step_finish" and e["part"]["reason"] == "stop" for e in events)
    else:
        native = [e["event"] for e in events if e.get("type") == "agent_event"]
        assert not any(e.get("type") == "error" for e in native)
        done = [e for e in native if e.get("type") == "done"]
        assert len(done) == 1 and done[0]["reason"] == "completed"
        answer = done[0]["text"]
        completed = [e for e in native if e.get("type") == "content_end" and e.get("contentType") == "tool"]
        actual_reads = [e for e in completed if e.get("toolName") == "read_files"]
        assert "FORMAT_ONE: 17" in json.dumps(actual_reads, ensure_ascii=False) and "FORMAT_TWO: 25" in json.dumps(actual_reads, ensure_ascii=False)
        started = {e["toolCallId"]: e["toolName"] for e in native if e.get("type") == "content_start" and e.get("contentType") == "tool"}
        assert started == {e["toolCallId"]: e["toolName"] for e in completed}
    assert answer and answers[-1] == answer
    assert_formatted_answer(answer)
    assert before == {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest() for name in files}
    print(f"{client_name}/{provider}: direct Subroute; real tool replay and Chat/native output verified; evidence={tmp_path}")
