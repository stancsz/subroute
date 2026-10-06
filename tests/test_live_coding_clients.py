"""Opt-in actual Claude Code/Codex tool turns through Subroute directly.

Uses temporary files and process-only settings; restores staging policy. A
loopback recorder saves header-free wire/client evidence in the pytest folder.
No mock provider response is used. Native terminal visuals are not asserted.
"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Thread
from urllib.parse import urlparse

import httpx
import pytest

from test_live_thinking_format import formatting_gateway
from test_thinking_format import collect_messages
from test_client_stream_contracts import collect_responses

pytestmark = pytest.mark.live


def assert_formatted_answer(answer):
    plain_markdown = re.sub(r"[*`]", "", answer)
    assert re.search(r"FORMAT_SUM:\s*42", plain_markdown) and "\n" in answer and "```" in answer and "\\n" in answer
    assert "\x00" not in answer, "The displayed answer must not contain NUL control characters"
    assert "<think>" not in answer and "</think>" not in answer
    assert "START_FOLLOW_CONTRACT" not in answer and "END_FOLLOW_CONTRACT" not in answer, "Provider control markers leaked into the answer"
    assert len(re.findall(r"(?m)^\s*```", answer)) % 2 == 0, "Every code fence must close"
    assert len(re.findall(r"FORMAT_SUM:\s*42", plain_markdown)) == 1, "The brief final answer must not repeat the computed result"


class WireRecorder:
    def __init__(self, upstream="http://127.0.0.1:4005", allowed_tools=None):
        self.records = []
        self.upstream = upstream.rstrip("/")
        self.allowed_tools = allowed_tools
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                # Native Codex asks for model metadata as well as Responses.
                # Forward the customer entry point instead of returning 501.
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() not in {"host", "connection", "accept-encoding"}}
                record = {"method": "GET", "path": self.path, "request": {}, "body": "", "status": None, "content_type": ""}
                owner.records.append(record)
                with httpx.Client(timeout=10) as client:
                    response = client.get(f"{owner.upstream}{self.path}", headers=headers)
                record.update(status=response.status_code, content_type=response.headers.get("content-type", ""), body=response.text)
                self.send_response(response.status_code)
                self.send_header("content-type", response.headers.get("content-type", "application/json"))
                self.send_header("content-length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            def do_POST(self):
                payload = self.rfile.read(int(self.headers.get("content-length", "0")))
                record = {"method": "POST", "path": self.path, "request": json.loads(payload), "body": "", "status": None, "content_type": ""}
                owner.records.append(record)
                if owner.allowed_tools is not None:
                    names = {tool.get("function", {}).get("name") for tool in record["request"].get("tools", [])}
                    if not names <= owner.allowed_tools:
                        body = json.dumps({"error": {"message": "Client test exposed tools outside its read-only allowlist"}}).encode()
                        record.update(status=400, content_type="application/json", body=body.decode())
                        self.send_response(400)
                        self.send_header("content-type", "application/json")
                        self.send_header("content-length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}}
                with httpx.Client(timeout=120) as client:
                    with client.stream("POST", f"{owner.upstream}{self.path}", headers=headers, content=payload) as upstream:
                        record["status"] = upstream.status_code
                        record["content_type"] = upstream.headers.get("content-type", "")
                        pieces = []
                        try:
                            self.send_response(upstream.status_code)
                            self.send_header("content-type", record["content_type"])
                            self.send_header("connection", "close")
                            self.end_headers()
                            for piece in upstream.iter_bytes():
                                pieces.append(piece)
                                self.wfile.write(piece)
                                self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            record["client_disconnected"] = True
                        finally:
                            record["body"] = b"".join(pieces).decode("utf-8", errors="replace")
                            self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Join request workers before saving evidence, including background
        # client requests. Otherwise unfinished records can masquerade as SSE.
        self.server.daemon_threads = False
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}"


def _executables(client):
    npm = Path(os.getenv("APPDATA", "")) / "npm/node_modules"
    if client == "claude":
        executable = npm / "@anthropic-ai/claude-code/bin/claude.exe"
    else:
        matches = list((npm / "@openai").glob("**/bin/codex.exe"))
        executable = matches[0] if matches else None
    if executable is not None and executable.is_file():
        return str(executable)
    executable = shutil.which(client)
    if not executable or executable.endswith((".cmd", ".ps1")):
        pytest.skip(f"install a native {client} executable for the CLI live check")
    return executable


@pytest.mark.parametrize("provider", ["minimax", "openrouter", "mimo-v2.6-pro", "codex-luna", "gemini-subscription"])
@pytest.mark.parametrize("client_name", ["claude", "codex"])
def test_actual_client_thinking_tools_and_answer(formatting_gateway, tmp_path, provider, client_name):
    executable = _executables(client_name)
    upstream = os.getenv("SUBROUTE_CLI_BASE_URL", "http://127.0.0.1:4005")
    address = urlparse(upstream)
    assert address.scheme == "http" and address.hostname in {"127.0.0.1", "localhost", "::1"}
    assert address.port in {4005, 11435}, "Only staging or its local forwarder may be mutated by this test"
    token = os.getenv("SUBROUTE_LOCAL_API_KEY") if address.port == 11435 else os.getenv("SUBROUTE_LIVE_API_KEY", "subroute-cli-test")
    if not token:
        pytest.skip("set SUBROUTE_LOCAL_API_KEY to authenticate the local-forwarder CLI test")
    files = {"one.txt": 'FORMAT_ONE: 17\nLiteral code escape: \\n\n中文行。\n', "two.txt": "FORMAT_TWO: 25\n"}
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    if os.name == "nt":
        # pytest's private temporary ACL otherwise blocks Codex's low-privilege
        # Windows sandbox. Grant read/traverse only on this synthetic fixture.
        subprocess.run(["icacls", str(tmp_path), "/grant", "*S-1-5-32-545:(OI)(CI)RX"],
                       check=True, capture_output=True)
    before = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in files}
    formatting_gateway.set_policy(target=provider, mode="force", advisor=None)
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE_")):
            env.pop(key)
    prompt = (f'Read {tmp_path / "one.txt"} and {tmp_path / "two.txt"} using your file tools. '
              'Compute and verify FORMAT_ONE + FORMAT_TWO. '
              'Return a brief Markdown answer containing FORMAT_SUM: <computed sum>, a two-row table of the inputs, '
              'and a fenced Python code example containing the literal raw string r"\\n". '
              'Do not edit files. Do not use image tools for these text files.')
    # The subscription sidecar serializes whole completions, not token streams.
    # Allow its tool round trips while retaining a bounded overall deadline.
    cli_timeout = 240 if provider == "gemini-subscription" else 120
    with WireRecorder(upstream) as recorder:
        if client_name == "claude":
            env.update(ANTHROPIC_BASE_URL=recorder.base_url, ANTHROPIC_API_KEY=token,
                       CLAUDE_CODE_MAX_OUTPUT_TOKENS="4096", MAX_THINKING_TOKENS="1024")
            command = [executable, "-p", "--bare", "--no-session-persistence", "--setting-sources", "",
                       "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
                       "--tools", "Read", "--allowedTools", "Read", "--permission-mode", "dontAsk",
                       "--model", "current" if address.port == 4005 else "claude-sonnet-4-6", "--settings", '{"alwaysThinkingEnabled":true}',
                       "--output-format", "stream-json", "--include-partial-messages", "--verbose",
                       "--system-prompt", "You are a coding assistant. Use Read for the requested files, then answer concisely."]
        else:
            env["SUBROUTE_CLI_TEST_KEY"] = token
            command = [executable, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                       "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
                       "--cd", str(tmp_path),
                       "--enable", "skip_host_skill_discovery", "--disable", "apps", "--disable", "hooks",
                       "--disable", "plugins", "--disable", "remote_plugin", "--disable", "skill_search",
                       "--disable", "view_image", "--disable", "image_generation",
                       "-m", "current" if address.port == 4005 else "gpt-6-luna", "-c", 'model_provider="format_test"',
                       "-c", 'model_providers.format_test={name="Local format test",base_url="' + recorder.base_url + '/v1",wire_api="responses",env_key="SUBROUTE_CLI_TEST_KEY"}',
                       "-c", 'model_reasoning_effort="low"', "-c", 'model_reasoning_summary="auto"',
                       "-c", "model_context_window=200000", "-c", "model_auto_compact_token_limit=190000",
                       "-c", 'web_search="disabled"', "-c", 'windows.sandbox="unelevated"', "-"]
        try:
            result = subprocess.run(command, input=prompt, cwd=tmp_path, env=env,
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=cli_timeout)
        except subprocess.TimeoutExpired as error:
            # Preserve failure evidence too; a timeout must not become a skip.
            def decoded(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            result = subprocess.CompletedProcess(command, 124, decoded(error.stdout),
                                                 decoded(error.stderr) + f"\nCLI exceeded the {cli_timeout}-second test limit")
    # Keep credentials out of evidence, even if a client prints one unexpectedly.
    for name, text in [("client.jsonl", result.stdout), ("client.stderr", result.stderr),
                       ("wire.json", json.dumps(recorder.records, ensure_ascii=False, indent=2))]:
        (tmp_path / name).write_text(text.replace(token, "[REDACTED]"), encoding="utf-8")
    (tmp_path / "connection.json").write_text(json.dumps({"upstream": upstream, "direct_subroute": address.port == 4005}), encoding="utf-8")
    assert result.returncode == 0, result.stderr[-1200:]
    assert "without active item" not in result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    wire = [record for record in recorder.records if "text/event-stream" in record.get("content_type", "") and not record.get("client_disconnected")]
    assert 2 <= len(wire) <= 5, "The client should use tools and then finish, without a repeating tool loop"
    tool_results = []
    for record in wire:
        request = record["request"]
        for message in request.get("messages", []):
            for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
                if block.get("type") == "tool_result":
                    tool_results.append(block.get("content"))
        for item in request.get("input", []):
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                tool_results.append(item.get("output"))
    replay = json.dumps(tool_results, ensure_ascii=False)
    assert "FORMAT_ONE: 17" in replay and "FORMAT_TWO: 25" in replay, "Both real file results must be sent back to the provider"
    for record in wire:
        assert record["status"] == 200
        chunks = [json.loads(line[6:]) for line in record["body"].splitlines()
                  if line.startswith("data: ") and line[6:] != "[DONE]"]
        if client_name == "claude":
            blocks = collect_messages(chunks)
            text = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
        else:
            output = collect_responses(chunks)
            text = "".join(part["text"] for item in output if item["type"] == "message" for part in item.get("content", []))
        assert "<think>" not in text and "</think>" not in text
    if client_name == "claude":
        final = next(e for e in events if e.get("type") == "result")
        assert final["subtype"] == "success" and not final.get("is_error")
        answer = final["result"]
    else:
        assert any(e.get("type") == "turn.completed" for e in events)
        messages = [e["item"]["text"] for e in events if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"]
        assert messages
        answer = messages[-1]
        assert all("<think>" not in message and "</think>" not in message for message in messages)
    assert_formatted_answer(answer)
    assert before == {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in files}
    errors = [record["status"] for record in recorder.records if record["status"] is None or record["status"] >= 400]
    print(f"{client_name}/{provider}: upstream={upstream}; completed real tool turn; wire/client structure verified; HTTP errors={errors}; evidence={tmp_path}")
    assert not errors, "A successful final turn must not conceal failed customer requests"
