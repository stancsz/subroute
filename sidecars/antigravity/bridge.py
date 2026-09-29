"""Private, bounded HTTP bridge for the container-local Antigravity CLI."""

from __future__ import annotations

import json
import logging
import os
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
AGY_ADVISOR_AGENT = "subroute-advisor"
AGY_TARGET_AGENT = "subroute-target"
MAX_REQUEST_BYTES = 1_000_000
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MAX_CONCURRENT_CLI_REQUESTS = 4
CLI_ADMISSION_WAIT_SECONDS = 0.5
CLI_TIMEOUT_SECONDS = 125
CLI_POLL_SECONDS = 0.1
CLI_TERMINATE_GRACE_SECONDS = 2
_CLI_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CLI_REQUESTS)


class ClientDisconnected(Exception):
    """The caller left before the CLI produced a terminal result."""


def _terminate_and_reap(process: subprocess.Popen[str]) -> None:
    """Stop the CLI process group, then drain and reap it before releasing its slot."""
    # In Compose the parent can exit while descendants still hold its pipes.
    # Signal the process group even after the parent has been reaped.
    if os.name != "posix" and process.poll() is not None:
        process.communicate()
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=CLI_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.communicate()


def _run_agy(
    command: list[str],
    prompt: str,
    *,
    timeout: float = CLI_TIMEOUT_SECONDS,
    client_disconnected: Any = None,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded CLI process and always kill/reap it on timeout or disconnect."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )
    deadline = time.monotonic() + timeout
    input_text: str | None = prompt
    try:
        while True:
            if client_disconnected is not None and client_disconnected():
                raise ClientDisconnected()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(
                    input=input_text,
                    timeout=min(CLI_POLL_SECONDS, remaining),
                )
                break
            except subprocess.TimeoutExpired:
                # communicate() retains its pipe state, so subsequent polls can
                # drain output without resending input.
                input_text = None
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except (ClientDisconnected, subprocess.TimeoutExpired):
        _terminate_and_reap(process)
        raise
    except BaseException:
        _terminate_and_reap(process)
        raise


def _agy_command(
    model: str,
    *,
    json_schema_path: str | None = None,
    advisor: bool = False,
) -> list[str]:
    """Run AGY as a constrained text or structured-output target, not a workspace agent."""
    command = [
        "agy",
        "--agent", AGY_ADVISOR_AGENT if advisor else AGY_TARGET_AGENT,
        "--model", model,
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--print-timeout", "2m",
        "--sandbox",
    ]
    if json_schema_path is not None:
        command.extend(["--json-schema", json_schema_path])
    return command


def _prompt_input(prompt: str) -> str:
    """Keep user content off argv, which is subject to the OS per-argument size limit."""
    return json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"


def _terminal_result(stdout: str) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            if result is not None:
                raise RuntimeError("Antigravity CLI returned multiple terminal results")
            candidate = event.get("result")
            if not isinstance(candidate, dict):
                raise RuntimeError("Antigravity CLI returned an invalid terminal result")
            result = candidate
    if result is None:
        raise RuntimeError("Antigravity CLI returned no terminal result event")
    if result.get("status") != "SUCCESS":
        status = str(result.get("status") or "unknown")
        reason = result.get("error")
        detail = f": {str(reason)[:300]}" if isinstance(reason, str) and reason else ""
        raise RuntimeError(f"Antigravity CLI ended with status {status}{detail}")
    return result


def _response_text(stdout: str) -> str:
    result = _terminal_result(stdout)
    content = result.get("response")
    if not isinstance(content, str) or not content:
        raise RuntimeError("Antigravity returned no response text (status=SUCCESS)")
    return content


def _provider_usage(stdout: str) -> dict[str, int]:
    raw = _terminal_result(stdout).get("usage")
    keys = ("input_tokens", "output_tokens", "total_tokens")
    if isinstance(raw, dict) and all(type(raw.get(key)) is int and raw[key] >= 0 for key in keys):
        return {key: raw[key] for key in keys}
    raise RuntimeError("Antigravity returned no valid provider usage")


def _runtime_status() -> dict[str, Any]:
    if not _CLI_SLOTS.acquire(timeout=CLI_ADMISSION_WAIT_SECONDS):
        return {
            "status": "ready", "authenticated": None, "models": [],
            "detail": "Subscription CLI at capacity; model inventory unavailable",
        }
    try:
        try:
            completed = _run_agy(["agy", "models"], "", timeout=15)
        except subprocess.TimeoutExpired:
            return {"status": "ready", "authenticated": None, "models": [], "detail": "Model inventory timed out"}
        except OSError:
            return {"status": "ready", "authenticated": None, "models": [], "detail": "Unable to start model inventory"}
    finally:
        _CLI_SLOTS.release()
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        if "Please sign in" in combined:
            detail = "Docker bridge ready; Antigravity sign-in required"
        else:
            detail = (combined.strip() or f"agy models exited with {completed.returncode}")[:300]
        authenticated = False if "Please sign in" in combined else None
        return {"status": "ready", "authenticated": authenticated, "models": [], "detail": detail}
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
        request_id = self._request_id()
        if request_id is not None:
            self.send_header("X-Subroute-Request-ID", request_id)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _request_id(self) -> str | None:
        candidate = self.headers.get("X-Subroute-Request-ID", "")
        return candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else None

    def _client_disconnected(self) -> bool:
        """Use a non-consuming socket peek to notice EOF while the CLI is running."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

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
        request_id = self._request_id() or "untracked"
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_REQUEST_BYTES:
                raise ValueError(f"request body must be between 1 and {MAX_REQUEST_BYTES} bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            model, prompt = payload["model"], payload["prompt"]
            json_schema = payload.get("json_schema")
            advisor = payload.get("advisor", False)
            if not isinstance(advisor, bool):
                raise ValueError("advisor must be a boolean")
            if not isinstance(model, str) or not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("model and non-empty prompt are required strings")
            if json_schema is not None and not isinstance(json_schema, dict):
                raise ValueError("json_schema must be an object")
            if not _CLI_SLOTS.acquire(timeout=CLI_ADMISSION_WAIT_SECONDS):
                logger.warning("AGY request rejected at capacity request_id=%s model=%r", request_id, model)
                self._json(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"detail": "Antigravity is at CLI capacity; retry the request"},
                )
                return
            try:
                with tempfile.TemporaryDirectory(prefix="subroute-agy-") as temp_dir:
                    schema_path = None
                    if json_schema is not None:
                        schema_path = str(Path(temp_dir) / "schema.json")
                        Path(schema_path).write_text(
                            json.dumps(json_schema, separators=(",", ":")), encoding="utf-8"
                        )
                    completed = _run_agy(
                        _agy_command(model, json_schema_path=schema_path, **({"advisor": True} if advisor else {})),
                        _prompt_input(prompt),
                        client_disconnected=self._client_disconnected,
                    )
            finally:
                _CLI_SLOTS.release()
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip()[:300] or f"agy exited with {completed.returncode}")
            try:
                content = _response_text(completed.stdout)
            except RuntimeError as exc:
                event_names = []
                for line in completed.stdout.splitlines():
                    try:
                        event_name = json.loads(line).get("event")
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if isinstance(event_name, str):
                        event_names.append(event_name)
                logger.warning(
                    "AGY advisor produced no accepted answer request_id=%s model=%r error=%s returncode=%s stdout_bytes=%s events=%s stderr=%s",
                    request_id, model, str(exc)[:300], completed.returncode, len(completed.stdout.encode("utf-8")),
                    ",".join(event_names[:12]), completed.stderr.strip()[:300],
                )
                raise
            usage = _provider_usage(completed.stdout)
            print(
                f"completion succeeded request_id={request_id} model={model!r} output_chars={len(content)}",
                file=sys.stderr,
                flush=True,
            )
            self._json(
                HTTPStatus.OK,
                {"content": content, "usage": usage},
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"detail": str(exc)})
        except subprocess.TimeoutExpired:
            logger.warning("AGY CLI timed out request_id=%s model=%r", request_id, model)
            self._json(HTTPStatus.GATEWAY_TIMEOUT, {"detail": "Antigravity timed out"})
        except ClientDisconnected:
            logger.info("Antigravity CLI cancelled after client disconnect request_id=%s model=%r", request_id, model)
        except RuntimeError as exc:
            logger.warning("AGY request failed request_id=%s model=%r error=%s", request_id, model, str(exc)[:300])
            self._json(HTTPStatus.BAD_GATEWAY, {"detail": str(exc)})
        except OSError as exc:
            logger.warning("Unable to start Antigravity CLI request_id=%s: %s", request_id, str(exc)[:300])
            self._json(HTTPStatus.BAD_GATEWAY, {"detail": "Unable to start Antigravity CLI"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 4015), Handler).serve_forever()
