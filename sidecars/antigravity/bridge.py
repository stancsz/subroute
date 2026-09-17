"""Private, bounded HTTP bridge for the container-local Antigravity CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _response_text(stdout: str) -> str:
    deltas: list[str] = []
    result = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "step_update":
            delta = event.get("step_update", {}).get("text_delta", "")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event.get("event") == "result":
            candidate = event.get("result", {}).get("response", "")
            if isinstance(candidate, str):
                result = candidate
    content = result or "".join(deltas)
    if not content:
        raise ValueError("Antigravity returned no response text")
    return content


def _provider_usage(stdout: str) -> dict[str, int]:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "result":
            continue
        raw = event.get("result", {}).get("usage", {})
        keys = ("input_tokens", "output_tokens", "total_tokens")
        if all(type(raw.get(key)) is int and raw[key] >= 0 for key in keys):
            return {key: raw[key] for key in keys}
    raise ValueError("Antigravity returned no valid provider usage")


def _runtime_status() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["agy", "models"], text=True, capture_output=True, timeout=15, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "ready", "authenticated": False, "models": [], "detail": "Model inventory timed out"}
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        if "Please sign in" in combined:
            detail = "Docker bridge ready; Antigravity sign-in required"
        else:
            detail = (combined.strip() or f"agy models exited with {completed.returncode}")[:300]
        return {"status": "ready", "authenticated": False, "models": [], "detail": detail}
    models = [
        line.split("\t", 1)[0].strip()
        for line in completed.stdout.splitlines()
        if "\t" in line and line.split("\t", 1)[0].strip()
    ]
    return {
        "status": "ready",
        "authenticated": True,
        "models": models,
        "detail": f"Authenticated; {len(models)} Gemini subscription models available",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AntigravityBridge/1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ready"})
            return
        if self.path == "/v1/status":
            self._json(HTTPStatus.OK, _runtime_status())
            return
        self._json(HTTPStatus.NOT_FOUND, {"detail": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/completions":
            self._json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 100_000:
                raise ValueError("request body must be between 1 and 100000 bytes")
            payload = json.loads(self.rfile.read(length))
            model, prompt = payload["model"], payload["prompt"]
            if not isinstance(model, str) or not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("model and non-empty prompt are required strings")
            completed = subprocess.run(
                ["agy", "--model", model, "--output-format", "stream-json", "--print-timeout", "2m", "--print", prompt],
                text=True, capture_output=True, timeout=125, check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip()[:300] or f"agy exited with {completed.returncode}")
            content = _response_text(completed.stdout)
            print(
                f"completion succeeded model={model!r} output_chars={len(content)}",
                file=sys.stderr,
                flush=True,
            )
            self._json(
                HTTPStatus.OK,
                {"content": content, "usage": _provider_usage(completed.stdout)},
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
        except subprocess.TimeoutExpired:
            self._json(HTTPStatus.GATEWAY_TIMEOUT, {"detail": "Antigravity timed out"})
        except RuntimeError as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"detail": str(exc)})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 4015), Handler).serve_forever()
