"""Real HTTP checks for target and advisor failures against a Compose gateway.

Run only when explicitly authorized and pointed at staging:

    $env:SUBROUTE_RUN_LIVE_TESTS = "1"
    uv run --extra test --locked pytest -s tests/test_live_gateway_failure_matrix.py

These tests mutate the staging routing policy temporarily, make a small number of
real subscription calls, and restore the saved policy in a module-scoped finally.
They do not use mocked provider responses.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import time
import uuid
from urllib.parse import urlparse

import httpx
import pytest


pytestmark = pytest.mark.live


def _require_live_opt_in() -> None:
    if os.getenv("SUBROUTE_RUN_LIVE_TESTS") != "1":
        pytest.skip("set SUBROUTE_RUN_LIVE_TESTS=1 to make real gateway/provider calls")


class LiveGateway:
    def __init__(self, base_url: str, api_key: str | None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=170.0)

    def policy(self) -> dict:
        response = self.client.get("/api/active-model")
        response.raise_for_status()
        return response.json()

    def set_policy(
        self,
        *,
        target: str,
        advisor: str | None,
        mode: str = "force",
        reasoning_effort: str | None = None,
        advisor_reasoning_effort: str | None = None,
    ) -> None:
        current = self.policy()
        if any(current.get(key) != value for key, value in (
            ("active_model", target),
            ("mode", mode),
            ("reasoning_effort", reasoning_effort),
        )):
            response = self.client.post(
                "/api/active-model",
                json={"model": target, "mode": mode, "reasoning_effort": reasoning_effort},
            )
            response.raise_for_status()
        if any(current.get(key) != value for key, value in (
            ("advisor_model", advisor),
            ("advisor_reasoning_effort", advisor_reasoning_effort),
        )):
            response = self.client.post(
                "/api/advisor-model",
                json={"advisor_model": advisor, "reasoning_effort": advisor_reasoning_effort},
            )
            response.raise_for_status()

    def message(
        self, model: str, content: str | list[dict], *, max_tokens: int = 48,
        call_id: str | None = None,
    ) -> httpx.Response:
        headers = {"x-litellm-call-id": call_id} if call_id else None
        return self.client.post(
            "/v1/messages",
            json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]},
            headers=headers,
        )


@pytest.fixture(scope="module")
def live_gateway():
    _require_live_opt_in()
    base_url = os.getenv("SUBROUTE_LIVE_BASE_URL", "http://127.0.0.1:4005")
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("live failure matrix must target a loopback gateway")
    if parsed.port == 4000 and os.getenv("SUBROUTE_LIVE_ALLOW_PRODUCTION") != "1":
        pytest.fail("refusing to mutate production policy; use staging port 4005")

    gateway = LiveGateway(base_url, os.getenv("SUBROUTE_LIVE_API_KEY"))
    original = gateway.policy()
    try:
        yield gateway
    finally:
        gateway.set_policy(
            target=original["active_model"],
            advisor=original.get("advisor_model"),
            mode=original.get("mode", "alias"),
            reasoning_effort=original.get("reasoning_effort"),
            advisor_reasoning_effort=original.get("advisor_reasoning_effort"),
        )
        gateway.client.close()


def _assert_live_message(response: httpx.Response, label: str) -> dict:
    assert response.status_code == 200, f"{label}: HTTP {response.status_code}: {response.text[:1200]}"
    payload = response.json()
    assert payload.get("stop_reason") in {"end_turn", "max_tokens", "stop_sequence"}, payload
    text = "".join(
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    )
    assert text.strip(), payload
    usage = payload.get("usage") or {}
    assert isinstance(usage.get("input_tokens"), int) and usage["input_tokens"] >= 0, payload
    assert isinstance(usage.get("output_tokens"), int) and usage["output_tokens"] >= 0, payload
    print(f"LIVE PASS {label}: status=200 stop={payload['stop_reason']} usage={usage}")
    return payload


def _assert_live_failure(
    response: httpx.Response,
    label: str,
    expected_detail: str | None = None,
    expected_status: int | None = None,
) -> None:
    assert response.status_code >= 400, f"{label}: unexpected HTTP {response.status_code}: {response.text[:1200]}"
    if expected_status is not None:
        assert response.status_code == expected_status, (
            f"{label}: expected HTTP {expected_status}, got {response.status_code}: {response.text[:1200]}"
        )
    body = response.text[:2000]
    if expected_detail:
        assert expected_detail.lower() in body.lower(), f"{label}: expected {expected_detail!r}, got {body!r}"
    print(f"LIVE PASS {label}: status={response.status_code} error={body[:300]!r}")


def test_01_gemini_subscription_target_live_success(live_gateway: LiveGateway) -> None:
    target = os.getenv("SUBROUTE_LIVE_TARGET_MODEL", "gemini-subscription")
    effort = os.getenv("SUBROUTE_LIVE_TARGET_EFFORT", "low")
    live_gateway.set_policy(target=target, advisor=None, reasoning_effort=effort)
    response = live_gateway.message(
        "current",
        "Reply with exactly SUBROUTE-LIVE-TARGET-OK. Do not call tools.",
    )
    payload = _assert_live_message(response, f"target={target}")
    text = "".join(block.get("text", "") for block in payload.get("content", []))
    assert "SUBROUTE-LIVE-TARGET-OK" in text, text


def test_02_target_live_failure_matrix(live_gateway: LiveGateway) -> None:
    target = os.getenv("SUBROUTE_LIVE_TARGET_MODEL", "gemini-subscription")

    # Unknown public model: exercise the running gateway's real route rejection,
    # without dispatching to a provider.
    live_gateway.set_policy(target=target, advisor=None, mode="alias")
    response = live_gateway.message("subroute-live-no-such-model", "must not dispatch")
    _assert_live_failure(response, "target unknown model")

    chat_response = live_gateway.client.post(
        "/v1/chat/completions",
        json={"model": "subroute-live-no-such-model", "messages": [{"role": "user", "content": "must not dispatch"}]},
    )
    _assert_live_failure(chat_response, "chat unknown model")
    responses_response = live_gateway.client.post(
        "/v1/responses",
        json={"model": "subroute-live-no-such-model", "input": "must not dispatch"},
    )
    _assert_live_failure(responses_response, "responses unknown model")

    # A valid Anthropic image block routed to Gemini Subscription is rejected
    # by LiteLLM's deployment capability check before provider dispatch.
    live_gateway.set_policy(target=target, advisor=None, reasoning_effort="low")
    tiny_png = base64.b64encode(
        bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082")
    ).decode("ascii")
    image = [
        {"type": "text", "text": "Describe this image."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": tiny_png}},
    ]
    response = live_gateway.message("current", image)
    _assert_live_failure(response, "target unsupported image", "does not support 'image_url' content blocks")

    # The live gateway rejects the serialized bridge request at its actual
    # 1 MB boundary, before sending a body the sidecar would reject/reset.
    response = live_gateway.message("current", "x" * 1_000_100)
    _assert_live_failure(response, "target bridge request limit", "bridge request exceeds 1000000 bytes")

    response = live_gateway.message(
        "current",
        "Reply with exactly SUBROUTE-LIVE-TARGET-RECOVERED. Do not call tools.",
    )
    payload = _assert_live_message(response, f"target={target} after failure")
    text = "".join(block.get("text", "") for block in payload.get("content", []))
    assert "SUBROUTE-LIVE-TARGET-RECOVERED" in text, text


def test_03_gemini_advisor_live_failure_is_reported(live_gateway: LiveGateway) -> None:
    target = os.getenv("SUBROUTE_LIVE_BASE_TARGET", "codex-luna")
    advisor = os.getenv("SUBROUTE_LIVE_ADVISOR_MODEL", "gemini-subscription")
    live_gateway.set_policy(target=target, advisor=advisor, reasoning_effort="low")

    before = live_gateway.policy()
    invalid_advisor = live_gateway.client.post(
        "/api/advisor-model", json={"advisor_model": "subroute-live-no-such-advisor"}
    )
    _assert_live_failure(invalid_advisor, "advisor unknown model")
    assert live_gateway.policy()["advisor_model"] == before["advisor_model"]

    # This crosses the real gateway and advisor adapter but is rejected by the
    # sidecar request-size guard before either subscription provider is called.
    call_id = f"subroute-advisor-failure-{uuid.uuid4().hex}"
    response = live_gateway.message("current", "x" * 1_000_100, call_id=call_id)
    _assert_live_failure(
        response,
        f"advisor={advisor} bounded transport failure",
        "Selected advisor failed; base request was not sent",
        expected_status=413,
    )
    assert response.headers.get("x-litellm-call-id") == call_id
    error_body = response.json()
    assert error_body.get("type") == "error", error_body
    assert isinstance(error_body.get("error"), dict), error_body
    assert call_id in json.dumps(error_body), error_body
    print(f"LIVE CORRELATION gateway_request_id={call_id}")


def test_04_gemini_advisor_and_target_live_success_after_failure(live_gateway: LiveGateway) -> None:
    target = os.getenv("SUBROUTE_LIVE_BASE_TARGET", "codex-luna")
    advisor = os.getenv("SUBROUTE_LIVE_ADVISOR_MODEL", "gemini-subscription")
    live_gateway.set_policy(target=target, advisor=advisor, reasoning_effort="low")
    response = live_gateway.message(
        "current",
        "Reply with exactly SUBROUTE-LIVE-ADVISOR-BASE-OK. Do not call tools.",
    )
    payload = _assert_live_message(response, f"advisor={advisor} target={target}")
    text = "".join(block.get("text", "") for block in payload.get("content", []))
    assert "SUBROUTE-LIVE-ADVISOR-BASE-OK" in text, text


def test_05_codex_advisor_and_target_live_success(live_gateway: LiveGateway) -> None:
    target = os.getenv("SUBROUTE_LIVE_CODEX_TARGET", "codex-luna")
    advisor = os.getenv("SUBROUTE_LIVE_CODEX_ADVISOR_MODEL", "codex-sol-advisor")
    live_gateway.set_policy(target=target, advisor=advisor, reasoning_effort="low")
    response = live_gateway.message(
        "current",
        "Reply with exactly SUBROUTE-LIVE-CODEX-ADVISOR-OK. Do not call tools.",
    )
    payload = _assert_live_message(response, f"advisor={advisor} target={target}")
    text = "".join(block.get("text", "") for block in payload.get("content", []))
    assert "SUBROUTE-LIVE-CODEX-ADVISOR-OK" in text, text


def test_06_codex_reserve_alias_live_success(live_gateway: LiveGateway) -> None:
    live_gateway.set_policy(target="codex-reserve", advisor=None, reasoning_effort="high")
    response = live_gateway.message(
        "current", "Reply with exactly SUBROUTE-CODEX-RESERVE-OK. Do not call tools."
    )
    payload = _assert_live_message(response, "target=codex-reserve (GPT-5.6 Luna reserve-capable)")
    text = "".join(block.get("text", "") for block in payload.get("content", []))
    assert "SUBROUTE-CODEX-RESERVE-OK" in text, text


def test_07_openrouter_buffered_and_sse_live_success(live_gateway: LiveGateway) -> None:
    live_gateway.set_policy(target="openrouter", advisor=None, reasoning_effort=None)
    buffered = live_gateway.client.post(
        "/v1/chat/completions",
        json={"model": "current", "max_tokens": 256, "messages": [{
            "role": "user", "content": "Reply with exactly SUBROUTE-OPENROUTER-BUFFERED-OK."
        }]},
    )
    assert buffered.status_code == 200, buffered.text[:1200]
    body = buffered.json()
    choice = body["choices"][0]
    assert "SUBROUTE-OPENROUTER-BUFFERED-OK" in (choice["message"].get("content") or "")
    assert choice.get("finish_reason") == "stop"
    assert (body.get("usage") or {}).get("total_tokens", 0) > 0
    print(f"LIVE PASS target=openrouter buffered: status=200 usage={body['usage']}")

    request = {
        "model": "current", "stream": True,
        "stream_options": {"include_usage": True}, "max_tokens": 256,
        "messages": [{"role": "user", "content": "Reply with exactly SUBROUTE-OPENROUTER-SSE-OK."}],
    }
    content = ""
    usage = None
    finish_reason = None
    done = False
    with live_gateway.client.stream("POST", "/v1/chat/completions", json=request) as response:
        assert response.status_code == 200, response.read().decode()[:1200]
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            event_data = line[6:]
            if event_data == "[DONE]":
                done = True
                continue
            event = json.loads(event_data)
            usage = event.get("usage", usage)
            for item in event.get("choices", []):
                content += ((item.get("delta") or {}).get("content") or "")
                finish_reason = item.get("finish_reason", finish_reason)
    assert "SUBROUTE-OPENROUTER-SSE-OK" in content, content
    assert finish_reason == "stop" and done
    assert usage and usage.get("total_tokens", 0) > 0
    assert usage.get("cost", 0) > 0
    print(f"LIVE PASS target=openrouter SSE: finish={finish_reason} usage={usage} done={done}")


def test_08_client_disconnect_cancels_gemini_target_and_recovers(live_gateway: LiveGateway) -> None:
    target = os.getenv("SUBROUTE_LIVE_TARGET_MODEL", "gemini-subscription")
    live_gateway.set_policy(target=target, advisor=None, reasoning_effort="low")

    call_id = f"subroute-client-disconnect-{uuid.uuid4().hex}"
    request_body = json.dumps(
        {
            "model": "current",
            "max_tokens": 4096,
            "stream": True,
            "messages": [{
                "role": "user",
                "content": (
                    "Write a very detailed 3500-word expedition journal with scene-by-scene "
                    "descriptions, dialogue, and observations. Do not summarize or stop early."
                ),
            }],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    parsed = urlparse(os.getenv("SUBROUTE_LIVE_BASE_URL", "http://127.0.0.1:4005"))
    authorization = live_gateway.client.headers.get("authorization")
    auth_header = f"Authorization: {authorization}\r\n" if authorization else ""
    docker_code = (
        "import json,pathlib; print(json.dumps([p.name for p in pathlib.Path('/proc').iterdir() "
        "if p.name.isdigit() and (p/'cmdline').exists() and "
        "(p/'cmdline').read_bytes().split(b'\\0')[0].split(b'/')[-1] == b'agy']))"
    )

    def cli_pids() -> set[str]:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "antigravity", "python", "-c", docker_code],
            capture_output=True,
            check=True,
            text=True,
            timeout=6,
        )
        return set(json.loads(result.stdout))

    pids_before = cli_pids()
    sock = socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 4005), timeout=5)
    try:
        sock.sendall(
            (
                "POST /v1/messages HTTP/1.1\r\n"
                f"Host: {parsed.netloc}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(request_body)}\r\n"
                f"x-litellm-call-id: {call_id}\r\n"
                f"{auth_header}"
                "Connection: keep-alive\r\n\r\n"
            ).encode("ascii")
            + request_body
        )
        deadline = time.monotonic() + 12
        new_pids: set[str] = set()
        while time.monotonic() < deadline:
            new_pids = cli_pids() - pids_before
            if new_pids:
                break
            time.sleep(0.15)
        assert new_pids, "the public gateway request never started an Antigravity CLI process"
        # Close only after observing its exact CLI PID, while its long buffered
        # response is still in progress.
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    deadline = time.monotonic() + 5
    remaining_pids = new_pids
    while time.monotonic() < deadline:
        remaining_pids = cli_pids() & new_pids
        if not remaining_pids:
            break
        time.sleep(0.15)
    assert not remaining_pids, f"Antigravity CLI survived public client disconnect: {sorted(remaining_pids)}"

    recovered = live_gateway.message(
        "current", "Reply with exactly SUBROUTE-DISCONNECT-RECOVERY-OK.", max_tokens=64
    )
    payload = _assert_live_message(recovered, f"target={target} after client disconnect")
    text = "".join(block.get("text", "") for block in payload.get("content", []))
    assert "SUBROUTE-DISCONNECT-RECOVERY-OK" in text, text
    print(
        f"LIVE PASS client disconnect call_id={call_id}; CLI PID(s) {sorted(new_pids)} exited; "
        "subsequent target completed"
    )


def test_09_all_remaining_subscription_aliases_live_success(live_gateway: LiveGateway) -> None:
    for target in (
        "gemini-subscription-3.7-flash",
        "gemini-subscription-3.6-flash",
        "gemini-subscription-pro",
        "codex-subscription",
        "codex-astra",
        "codex-terra",
    ):
        effort = "low"
        live_gateway.set_policy(target=target, advisor=None, reasoning_effort=effort)
        response = live_gateway.message(
            "current",
            f"Reply with exactly SUBROUTE-{target.upper()}-OK. Do not call tools.",
            max_tokens=48,
        )
        payload = _assert_live_message(response, f"target={target}")
        text = "".join(block.get("text", "") for block in payload.get("content", []))
        assert f"SUBROUTE-{target.upper()}-OK" in text, text

    for advisor in ("codex-gpt-6.1-sol-advisor", "codex-astra-advisor", "codex-luna-advisor"):
        live_gateway.set_policy(
            target="codex-luna",
            advisor=advisor,
            reasoning_effort="low",
        )
        response = live_gateway.message(
            "current",
            f"Reply with exactly SUBROUTE-{advisor.upper()}-OK. Do not call tools.",
            max_tokens=48,
        )
        payload = _assert_live_message(response, f"advisor={advisor} target=codex-luna")
        text = "".join(block.get("text", "") for block in payload.get("content", []))
        assert f"SUBROUTE-{advisor.upper()}-OK" in text, text


def test_10_unconfigured_capabilities_return_client_errors(live_gateway: LiveGateway) -> None:
    cases = (
        (
            "skills",
            live_gateway.client.get(
                "/v1/skills",
                params={"beta": "true", "custom_llm_provider": "codex-subscription"},
            ),
        ),
        (
            "rerank",
            live_gateway.client.post(
                "/v1/rerank",
                json={"model": "codex-subscription", "query": "x", "documents": ["x"]},
            ),
        ),
        (
            "embeddings",
            live_gateway.client.post(
                "/v1/embeddings", json={"model": "codex-subscription", "input": "x"}
            ),
        ),
    )

    for operation, response in cases:
        assert response.status_code == 400, (
            f"{operation}: expected HTTP 400, got {response.status_code}: {response.text[:1200]}"
        )
        error = response.json().get("error", {})
        assert error.get("type") == "invalid_request_error", response.text
        assert error.get("code") == "unsupported_operation", response.text
        print(f"LIVE PASS unsupported {operation}: status=400 code=unsupported_operation")
