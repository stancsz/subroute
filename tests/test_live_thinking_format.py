"""Opt-in formatting smoke test on staging, with saved routing restored."""

import json
import os
import time
from urllib.parse import urlparse

import httpx
import pytest

from test_live_gateway_failure_matrix import LiveGateway, _require_live_opt_in
from test_thinking_format import collect_messages


pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def formatting_gateway():
    _require_live_opt_in()
    base = os.getenv("SUBROUTE_LIVE_BASE_URL", "http://127.0.0.1:4005")
    address = urlparse(base)
    assert address.hostname in {"127.0.0.1", "localhost", "::1"} and address.port == 4005
    gateway = LiveGateway(base, os.getenv("SUBROUTE_LIVE_API_KEY"))
    deadline = time.monotonic() + 30
    while True:
        try:
            if gateway.client.get("/health/readiness", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            gateway.client.close()
            pytest.fail("staging did not become ready within 30 seconds")
        time.sleep(0.25)
    original = gateway.policy()
    try:
        yield gateway
    finally:
        gateway.set_policy(target=original["active_model"], mode=original["mode"],
                           advisor=original["advisor_model"], reasoning_effort=original.get("reasoning_effort"),
                           advisor_reasoning_effort=original.get("advisor_reasoning_effort"))
        gateway.client.close()


def verify_stream(response):
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    blocks = collect_messages(events)
    text = "".join(block.get("text", "") for block in blocks if block["type"] == "text")
    # Assert display formatting, not nondeterministic prompt-following content.
    assert "\n" in text, repr(text)
    assert "<think>" not in text and "</think>" not in text
    return blocks


PAYLOAD = {"model": "current", "max_tokens": 2048, "stream": True,
           "thinking": {"type": "enabled", "budget_tokens": 1024},
           "messages": [{"role": "user", "content": "Write exactly two lines: FORMAT_LINE_A then FORMAT_LINE_B. Put a real line break between the lines, with no code fence or explanation."}]}


@pytest.mark.parametrize("model", ["minimax", "openrouter", "mimo-v2.6-pro", "codex-luna", "gemini-subscription"])
def test_configured_provider_formatting(formatting_gateway, model):
    formatting_gateway.set_policy(target=model, mode="force", advisor=None)
    response = formatting_gateway.client.post("/v1/messages", json=PAYLOAD)
    blocks = verify_stream(response)
    print(f"{model}: valid blocks={[block['type'] for block in blocks]}")


def test_local_claude_code_endpoint(formatting_gateway):
    base = os.getenv("SUBROUTE_LOCAL_BASE_URL")
    if not base:
        pytest.skip("set SUBROUTE_LOCAL_BASE_URL to check the local forwarder")
    address = urlparse(base)
    assert address.hostname in {"127.0.0.1", "localhost", "::1"} and address.port == 11435
    formatting_gateway.set_policy(target="minimax", mode="force", advisor=None)
    token = os.getenv("SUBROUTE_LOCAL_API_KEY")
    headers = {"x-api-key": token} if token else {}
    with httpx.Client(base_url=base, headers=headers, timeout=120) as client:
        blocks = verify_stream(client.post("/v1/messages", json=PAYLOAD))
    print(f"local 11435: valid blocks={[block['type'] for block in blocks]}")


def test_local_parallel_tools_and_followup(formatting_gateway):
    base = os.getenv("SUBROUTE_LOCAL_BASE_URL")
    if not base:
        pytest.skip("set SUBROUTE_LOCAL_BASE_URL to check the local forwarder")
    address = urlparse(base)
    assert address.hostname in {"127.0.0.1", "localhost", "::1"} and address.port == 11435
    formatting_gateway.set_policy(target="codex-luna", mode="force", advisor=None)
    token = os.getenv("SUBROUTE_LOCAL_API_KEY")
    headers = {"x-api-key": token} if token else {}
    messages = [{"role": "user", "content": "Call lookup twice, once with q=1 and once with q=2. Do not answer until both tool results return."}]
    payload = {"model": "current", "max_tokens": 2048, "stream": True, "messages": messages,
               "tools": [{"name": "lookup", "description": "Look up a number.", "input_schema": {"type": "object", "properties": {"q": {"type": "integer"}}, "required": ["q"], "additionalProperties": False}}],
               "tool_choice": {"type": "any"}}
    with httpx.Client(base_url=base, headers=headers, timeout=120) as client:
        response = client.post("/v1/messages", json=payload)
        assert response.status_code == 200, response.text
        blocks = collect_messages([json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")])
        tools = [block for block in blocks if block["type"] == "tool_use"]
        assert sorted(tool["input"]["q"] for tool in tools) == [1, 2]
        assert len({tool["id"] for tool in tools}) == 2
        messages.extend([{"role": "assistant", "content": blocks}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool["id"], "content": f"lookup {tool['input']['q']} completed"} for tool in tools]}])
        payload["tool_choice"] = {"type": "auto"}
        response = client.post("/v1/messages", json=payload)
        assert response.status_code == 200, response.text
        final = collect_messages([json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")])
        assert any(block["type"] == "text" and block["text"].strip() for block in final)
        assert all(block["type"] != "tool_use" for block in final)
    print("local 11435: two distinct tool inputs and replayed followup completed")
