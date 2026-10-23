import json
import asyncio

import pytest

from subroute.plugins.codex_credentials import (
    CODEX_API_BASE,
    CODEX_RESPONSES_LITE_HEADER,
    codex_credential_refresher,
    read_codex_credentials,
)


def test_codex_credentials_are_reloaded_for_each_dispatch(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))
    request = {
        "api_base": CODEX_API_BASE,
        "api_key": "stale",
        "extra_headers": {"User-Agent": "test", "ChatGPT-Account-ID": "stale"},
    }

    auth_file.write_text(
        json.dumps({"tokens": {"access_token": "first", "account_id": "account-1"}}),
        encoding="utf-8",
    )
    first = asyncio.run(
        codex_credential_refresher.async_pre_call_deployment_hook(request, None)
    )
    auth_file.write_text(
        json.dumps({"tokens": {"access_token": "second", "account_id": "account-2"}}),
        encoding="utf-8",
    )
    second = asyncio.run(
        codex_credential_refresher.async_pre_call_deployment_hook(request, None)
    )

    assert first["api_key"] == "first"
    assert first["extra_headers"]["ChatGPT-Account-ID"] == "account-1"
    assert second["api_key"] == "second"
    assert second["extra_headers"]["ChatGPT-Account-ID"] == "account-2"
    assert request["api_key"] == "stale"


def test_non_codex_deployments_are_unchanged():
    result = asyncio.run(
        codex_credential_refresher.async_pre_call_deployment_hook(
            {"api_base": "https://api.openai.com/v1"}, None
        )
    )
    assert result is None


@pytest.mark.parametrize(
    "payload",
    [
        {"tokens": {"access_token": "", "account_id": "fixture-account"}},
        {"tokens": {"access_token": "fixture-token", "account_id": " "}},
        {"tokens": {"access_token": None, "account_id": "fixture-account"}},
        {"tokens": {"access_token": "fixture-token", "account_id": 123}},
        {"tokens": None},
        [],
    ],
)
def test_invalid_codex_credentials_fail_closed(tmp_path, monkeypatch, payload):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))

    with pytest.raises(RuntimeError, match="Unable to load Codex credentials"):
        read_codex_credentials()


def test_translation_only_applies_at_codex_deployment(monkeypatch):
    from subroute.plugins import codex_credentials
    monkeypatch.setattr(codex_credentials, "read_codex_credentials", lambda: ("fixture", "account"))
    request = {"api_base": CODEX_API_BASE, "max_output_tokens": 50, "user": "client-user",
               "messages": [{"role": "system", "content": "constraints"}]}
    updated = asyncio.run(codex_credential_refresher.async_pre_call_deployment_hook(request, None))
    assert updated["messages"] == [{"role": "developer", "content": "constraints"}]
    assert request["messages"][0]["role"] == "system"
    # Unsupported provider parameters must remain visible as provider errors;
    # the credential hook must not silently remove output limits or identity.
    assert updated["max_output_tokens"] == 50
    assert request["max_output_tokens"] == 50
    assert updated["user"] == "client-user"
    assert request["user"] == "client-user"
    request.update(api_base="https://api.openai.com/v1", model="openai/responses/test")
    assert asyncio.run(codex_credential_refresher.async_pre_call_deployment_hook(request, None)) is None


def test_codex_responses_string_input_is_wrapped_as_one_user_message(monkeypatch):
    from subroute.plugins.codex_credentials import codex_credential_refresher

    monkeypatch.setattr(
        "subroute.plugins.codex_credentials.read_codex_credentials",
        lambda: ("fixture-token", "fixture-account"),
    )
    request = {
        "api_base": "https://chatgpt.com/backend-api/codex",
        "input": "Keep the original prompt intact.",
        "store": False,
    }

    updated = asyncio.run(codex_credential_refresher.async_pre_call_deployment_hook(
        request, "aresponses"
    ))

    assert updated is not None
    assert updated["input"] == [
        {"role": "user", "content": "Keep the original prompt intact."}
    ]
    assert updated["store"] is False
    assert request["input"] == "Keep the original prompt intact."


def test_codex_responses_input_items_keep_content_and_tool_fields(monkeypatch):
    from subroute.plugins.codex_credentials import codex_credential_refresher

    monkeypatch.setattr(
        "subroute.plugins.codex_credentials.read_codex_credentials",
        lambda: ("fixture-token", "fixture-account"),
    )
    items = [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Inspect this"},
            {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
        ]},
        {"type": "function_call_output", "call_id": "call-1", "output": "done"},
    ]
    request = {
        "api_base": "https://chatgpt.com/backend-api/codex",
        "input": items,
        "store": False,
    }

    updated = asyncio.run(codex_credential_refresher.async_pre_call_deployment_hook(
        request, "aresponses"
    ))

    assert updated is not None
    assert updated["input"] == items
    assert updated["store"] is False
    assert request["input"] is items


def test_codex_additional_tools_enables_responses_lite_without_forwarding_client_headers(monkeypatch):
    monkeypatch.setattr(
        "subroute.plugins.codex_credentials.read_codex_credentials",
        lambda: ("fixture-token", "fixture-account"),
    )
    items = [
        {"type": "additional_tools", "role": "developer", "tools": [{"type": "web_search"}]},
        {"type": "message", "role": "user", "content": "Search"},
    ]
    client_headers = {"User-Agent": "test", "x-openai-internal-unrelated": "do-not-forward"}
    request = {
        "api_base": CODEX_API_BASE,
        "model": "codex-subscription/gpt-6-sol",
        "service_tier": "fast",
        "input": items,
        "extra_headers": client_headers,
    }

    updated = asyncio.run(
        codex_credential_refresher.async_pre_call_deployment_hook(request, "aresponses")
    )

    assert updated["extra_headers"] == {
        **client_headers,
        "ChatGPT-Account-ID": "fixture-account",
        CODEX_RESPONSES_LITE_HEADER: "true",
    }
    assert updated["input"] == items
    assert request["input"] is items
    assert request["extra_headers"] == client_headers
    assert updated["reasoning"] == {"context": "all_turns"}
    assert updated["custom_llm_provider"] == "openai"
    assert updated["model"] == "gpt-6-sol"
    assert request["model"] == "codex-subscription/gpt-6-sol"
    assert updated["service_tier"] == "priority"
    assert request["service_tier"] == "fast"
    assert updated["parallel_tool_calls"] is False
    assert "reasoning" not in request
    assert "parallel_tool_calls" not in request


def test_codex_lite_defaults_preserve_explicit_reasoning_and_tool_policy(monkeypatch):
    monkeypatch.setattr(
        "subroute.plugins.codex_credentials.read_codex_credentials",
        lambda: ("fixture-token", "fixture-account"),
    )
    reasoning = {"effort": "low", "context": "caller-choice"}
    request = {"api_base": CODEX_API_BASE,
               "input": [{"type": "additional_tools", "role": "developer", "tools": []}],
               "reasoning": reasoning, "parallel_tool_calls": True}
    updated = asyncio.run(codex_credential_refresher.async_pre_call_deployment_hook(request, "aresponses"))
    assert updated["reasoning"] == reasoning
    assert updated["parallel_tool_calls"] is True
    assert request["reasoning"] is reasoning


@pytest.mark.parametrize(
    "call_type,input_value",
    [
        ("aresponses", [{"type": "message", "role": "user", "content": "x"}]),
        ("acompletion", [{"type": "additional_tools", "tools": []}]),
        ("aresponses", "plain prompt"),
    ],
)
def test_codex_responses_lite_header_is_limited_to_additional_tools(monkeypatch, call_type, input_value):
    monkeypatch.setattr(
        "subroute.plugins.codex_credentials.read_codex_credentials",
        lambda: ("fixture-token", "fixture-account"),
    )
    updated = asyncio.run(
        codex_credential_refresher.async_pre_call_deployment_hook(
            {"api_base": CODEX_API_BASE, "input": input_value}, call_type
        )
    )

    assert CODEX_RESPONSES_LITE_HEADER not in updated["extra_headers"]
