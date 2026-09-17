import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "sidecars" / "antigravity" / "bridge.py"
SPEC = importlib.util.spec_from_file_location("antigravity_bridge", BRIDGE_PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def test_response_text_requires_provider_content():
    payload = '\n'.join([
        '{"event":"step_update","step_update":{"text_delta":"partial"}}',
        '{"event":"result","result":{"response":"complete"}}',
    ])

    assert bridge._response_text(payload) == "complete"


def test_provider_usage_requires_terminal_provider_counts():
    payload = '{"event":"result","result":{"usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15}}}'

    assert bridge._provider_usage(payload) == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
    }


def test_runtime_status_distinguishes_sign_in_from_bridge_health(monkeypatch):
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="Fetching available models...",
            stderr="Error: Please sign in to view available models.",
        ),
    )

    assert bridge._runtime_status() == {
        "status": "ready",
        "authenticated": False,
        "models": [],
        "detail": "Docker bridge ready; Antigravity sign-in required",
    }


def test_runtime_status_reports_authenticated_model_inventory(monkeypatch):
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="gemini-a\tGemini A\ngemini-b\tGemini B\n",
            stderr="",
        ),
    )

    assert bridge._runtime_status() == {
        "status": "ready",
        "authenticated": True,
        "models": ["gemini-a", "gemini-b"],
        "detail": "Authenticated; 2 Gemini subscription models available",
    }


def test_runtime_status_bounds_inventory_timeout(monkeypatch):
    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="agy models", timeout=15)

    monkeypatch.setattr(bridge.subprocess, "run", time_out)

    assert bridge._runtime_status()["detail"] == "Model inventory timed out"
