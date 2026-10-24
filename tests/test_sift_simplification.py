"""Normal dispatch preserves caller content even when Sift is unavailable."""

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from test_dynamic_routing import make_control_plane
from test_codex_proxy_http import verify_litellm_proxy_http_routes
from subroute.plugins.advisor_plugin import ADVISOR_TOOL_TYPE, AdvisorPlugin
from subroute.plugins.dynamic_router import DynamicRoutingPlugin


@pytest.fixture
def unavailable_sift(monkeypatch):
    import sift.core

    def unavailable(*args, **kwargs):
        pytest.fail("Normal target dispatch attempted Sift compaction")

    monkeypatch.setattr(sift.core, "compact_messages", unavailable)


@pytest.mark.parametrize("call_type,content", [
    ("acompletion", {
        "messages": [{"role": "system", "content": "Keep my instructions"},
                     {"role": "user", "content": " history" * 2000}],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object"}}}],
        "max_completion_tokens": 1234,
    }),
    ("aresponses", {
        "instructions": "Keep my instructions",
        "input": [{"role": "user", "content": " history" * 2000}],
        "tools": [{"type": "function", "name": "lookup",
                   "parameters": {"type": "object"}}],
        "max_output_tokens": 1234,
    }),
    ("aanthropic_messages", {
        "system": [{"type": "text", "text": "Keep my instructions"}],
        "messages": [{"role": "user", "content": " history" * 2000}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "max_tokens": 1234,
    }),
])
def test_normal_routing_preserves_content_and_limits_without_sift(
    tmp_path, unavailable_sift, call_type, content,
):
    routing = DynamicRoutingPlugin(make_control_plane(tmp_path))
    data = {"model": "default", **deepcopy(content)}

    async def dispatch():
        assert await routing.async_pre_call_hook(None, None, data, call_type) is data
        await AdvisorPlugin().async_pre_call_hook(None, None, data, call_type)

    asyncio.run(dispatch())
    assert data["model"] == "minimax"
    assert {key: data[key] for key in content if key != "tools"} == {
        key: value for key, value in content.items() if key != "tools"
    }
    caller_tools = [tool for tool in data["tools"] if tool.get("type") != ADVISOR_TOOL_TYPE]
    assert caller_tools == content["tools"]
    advertised = [tool for tool in data["tools"] if tool.get("type") == ADVISOR_TOOL_TYPE]
    assert len(advertised) <= (1 if call_type == "aanthropic_messages" else 0)
    for bucket in ("metadata", "litellm_metadata"):
        assert "gateway_advisor" not in data.get(bucket, {})
        assert "sift" not in data.get(bucket, {})


def test_configured_normal_callbacks_do_not_register_sift():
    settings = yaml.safe_load(
        (Path(__file__).parents[1] / "config/litellm.yaml").read_text(encoding="utf-8")
    )
    assert all("sift" not in str(callback).lower()
               for callback in settings["litellm_settings"]["callbacks"])


def test_buffered_and_streamed_protocol_dispatch_without_sift(unavailable_sift):
    # Real LiteLLM HTTP entry points and transport, with only upstream stubbed.
    # The helper also asserts exactly six provider calls and terminal responses.
    verify_litellm_proxy_http_routes()
