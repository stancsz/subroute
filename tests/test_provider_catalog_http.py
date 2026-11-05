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


def test_openai_reasoning_efforts_are_taken_from_inventory_and_persisted(monkeypatch, tmp_path):
    from pathlib import Path
    from subroute.plugins.dynamic_router import RoutingControlPlane
    async def fake_get(url, **kwargs):
        return {"models": [{"slug": "gpt-new", "visibility": "list", "supported_in_api": True,
                            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "unknown"}]}]}
    monkeypatch.setattr(provider_catalog, "read_codex_credentials", lambda: ("token", "account"))
    monkeypatch.setattr(provider_catalog, "_get_json", fake_get)
    inventory = asyncio.run(provider_catalog._openai_models())
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    plane.add_catalog_models("openai", list(inventory), inventory.efforts)
    assert next(c for c in plane.choices if c.model_id == "catalog-openai/gpt-new").reasoning_efforts == ("low", "high")
    plane.update("catalog-openai/gpt-new", "alias", reasoning_effort="high")
    restored = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    assert restored.snapshot().reasoning_effort == "high"
    assert next(c for c in restored.choices if c.model_id == "catalog-openai/gpt-new").reasoning_efforts == ("low", "high")
