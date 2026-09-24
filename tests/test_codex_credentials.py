import json
import asyncio

from subroute.plugins.codex_credentials import (
    CODEX_API_BASE,
    codex_credential_refresher,
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


def test_translation_only_applies_at_codex_deployment(monkeypatch):
    from subroute.plugins import codex_credentials
    monkeypatch.setattr(codex_credentials, "read_codex_credentials", lambda: ("fixture", "account"))
    request = {"api_base": CODEX_API_BASE, "max_output_tokens": 50, "user": "client-user",
               "messages": [{"role": "system", "content": "constraints"}]}
    updated = asyncio.run(codex_credential_refresher.async_pre_call_deployment_hook(request, None))
    assert updated["messages"] == [{"role": "developer", "content": "constraints"}]
    assert request["messages"][0]["role"] == "system"
    # LiteLLM's Anthropic-to-Responses adapter creates this field from
    # Claude Code's required max_tokens value, but the subscription backend
    # rejects it. The Codex boundary must remove it after translation.
    assert "max_output_tokens" not in updated
    assert request["max_output_tokens"] == 50
    assert "user" not in updated
    assert request["user"] == "client-user"
    request.update(api_base="https://api.openai.com/v1", model="openai/responses/test")
    assert asyncio.run(codex_credential_refresher.async_pre_call_deployment_hook(request, None)) is None
