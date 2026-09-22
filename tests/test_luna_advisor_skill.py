import importlib.util
import io
from pathlib import Path
import sys
import json
import subprocess
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
CALLER = ROOT / "skills" / "luna-advisor-escalation" / "scripts" / "ask_expert.py"


def load_caller():
    spec = importlib.util.spec_from_file_location("luna_advisor_caller", CALLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_packaged_caller_reconfigures_legacy_windows_stdout_to_utf8():
    caller = load_caller()
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")

    caller.configure_utf8_stdout(stream)
    stream.write("Astra 建议 ✅")
    stream.flush()

    assert raw.getvalue() == "Astra 建议 ✅".encode("utf-8")


def load_reader():
    spec = importlib.util.spec_from_file_location("expert_reader", CALLER.with_name("expert_reader.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("retries = 0\n", encoding="utf-8")
    (repo / "src" / "auth.json").write_text('{"private":true}', encoding="utf-8")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    (repo / "src" / "untracked.py").write_text("not approved", encoding="utf-8")
    return repo


def evidence():
    return json.dumps({"findings": [{"file": "src/app.py", "line": 1,
        "quote": "retries = 0", "fact": "Retries are disabled in source."}],
        "unknowns": "Runtime behavior not verified."})


def test_snapshot_limits_authority_and_keeps_current_edits(source_repo, tmp_path):
    reader = load_reader()
    (source_repo / "src" / "app.py").write_text("retries = 1\n", encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    result = reader.snapshot_sources(source_repo, ["src"], snapshot)
    assert list(result["files"]) == ["src/app.py"]
    assert result["excluded_files"] == 1
    assert (snapshot / "src/app.py").read_text() == "retries = 1\n"
    with pytest.raises(ValueError, match="escapes"):
        reader.snapshot_sources(source_repo, [".."], tmp_path / "bad")


@pytest.mark.parametrize("mutation", ["escape", "invented_quote", "wrong_line", "oversized"])
def test_reader_rejects_unverifiable_evidence(source_repo, mutation):
    reader = load_reader()
    data = json.loads(evidence())
    if mutation == "escape":
        data["findings"][0]["file"] = "../outside.py"
    elif mutation == "invented_quote":
        data["findings"][0]["quote"] = "retries = 9"
    elif mutation == "wrong_line":
        data["findings"][0]["line"] = 999
    else:
        data["unknowns"] = "x" * 2001
    with pytest.raises(ValueError):
        reader.validate_evidence(json.dumps(data), source_repo)


@pytest.mark.parametrize("decision,worker_ok,expected_calls,status", [
    ('{"read":"What is the retry setting?"}', True, 2, "ok"),
    ('{"read":"What is the retry setting?"}', False, 1, "unavailable"),
    ('{"advice":"Verdict: Enough.\\nNext: Verify.\\nRisk: none"}', True, 1, "ok"),
    ('not JSON', True, 1, "unavailable"),
    ('{"read":"question", "advice":"ambiguous"}', True, 1, "unavailable"),
])
def test_expert_controls_read_without_luna_history_or_retries(
        source_repo, monkeypatch, decision, worker_ok, expected_calls, status):
    reader = load_reader()
    monkeypatch.setattr(reader, "pi_package", lambda: source_repo)
    calls, questions = [], []
    def consult(args, messages):
        calls.append(messages)
        return {"advice": decision if len(calls) == 1 else '{"advice":"Verdict: Inspect deployment.\\nNext: Test.\\nRisk: none"}',
                "usage": {"total_tokens": 50}}
    def pi(package, snapshot, question, model, **kwargs):
        if kwargs.get("preflight"):
            return {"status": "ok"}
        questions.append(question)
        return {"status": "ok" if worker_ok else "unavailable", "evidence": evidence()}
    monkeypatch.setattr(reader, "run_pi", pi)
    args = SimpleNamespace(reader_root=source_repo, reader_scope=["src"], reader_model="current")
    result = reader.run_with_reader(args, "Luna hypothesis: broken retries.", consult, "Be concise.")
    assert result["status"] == status
    assert len(calls) == expected_calls
    assert len(result["expert_calls"]) == expected_calls
    assert all(q == "What is the retry setting?" for q in questions)
    if expected_calls == 2:
        assert "retries = 0" in calls[1][-1]["content"]
        assert "advice" in result
    if status == "unavailable":
        assert "advice" not in result


def test_bad_reader_citation_stops_before_paid_followup(source_repo, monkeypatch):
    reader = load_reader()
    monkeypatch.setattr(reader, "pi_package", lambda: source_repo)
    monkeypatch.setattr(reader, "run_pi", lambda *a, **kw: {"status": "ok"} if kw else {
        "status": "ok", "evidence": evidence().replace("retries = 0", "retries = 8")})
    calls = []
    def consult(*args):
        calls.append(args)
        return {"advice": '{"read":"Inspect retries"}', "usage": {"total_tokens": 10}}
    args = SimpleNamespace(reader_root=source_repo, reader_scope=["src"], reader_model="current")
    result = reader.run_with_reader(args, "packet", consult, "instruction")
    assert result["status"] == "unavailable"
    assert len(calls) == 1
    assert result["expert_calls"][0]["usage"]["total_tokens"] == 10


def test_incomplete_expert_is_not_advice(monkeypatch):
    caller = load_caller()
    body = {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}
    monkeypatch.setattr(caller, "urlopen", lambda *a, **k: io.StringIO(json.dumps(body)))
    args = SimpleNamespace(model="sol", base_url="http://localhost:4040/v1", timeout_seconds=1)
    with pytest.raises(RuntimeError, match="completed"):
        caller.consult(args, [])


@pytest.mark.parametrize("batches,expected_expert,expected_pi", [
    ([["q1", "q2", "q3"]], 2, 3),
    ([["q1"], ["q2", "q3"]], 3, 3),
    ([["q1"], ["q2"]], 3, 2),
])
def test_total_three_expert_three_pi_budget(source_repo, monkeypatch, batches, expected_expert, expected_pi):
    reader = load_reader()
    monkeypatch.setattr(reader, "pi_package", lambda: source_repo)
    questions, calls = [], []
    def pi(package, snapshot, question, model, **kwargs):
        if not kwargs.get("preflight"):
            questions.append(question)
        return {"status": "ok", "evidence": evidence()}
    monkeypatch.setattr(reader, "run_pi", pi)
    def consult(args, messages):
        index = len(calls)
        calls.append(messages)
        answer = json.dumps({"read": batches[index]}) if index < len(batches) else "Verdict: enough.\nNext: Test.\nRisk: none"
        return {"advice": answer, "usage": {"total_tokens": 10}}
    args = SimpleNamespace(reader_root=source_repo, reader_scope=["src"], reader_model="current")
    result = reader.run_with_reader(args, "packet", consult, "final")
    assert result["status"] == "ok"
    assert len(calls) == expected_expert
    assert len(questions) == expected_pi


@pytest.mark.parametrize("batch", [["q"] * 2, ["q1", "q2", "q3", "q4"], []])
def test_invalid_or_repeat_batch_spends_no_pi_calls(source_repo, monkeypatch, batch):
    reader = load_reader()
    monkeypatch.setattr(reader, "pi_package", lambda: source_repo)
    def pi(*args, **kwargs):
        assert kwargs.get("preflight"), "no inference allowed for invalid batch"
        return {"status": "ok"}
    monkeypatch.setattr(reader, "run_pi", pi)
    args = SimpleNamespace(reader_root=source_repo, reader_scope=["src"], reader_model="current")
    result = reader.run_with_reader(args, "packet", lambda *a: {"advice": json.dumps({"read": batch})}, "final")
    assert result["status"] == "unavailable"
    assert len(result["expert_calls"]) == 1


def test_preflight_failure_does_not_spend_expert_tokens(source_repo, monkeypatch):
    reader = load_reader()
    monkeypatch.setattr(reader, "pi_package", lambda: source_repo)
    monkeypatch.setattr(reader, "run_pi", lambda *a, **kw: {"status": "unavailable"})
    def consult(*args):
        pytest.fail("Expert should not be called")
    args = SimpleNamespace(reader_root=source_repo, reader_scope=["src"], reader_model="current")
    result = reader.run_with_reader(args, "packet", consult, "instruction")
    assert result["status"] == "unavailable"
    assert result["expert_calls"] == []


@pytest.mark.parametrize("finish,content,exit_code", [
    ("stop", "Astra 建议 ✅", 0), ("length", "Astra 建议 ✅", 1),
    ("stop", "", 1), ("invalid_json", "", 1),
])
def test_real_cli_unicode_and_incomplete_response_boundary(finish, content, exit_code):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = {"choices": [{"finish_reason": finish, "message": {"content": content}}],
                    "model": "test-stub", "usage": {"total_tokens": 1}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"not JSON" if finish == "invalid_json" else
                             json.dumps(body, ensure_ascii=False).encode("utf-8"))
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
    environment.pop("EXPERTS_API_KEY", None)
    try:
        result = subprocess.run([sys.executable, str(CALLER), "--question", "test",
            "--base-url", f"http://127.0.0.1:{server.server_port}/v1"],
            env=environment, capture_output=True, timeout=10)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.returncode == exit_code
    receipt = json.loads(result.stdout.decode("utf-8"))
    if exit_code == 0:
        assert receipt["advice"] == content
    else:
        assert receipt["status"] == "unavailable"
        assert "advice" not in receipt
