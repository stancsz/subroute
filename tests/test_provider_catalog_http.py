import asyncio

from subroute import provider_catalog


def test_provider_inventory_filters_hidden_models_and_preserves_public_names(monkeypatch):
    async def fake_get(url, **kwargs):
        assert url.startswith("https://chatgpt.com/backend-api/codex/models?client_version=")
        assert kwargs["timeout"] <= 8
        return {"models": [
            {"slug": "gpt-6.1-terra", "visibility": "list", "supported_in_api": True},
            {"slug": "hidden", "visibility": "hide", "supported_in_api": True},
            {"slug": "not-api", "visibility": "list", "supported_in_api": False},
        ]}
    monkeypatch.setattr(provider_catalog, "read_codex_credentials", lambda: ("token", "account"))
    monkeypatch.setattr(provider_catalog, "_get_json", fake_get)
    assert asyncio.run(provider_catalog._openai_models()) == ["gpt-6.1-terra"]
