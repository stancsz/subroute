"""Exercise LiteLLM's actual proxy HTTP routes with the Codex upstream stubbed."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from importlib.metadata import version
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient
import litellm
from litellm import Router
from litellm.proxy import proxy_server
from litellm.proxy._types import UserAPIKeyAuth

from subroute.handlers.codex_subscription import codex_subscription_handler
from subroute.plugins.codex_credentials import (
    CODEX_API_BASE,
    codex_credential_refresher,
)


class FakeStream:
    def __init__(self, chunks=None):
        self.chunks = iter(chunks if chunks is not None else [
            {
                "id": "chatcmpl-http-fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "gpt-6-luna",
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": "fixture answer"},
                    "finish_reason": None,
                }],
            },
            {
                "id": "chatcmpl-http-fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "gpt-6-luna",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": "chatcmpl-http-fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "gpt-6-luna",
                "choices": [],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        ])

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def aclose(self):
        pass


@contextmanager
def _configured_proxy(
    *,
    api_base="https://fixture.invalid/backend-api/codex",
    credential_callback=False,
    incomplete_stream=False,
    omit_provider_usage=False,
    dynamic_routing_plugin=None,
):
    original_acompletion = litellm.acompletion
    upstream_models = []
    upstream_credentials = []
    upstream_stream_options = []
    upstream_request_options = []

    async def fake_upstream_or_dispatch(**kwargs):
        if kwargs.get("model", "").startswith("openai/responses/"):
            assert kwargs["stream"] is True
            upstream_models.append(kwargs["model"])
            upstream_credentials.append((kwargs.get("api_key"), kwargs.get("extra_headers", {})))
            upstream_stream_options.append(kwargs.get("stream_options"))
            upstream_request_options.append({
                "store": kwargs.get("store"),
                "stream_options": kwargs.get("stream_options"),
            })
            if incomplete_stream:
                return FakeStream([{
                    "id": "chatcmpl-incomplete",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "gpt-6-luna",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "partial answer"},
                        "finish_reason": None,
                    }],
                }])
            if omit_provider_usage:
                return FakeStream([
                    {
                        "id": "chatcmpl-no-usage",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "gpt-6-luna",
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "fixture answer"}, "finish_reason": None}],
                    },
                    {
                        "id": "chatcmpl-no-usage",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "gpt-6-luna",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ])
            return FakeStream()
        return await original_acompletion(**kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(litellm, "custom_provider_map", [{
            "provider": "codex-subscription",
            "custom_handler": codex_subscription_handler,
        }]))
        stack.enter_context(patch.object(litellm, "acompletion", fake_upstream_or_dispatch))
        callbacks = []
        if credential_callback:
            callbacks.append(codex_credential_refresher)
        if dynamic_routing_plugin is not None:
            callbacks.append(dynamic_routing_plugin)
        if callbacks:
            stack.enter_context(patch.object(litellm, "callbacks", callbacks))
        litellm.utils.custom_llm_setup()
        model_list = [{
            "model_name": "codex-luna",
            "litellm_params": {
                "model": "codex-subscription/gpt-6-luna",
                "api_base": api_base,
                "api_key": "fixture-key",
                "extra_headers": {"ChatGPT-Account-ID": "fixture-account"},
                "store": False,
                "allowed_openai_params": ["reasoning_effort"],
            },
        }]
        if dynamic_routing_plugin is not None:
            model_list = [{
                "model_name": model_name,
                "litellm_params": {
                    "model": f"codex-subscription/{target}",
                    "api_base": api_base,
                    "api_key": "fixture-key",
                    "extra_headers": {"ChatGPT-Account-ID": "fixture-account"},
                    "store": False,
                    "allowed_openai_params": ["reasoning_effort"],
                },
            } for model_name, target in (
                ("current", "gpt-6-luna"),
                ("codex-luna", "gpt-6-luna"),
                ("codex-gpt-6.1-sol", "gpt-6.1-sol"),
            )]
        router = Router(
            model_list=model_list,
            num_retries=0,
            fallbacks=[],
        )
        stack.enter_context(patch.object(proxy_server, "llm_router", router))
        stack.enter_context(patch.dict(proxy_server.app.dependency_overrides, {
            proxy_server.user_api_key_auth: lambda: UserAPIKeyAuth(api_key="fixture-key"),
        }))
        yield TestClient(
            proxy_server.app,
            client=("127.0.0.1", 50000),
        ), upstream_models, upstream_credentials, upstream_stream_options, upstream_request_options


def _request(protocol: str, stream: bool):
    if protocol == "chat":
        return "/v1/chat/completions", {
            "model": "codex-luna",
            "messages": [{"role": "user", "content": "say hello"}],
            "stream": stream,
        }
    if protocol == "responses":
        return "/v1/responses", {
            "model": "codex-luna",
            "input": "say hello",
            "stream": stream,
            "store": False,
        }
    return "/v1/messages", {
        "model": "codex-luna",
        "messages": [{"role": "user", "content": "say hello"}],
        "max_tokens": 32,
        "stream": stream,
    }


def verify_litellm_proxy_http_routes():
    with _configured_proxy() as (client, upstream_models, _, upstream_stream_options, upstream_request_options):
        headers = {"anthropic-version": "2023-06-01"}

        for protocol in ("chat", "responses", "messages"):
            for stream in (False, True):
                path, payload = _request(protocol, stream)
                response = client.post(path, json=payload, headers=headers)
                assert response.status_code == 200, (protocol, stream, response.text)
                body = response.text
                assert "fixture answer" in body
                assert "codex-luna" in body
                if stream:
                    assert "[DONE]" in body or "response.completed" in body or "message_stop" in body
                else:
                    parsed = response.json()
                    assert parsed.get("model") == "codex-luna"

        assert len(upstream_models) == 6
        assert set(upstream_models) == {"openai/responses/gpt-6-luna"}
        assert upstream_stream_options[4:6] == [
            {"include_usage": True},
            {"include_usage": True},
        ]
        assert all(options["store"] is False for options in upstream_request_options), upstream_request_options


def test_litellm_proxy_http_routes_serve_codex_protocols():
    verify_litellm_proxy_http_routes()


def verify_codex_auth_file_reload_through_proxy(auth_file: Path):
    upstream_credentials = []
    with patch.dict("os.environ", {"CODEX_AUTH_FILE": str(auth_file)}):
        with _configured_proxy(api_base=CODEX_API_BASE, credential_callback=True) as (
            client,
        _,
        upstream_credentials,
        _,
        _,
        ):
            headers = {"anthropic-version": "2023-06-01"}
            for access_token, account_id in (
                ("refreshed-token-1", "account-1"),
                ("refreshed-token-2", "account-2"),
            ):
                auth_file.write_text(json.dumps({
                    "tokens": {"access_token": access_token, "account_id": account_id},
                }), encoding="utf-8")
                response = client.post("/v1/chat/completions", json={
                    "model": "codex-luna",
                    "messages": [{"role": "user", "content": "say hello"}],
                }, headers=headers)
                assert response.status_code == 200, response.text

    assert [api_key for api_key, _ in upstream_credentials] == [
        "refreshed-token-1",
        "refreshed-token-2",
    ]
    assert [headers["ChatGPT-Account-ID"] for _, headers in upstream_credentials] == [
        "account-1",
        "account-2",
    ]


def test_proxy_dispatch_reloads_codex_auth_file_before_each_request(tmp_path):
    verify_codex_auth_file_reload_through_proxy(tmp_path / "auth.json")


def verify_current_model_uses_control_plane_policy_through_proxy(temp_dir: Path):
    import subroute.plugins.dynamic_router as dynamic_routing
    from subroute.plugins.dynamic_router import DynamicRoutingPlugin, RoutingControlPlane

    config_path = temp_dir / "routing.yaml"
    config_path.write_text("""model_list:
  - model_name: current
    litellm_params: {model: openai/virtual}
  - model_name: codex-luna
    litellm_params: {model: codex-subscription/gpt-6-luna}
  - model_name: codex-gpt-6.1-sol
    litellm_params: {model: codex-subscription/gpt-6.1-sol}
  - model_name: gemini-subscription
    model_info: {selectable: false, advisor_selectable: true}
    litellm_params: {model: antigravity/gemini}
""", encoding="utf-8")
    with patch.dict("os.environ", {
        "ACTIVE_MODEL": "codex-luna",
        "ADVISOR_MODEL": "gemini-subscription",
    }):
        control = RoutingControlPlane(config_path, temp_dir / "routing-state.json")
    plugin = DynamicRoutingPlugin(control)
    routed_metadata = []
    original_hook = plugin.async_pre_call_hook

    async def recording_hook(*args, **kwargs):
        data = args[2] if len(args) > 2 else kwargs["data"]
        result = await original_hook(*args, **kwargs)
        routed_metadata.append(dict(data.get("metadata") or {}))
        return result

    plugin.async_pre_call_hook = recording_hook
    with patch.object(dynamic_routing, "control_plane", control):
        with _configured_proxy(dynamic_routing_plugin=plugin) as (
            client,
            upstream_models,
            _,
            _,
            _,
        ):
            update_response = client.post("/api/active-model", json={
                "model": "codex-gpt-6.1-sol",
                "mode": "alias",
            })
            assert update_response.status_code == 200, update_response.text
            saved_state = update_response.json()
            response = client.post("/v1/chat/completions", json={
                "model": "current",
                "messages": [{"role": "user", "content": "say hello"}],
            })

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "fixture answer"
    assert saved_state["active_model"] == "codex-gpt-6.1-sol"
    assert saved_state["mode"] == "alias"
    assert upstream_models == ["openai/responses/gpt-6.1-sol"]
    assert routed_metadata[0]["routing"] == {
        "requested_model": "current",
        "resolved_model": "codex-gpt-6.1-sol",
        "mode": "alias",
        "policy_version": saved_state["policy_version"],
    }
    assert routed_metadata[0]["gateway_policy"]["active_model"] == "codex-gpt-6.1-sol"
    assert routed_metadata[0]["gateway_policy"]["policy_version"] == saved_state["policy_version"]


def test_current_model_dispatch_uses_saved_control_plane_policy(tmp_path):
    verify_current_model_uses_control_plane_policy_through_proxy(tmp_path)


def verify_incomplete_stream_through_proxy():
    with _configured_proxy(incomplete_stream=True) as (client, upstream_models, _, _, _):
        for protocol in ("chat", "responses", "messages"):
            path, payload = _request(protocol, stream=True)
            response = client.post(path, json=payload, headers={
                "anthropic-version": "2023-06-01",
            })
            assert response.status_code == 200, (protocol, response.text)
            assert "partial answer" in response.text, (protocol, response.text)
            assert "Codex stream ended without a terminal finish reason" in response.text, (
                protocol,
                response.text,
            )
            success_terminal = {
                "chat": "[DONE]",
                "responses": "response.completed",
                "messages": "message_stop",
            }[protocol]
            assert success_terminal not in response.text, (protocol, response.text)
    assert upstream_models == ["openai/responses/gpt-6-luna"] * 3


def test_messages_without_provider_usage_fails_with_a_bounded_gateway_error():
    with _configured_proxy(omit_provider_usage=True) as (client, _, _, _, _):
        response = client.post("/v1/messages", json={
            "model": "codex-luna",
            "messages": [{"role": "user", "content": "say hello"}],
            "max_tokens": 32,
        }, headers={"anthropic-version": "2023-06-01"})

    assert response.status_code == 502, response.text
    assert "omitted usage required by the Anthropic Messages bridge" in response.text


def test_streamed_messages_do_not_emit_success_when_provider_usage_is_missing():
    with _configured_proxy(omit_provider_usage=True) as (client, _, _, _, _):
        response = client.post("/v1/messages", json={
            "model": "codex-luna",
            "messages": [{"role": "user", "content": "say hello"}],
            "max_tokens": 32,
            "stream": True,
        }, headers={"anthropic-version": "2023-06-01"})

    assert "omitted usage required by the Anthropic Messages bridge" in response.text
    assert "message_stop" not in response.text


def test_proxy_stream_does_not_report_success_without_terminal_upstream_chunk():
    verify_incomplete_stream_through_proxy()


if __name__ == "__main__":
    verify_litellm_proxy_http_routes()
    with tempfile.TemporaryDirectory() as temp_dir:
        verify_codex_auth_file_reload_through_proxy(Path(temp_dir) / "auth.json")
        verify_current_model_uses_control_plane_policy_through_proxy(Path(temp_dir))
    verify_incomplete_stream_through_proxy()
    print(json.dumps({
        "litellm": version("litellm"),
        "http_cases": 6,
        "credential_reload_cases": 2,
        "dynamic_routing_cases": 1,
        "incomplete_stream_protocols": 3,
        "result": "PASS",
        "provider_calls": 0,
    }))
