"""Exercise LiteLLM's actual fallback traversal without contacting providers."""

import asyncio
from pathlib import Path

import litellm
import pytest
import yaml

from subroute.plugins.dynamic_router import DynamicRoutingPlugin, RoutingControlPlane


CONFIG = Path(__file__).parents[1] / "config" / "litellm.yaml"


@pytest.mark.parametrize("mode", ["alias", "force", "off"])
def test_auto_preserves_client_effort_and_bypasses_saved_target(tmp_path, mode):
    control = RoutingControlPlane(CONFIG, tmp_path / "policy.json")
    control.update("codex-luna", mode, reasoning_effort="high")
    plugin = DynamicRoutingPlugin(control)
    data = {"model": "auto", "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "low"}
    result = asyncio.run(plugin.async_pre_call_hook({}, None, data, "acompletion"))
    assert result["model"] == "auto"
    assert result["reasoning_effort"] == "low"
    assert result["metadata"]["gateway_reasoning_effort"] is None


@pytest.mark.parametrize("failed_count", [0, 1, 2, 3])
def test_litellm_auto_fallback_order_and_exhaustion(monkeypatch, failed_count):
    settings = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    groups = ["auto", "auto-gemini-subscription", "auto-codex-luna"]
    calls = []

    async def completion(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) <= failed_count:
            raise litellm.ServiceUnavailableError(
                message="controlled provider failure", llm_provider="openai", model=kwargs["model"],
            )
        return litellm.ModelResponse(model=kwargs["model"], choices=[{
            "index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop",
        }])

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(litellm, "callbacks", [])

    async def check():
        router = litellm.Router(model_list=[{
            "model_name": group,
            "litellm_params": {"model": f"openai/{group}", "api_key": "fixture-only"},
        } for group in groups], **settings["router_settings"])
        if failed_count == 3:
            with pytest.raises(litellm.ServiceUnavailableError):
                await router.acompletion(model="auto", messages=[{"role": "user", "content": "hello"}])
        else:
            response = await router.acompletion(model="auto", messages=[{"role": "user", "content": "hello"}])
            assert response.choices[0].message.content == "OK"

    asyncio.run(check())
    assert calls == [f"openai/{group}" for group in groups[:min(failed_count + 1, 3)]]


def test_fixed_route_failure_never_uses_auto_fallback(monkeypatch):
    settings = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    calls = []

    async def fail(*args, **kwargs):
        calls.append(kwargs["model"])
        raise litellm.ServiceUnavailableError(message="controlled failure", llm_provider="openai", model=kwargs["model"])

    monkeypatch.setattr(litellm, "acompletion", fail)
    monkeypatch.setattr(litellm, "callbacks", [])

    async def check():
        router = litellm.Router(model_list=[{
            "model_name": "codex-luna",
            "litellm_params": {"model": "openai/fixed", "api_key": "fixture-only"},
        }], **settings["router_settings"])
        with pytest.raises(litellm.ServiceUnavailableError):
            await router.acompletion(model="codex-luna", messages=[{"role": "user", "content": "hello"}])

    asyncio.run(check())
    assert calls == ["openai/fixed"]
