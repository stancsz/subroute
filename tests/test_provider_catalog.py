from pathlib import Path
from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app
import pytest
import subroute.plugins.dynamic_router
import subroute.provider_catalog as catalog
from subroute.plugins.dynamic_router import _load_choices


def test_requested_models_are_present_as_configured_choices():
    choices = {choice.model_id: choice for choice in _load_choices(Path("config/litellm.yaml"))}
    assert choices["codex-gpt-6.1-terra"].provider == "OpenAI"
    assert all(choice.access == "Subscription" for choice in choices.values() if choice.source_id == "openai-subscription")
    for model in ("gemini-subscription", "gemini-subscription-argon", "antigravity-claude-opus-5-5", "antigravity-claude-sonnet-5-5"):
        assert choices[model].provider == "Antigravity"


def test_catalog_refresh_reports_both_providers_and_isolates_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "_snapshot", None)
    plane = subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    async def openai():
        return ["gpt-6.1-terra"]
    async def gemini():
        raise RuntimeError("do not expose secrets")
    monkeypatch.setattr(catalog, "_openai_models", openai)
    monkeypatch.setattr(catalog, "_gemini_models", gemini)
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.post("/api/model-catalog/refresh")
    assert response.status_code == 200
    assert response.json()["openai"]["models"] == ["gpt-6.1-terra"]
    assert response.json()["gemini"]["state"] == "unavailable"
    assert "catalog-openai/gpt-6.1-terra" in plane.allowed_models
    assert "secrets" not in response.text
    assert TestClient(app, client=("203.0.113.1", 50000)).post("/api/model-catalog/refresh").status_code == 403


def test_failed_refresh_keeps_previous_provider_inventory(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "_snapshot", None)
    plane = subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    async def ready():
        return ["model-a"]
    async def failed():
        raise RuntimeError("unavailable")
    monkeypatch.setattr(catalog, "_openai_models", ready)
    monkeypatch.setattr(catalog, "_gemini_models", ready)
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.post("/api/model-catalog/refresh").json()["gemini"]["state"] == "ready"
    monkeypatch.setattr(catalog, "_gemini_models", failed)
    stale = client.post("/api/model-catalog/refresh").json()["gemini"]
    assert stale["state"] == "stale"
    assert stale["models"] == ["model-a"]


def test_failed_catalog_write_does_not_report_uninstalled_models_as_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "_snapshot", None)
    plane = subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    async def ready(): return ["model-a"]
    monkeypatch.setattr(catalog, "_openai_models", ready)
    monkeypatch.setattr(catalog, "_gemini_models", ready)
    def failed_write(*args): raise OSError("fixture")
    monkeypatch.setattr(catalog, "write_catalog", failed_write)
    client = TestClient(app, client=("127.0.0.1", 50000))
    for _ in range(2):
        result = client.post("/api/model-catalog/refresh").json()
        assert result["openai"]["state"] == "unavailable"
        assert result["openai"]["models"] == []
        assert "catalog-openai/model-a" not in plane.allowed_models


def test_successful_refresh_updates_visible_models_without_breaking_existing_clients(monkeypatch, tmp_path):
    plane = subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    plane.add_catalog_models("openai", ["old-model"])
    plane.add_catalog_models("openai", ["new-model"])
    client = TestClient(app, client=("127.0.0.1", 50000))
    models = {item["model_id"] for item in client.get("/api/routing-options").json()["models"]}
    assert "catalog-openai/new-model" in models
    assert "catalog-openai/old-model" not in models
    assert "catalog-openai/old-model" in plane.allowed_models
    restarted = subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    assert restarted.catalog["openai_current"] == ["new-model"]
    assert "catalog-openai/old-model" in restarted.allowed_models


def test_refreshed_model_survives_restart_and_unlisted_names_are_rejected(monkeypatch, tmp_path):
    import asyncio
    from fastapi import HTTPException
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    import pytest
    path = tmp_path / "active.json"
    plane = RoutingControlPlane(Path("config/litellm.yaml"), path)
    before = plane.snapshot()
    plane.add_catalog_models("openai", ["new-model"])
    assert plane.snapshot() == before
    plane.update("catalog-openai/new-model", "alias")
    restarted = RoutingControlPlane(Path("config/litellm.yaml"), path)
    assert restarted.snapshot().active_model == "catalog-openai/new-model"
    assert "catalog-openai/new-model" in restarted.allowed_models
    with pytest.raises(HTTPException) as error:
        asyncio.run(DynamicRoutingPlugin(restarted).async_pre_call_hook(None, None, {"model": "catalog-openai/unlisted"}, "completion"))
    assert error.value.status_code == 404
    catalog.catalog_path(path).write_text("invalid", encoding="utf-8")
    recovered = RoutingControlPlane(Path("config/litellm.yaml"), path)
    assert recovered.snapshot().active_model == "catalog-openai/new-model"
    assert "catalog-openai/new-model" in recovered.allowed_models


def test_catalog_patterns_preserve_provider_model_identity(monkeypatch):
    import litellm
    import yaml
    from litellm import Router
    from subroute.handlers.codex_subscription import codex_subscription_handler
    from subroute.handlers.antigravity import antigravity_handler
    monkeypatch.setattr(litellm, "custom_provider_map", [
        {"provider": "codex-subscription", "custom_handler": codex_subscription_handler},
        {"provider": "antigravity", "custom_handler": antigravity_handler},
    ])
    data = yaml.safe_load(Path("config/litellm.yaml").read_text())
    models = [entry for entry in data["model_list"] if entry["model_name"] in {"catalog-openai/*", "catalog-antigravity/*"}]
    router = Router(model_list=models)
    assert router.pattern_router.route("catalog-openai/gpt-6.1-terra")[0]["litellm_params"]["model"] == "codex-subscription/gpt-6.1-terra"
    name = catalog.catalog_alias("gemini", "claude-sonnet-5-5-medium")
    assert router.pattern_router.route(name)[0]["litellm_params"]["model"] == "antigravity/catalog--" + name.partition("/")[2]


def test_dynamic_codex_private_responses_keep_selected_model(tmp_path):
    import asyncio
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    plane.add_catalog_models("openai", ["gpt-6.1-terra"])
    data = {"model": "catalog-openai/gpt-6.1-terra", "input": "hello", "tools": [{"type": "custom", "name": "x"}]}
    result = asyncio.run(DynamicRoutingPlugin(plane).async_pre_call_hook(None, None, data, "responses"))
    assert result["model"] == "catalog-openai/gpt-6.1-terra"
    assert result["disable_fallbacks"] is True


def test_dynamic_codex_private_responses_reach_correct_upstream(monkeypatch, tmp_path):
    import json
    import httpx
    from test_codex_proxy_http import _configured_proxy
    from test_usage_accounting import MemoryStore
    from subroute import usage_accounting
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    plane.add_catalog_models("openai", ["new-codex-model"])
    store = MemoryStore()
    monkeypatch.setattr(usage_accounting.usage_accounting, "store", store)
    usage_accounting.install_stream_accounting()
    captured = []
    async def fake_send(self, request, **kwargs):
        captured.append(json.loads(request.content))
        assert captured[-1].get("stream") is True
        terminal = {"id": "resp-fixture", "object": "response", "created_at": 1,
                    "model": "new-codex-model", "output": [], "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}
        return httpx.Response(200, request=request, content=("data: " + json.dumps({"type": "response.completed", "response": terminal}) + "\n\n").encode(), headers={"content-type": "text/event-stream"})
    monkeypatch.setattr("subroute.plugins.codex_credentials.read_codex_credentials", lambda: ("fixture-token", "fixture-account"))
    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    models = [{"model_name": "catalog-openai/*", "litellm_params": {
        "model": "codex-subscription/*", "api_base": "https://chatgpt.com/backend-api/codex",
        "api_key": "fixture", "store": False, "allowed_openai_params": ["reasoning_effort"],
    }}]
    items = [{"type": "additional_tools", "role": "developer", "tools": []}, {"role": "user", "content": "hello"}]
    callbacks = [usage_accounting.usage_accounting, DynamicRoutingPlugin(plane)]
    with _configured_proxy(credential_callback=True, additional_callbacks=callbacks, provider_model_list=models) as (client, *_):
        response = client.post("/v1/responses", json={"model": "catalog-openai/new-codex-model", "input": items, "stream": True, "store": False})
        conflict = client.post("/v1/responses", json={"model": "catalog-openai/new-codex-model", "input": items, "stream": True, "store": False, "extra_body": {"model": "other-model"}})
    assert response.status_code == 200, response.text
    assert "response.completed" in response.text
    events = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:") and line[5:].strip() not in {"", "[DONE]"}]
    terminal = next(event for event in events if event.get("type") == "response.completed")
    assert terminal["response"]["model"] == "catalog-openai/new-codex-model"
    assert captured[0]["model"] == "new-codex-model"
    assert captured[0]["input"] == items
    assert conflict.status_code == 400, conflict.text
    assert len(captured) == 1
    target_rows = [row for row in store.rows.values() if row["model"].endswith("new-codex-model")]
    assert target_rows
    assert any(row["outcome"] == "complete" and row["facts"].get("input_tokens") == 1 for row in target_rows)
    assert all("provider_cost_micros" not in row["facts"] for row in target_rows)
    assert all("subroute-codex-responses-stream" not in row["model"] for row in target_rows)


def test_dynamic_codex_custom_tool_reaches_native_responses(monkeypatch, tmp_path):
    import json
    import httpx
    from test_codex_proxy_http import _configured_proxy
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    plane.add_catalog_models("openai", ["new-codex-model"])
    captured = []
    async def fake_send(self, request, **kwargs):
        captured.append(json.loads(request.content))
        output = [{"type": "custom_tool_call", "id": "ctc_fixture", "call_id": "call_fixture",
                   "name": "fixture_tool", "input": "fixture argument"}] if len(captured) == 1 else []
        terminal = {"id": "resp-fixture", "object": "response", "created_at": 1,
                    "model": "new-codex-model", "output": output, "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}
        return httpx.Response(200, request=request, content=("data: " + json.dumps({"type": "response.completed", "response": terminal}) + "\n\n").encode(), headers={"content-type": "text/event-stream"})
    monkeypatch.setattr("subroute.plugins.codex_credentials.read_codex_credentials", lambda: ("fixture-token", "fixture-account"))
    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    models = [{"model_name": "catalog-openai/*", "litellm_params": {
        "model": "codex-subscription/*", "api_base": "https://chatgpt.com/backend-api/codex",
        "api_key": "fixture", "store": False, "allowed_openai_params": ["reasoning_effort"],
    }}]
    tools = [{"type": "custom", "name": "fixture_tool", "description": "Fixture"}]
    with _configured_proxy(credential_callback=True, dynamic_routing_plugin=DynamicRoutingPlugin(plane), provider_model_list=models) as (client, *_):
        response = client.post("/v1/responses", json={"model": "catalog-openai/new-codex-model", "input": "hello", "tools": tools, "stream": True, "store": False})
        continuation = [{"type": "custom_tool_call", "id": "ctc_fixture", "call_id": "call_fixture",
                         "name": "fixture_tool", "input": "fixture argument"},
                        {"type": "custom_tool_call_output", "call_id": "call_fixture", "output": "fixture result"}]
        followup = client.post("/v1/responses", json={"model": "catalog-openai/new-codex-model", "input": continuation, "tools": tools, "stream": True, "store": False})
    assert response.status_code == 200, response.text
    assert "response.completed" in response.text
    assert "call_fixture" in response.text and "fixture argument" in response.text
    assert followup.status_code == 200, followup.text
    assert "response.completed" in followup.text
    assert captured[0]["model"] == "new-codex-model"
    assert captured[0]["tools"] == tools
    assert captured[1]["input"] == continuation


@pytest.mark.parametrize(("event_type", "status"), [("response.incomplete", "incomplete"), ("response.failed", "failed")])
def test_dynamic_codex_non_success_stream_is_not_reported_as_completed(monkeypatch, tmp_path, event_type, status):
    import json
    import httpx
    from test_codex_proxy_http import _configured_proxy
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    plane.add_catalog_models("openai", ["new-codex-model"])
    async def fake_send(self, request, **kwargs):
        terminal = {"id": "resp-fixture", "object": "response", "created_at": 1,
                    "model": "new-codex-model", "output": [], "status": status,
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        if status == "incomplete":
            terminal["incomplete_details"] = {"reason": "max_output_tokens"}
        else:
            terminal["error"] = {"code": "server_error", "message": "fixture failure"}
        event = {"type": event_type, "response": terminal}
        return httpx.Response(200, request=request, content=("data: " + json.dumps(event) + "\n\n").encode(), headers={"content-type": "text/event-stream"})
    monkeypatch.setattr("subroute.plugins.codex_credentials.read_codex_credentials", lambda: ("fixture-token", "fixture-account"))
    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    models = [{"model_name": "catalog-openai/*", "litellm_params": {
        "model": "codex-subscription/*", "api_base": "https://chatgpt.com/backend-api/codex",
        "api_key": "fixture", "store": False, "allowed_openai_params": ["reasoning_effort"],
    }}]
    items = [{"type": "additional_tools", "role": "developer", "tools": []}, {"role": "user", "content": "hello"}]
    with _configured_proxy(credential_callback=True, dynamic_routing_plugin=DynamicRoutingPlugin(plane), provider_model_list=models) as (client, *_):
        response = client.post("/v1/responses", json={"model": "catalog-openai/new-codex-model", "input": items, "stream": True, "store": False})
    assert response.status_code == 200, response.text
    assert event_type in response.text
    assert "response.completed" not in response.text


def test_dynamic_antigravity_dispatch_uses_discovered_id(monkeypatch, tmp_path):
    import asyncio
    from subroute.handlers import antigravity
    from subroute import usage_accounting
    from subroute.plugins.dynamic_router import RoutingControlPlane
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    plane.add_catalog_models("gemini", ["claude-sonnet-5-5-medium"])
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    monkeypatch.setenv("ANTIGRAVITY_BRIDGE_URL", "http://fixture")
    captured = []
    class Response:
        status_code = 200
        def json(self):
            return {"content": "OK", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            captured.append(kwargs["json"]["model"])
            return Response()
    async def observe(_): pass
    monkeypatch.setattr(antigravity.httpx, "AsyncClient", Client)
    monkeypatch.setattr(usage_accounting, "observe_direct", observe)
    name = catalog.catalog_alias("gemini", "claude-sonnet-5-5-medium")
    content, _ = asyncio.run(antigravity.invoke_agy("catalog--" + name.partition("/")[2], "hello"))
    assert content == "OK"
    assert captured == ["claude-sonnet-5-5-medium"]


def test_dynamic_openai_route_dispatches_through_public_proxy(tmp_path):
    from test_codex_proxy_http import _configured_proxy, _request
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    plane.add_catalog_models("openai", ["new-codex-model"])
    models = [{"model_name": "catalog-openai/*", "litellm_params": {
        "model": "codex-subscription/*", "api_base": "https://fixture.invalid/backend-api/codex",
        "api_key": "fixture", "store": False, "allowed_openai_params": ["reasoning_effort"],
    }}]
    with _configured_proxy(dynamic_routing_plugin=DynamicRoutingPlugin(plane), provider_model_list=models) as (client, upstream, *_):
        for protocol in ("chat", "responses", "messages"):
            path, body = _request(protocol, True)
            body["model"] = "catalog-openai/new-codex-model"
            response = client.post(path, json=body)
            assert response.status_code == 200, response.text
            assert "fixture answer" in response.text
            assert {"chat": "[DONE]", "responses": "response.completed", "messages": "message_stop"}[protocol] in response.text
        assert upstream == ["openai/responses/new-codex-model"] * 3


def test_dynamic_catalog_does_not_invent_a_context_limit(monkeypatch, tmp_path):
    import litellm
    import yaml
    from test_codex_proxy_http import _configured_proxy, _request
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    plane.add_catalog_models("openai", ["new-codex-model"])
    data = yaml.safe_load(Path("config/litellm.yaml").read_text())
    deployment = next(item for item in data["model_list"] if item["model_name"] == "catalog-openai/*")
    deployment["litellm_params"]["api_base"] = "https://fixture.invalid/backend-api/codex"
    monkeypatch.setattr(litellm, "token_counter", lambda **kwargs: 256001)
    with _configured_proxy(dynamic_routing_plugin=DynamicRoutingPlugin(plane), provider_model_list=[deployment], enable_pre_call_checks=True) as (client, upstream, *_):
        path, body = _request("chat", False)
        body["model"] = "catalog-openai/new-codex-model"
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        assert upstream == ["openai/responses/new-codex-model"]


def test_unlisted_catalog_models_are_rejected_at_public_protocols_in_all_modes(tmp_path):
    from test_codex_proxy_http import _configured_proxy, _request
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    models = [{"model_name": "catalog-openai/*", "litellm_params": {
        "model": "codex-subscription/*", "api_base": "https://fixture.invalid/backend-api/codex",
        "api_key": "fixture", "store": False, "allowed_openai_params": ["reasoning_effort"],
    }}]
    with _configured_proxy(dynamic_routing_plugin=DynamicRoutingPlugin(plane), provider_model_list=models) as (client, upstream, *_):
        for mode in ("alias", "force", "off"):
            plane.update("codex-luna", mode)
            for protocol in ("chat", "responses", "messages"):
                path, body = _request(protocol, False)
                body["model"] = "catalog-openai/not-authorized"
                response = client.post(path, json=body)
                assert response.status_code == 404, (mode, protocol, response.text)
        assert not upstream


def test_dynamic_antigravity_route_through_public_messages(monkeypatch, tmp_path):
    import json
    from litellm.types.utils import Usage
    from test_codex_proxy_http import _configured_proxy, _request
    from subroute.handlers import antigravity
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    plane = RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    slug = "claude-sonnet-5-5-medium"
    plane.add_catalog_models("gemini", [slug])
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    captured = []
    async def fake_agy(model, prompt, **kwargs):
        captured.append(model)
        return json.dumps({"kind": "text", "text": "fixture answer"}), Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    monkeypatch.setattr(antigravity, "invoke_agy", fake_agy)
    models = [{"model_name": "catalog-antigravity/*", "litellm_params": {
        "model": "antigravity/catalog--*", "allowed_openai_params": ["reasoning_effort"],
    }}]
    providers = [{"provider": "antigravity", "custom_handler": antigravity.antigravity_handler}]
    with _configured_proxy(dynamic_routing_plugin=DynamicRoutingPlugin(plane), provider_model_list=models, additional_custom_providers=providers) as (client, *_):
        path, body = _request("messages", True)
        body["model"] = catalog.catalog_alias("gemini", slug)
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        assert "fixture answer" in response.text and "message_stop" in response.text
        assert captured == ["catalog--" + slug.encode("ascii").hex()]



def test_catalog_menu_uses_provider_names_and_preserves_saved_routes(monkeypatch, tmp_path):
    plane = subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    plane.add_catalog_models("openai", ["gpt-6.1-terra", "new-model"])
    plane.add_catalog_models("gemini", ["gemini-3.8-flash-high", "claude-opus-5-5-medium"])
    client = TestClient(app, client=("127.0.0.1", 50000))
    def models():
        return {entry["model_id"]: entry["display_name"] for entry in client.get("/api/routing-options").json()["models"]}
    result = models()
    assert result["catalog-openai/gpt-6.1-terra"] == "gpt-6.1-terra"
    assert "codex-gpt-6.1-terra" not in result
    assert result["catalog-openai/new-model"] == "new-model"
    assert "gemini-subscription" not in result
    assert catalog.catalog_alias("gemini", "claude-opus-5-5-medium") in result
    plane.update("codex-gpt-6.1-terra", "alias")
    result = models()
    assert result["codex-gpt-6.1-terra"] == "gpt-6.1-terra"
    assert "catalog-openai/gpt-6.1-terra" not in result
    assert "catalog-openai/gpt-6.1-terra" in plane.allowed_models
    plane.update("gemini-subscription", "alias")
    result = models()
    assert result["gemini-subscription"] == "gemini-3.8-flash-high"
    assert catalog.catalog_alias("gemini", "gemini-3.8-flash-high") not in result
    plane.add_catalog_models("gemini", ["gemini-3.8-flash-high", "gemini-3.8-flash-medium"])
    plane.update("gemini-subscription", "alias", reasoning_effort="medium")
    result = models()
    assert result["gemini-subscription"] == "gemini-3.8-flash-medium"
    assert catalog.catalog_alias("gemini", "gemini-3.8-flash-medium") not in result


def test_static_provider_menu_uses_upstream_model_names(monkeypatch, tmp_path):
    plane = subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    client = TestClient(app, client=("127.0.0.1", 50000))
    models = {entry["model_id"]: entry["display_name"] for entry in client.get("/api/routing-options").json()["models"]}
    assert models["minimax-flash-direct"] == "MiniMax-M3.1-Flash"
    assert models["mimo-v2.6-pro"] == "mimo-v2.6-pro"
    assert models["desktop"] == "qwen2.5-coder"
    assert models["antigravity-claude-opus-5-5"] == "claude-opus-5-5-medium"


def test_refreshed_known_codex_model_keeps_supported_reasoning_efforts(tmp_path):
    plane = subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    plane.add_catalog_models("openai", ["gpt-6.1-terra", "unknown-model"])
    choices = {choice.model_id: choice for choice in plane.choices}
    assert choices["catalog-openai/gpt-6.1-terra"].reasoning_efforts == choices["codex-gpt-6.1-terra"].reasoning_efforts
    assert choices["catalog-openai/gpt-6.1-terra"].reasoning_efforts
    assert choices["catalog-openai/gpt-6.1-terra"].access == "Subscription"
    assert not choices["catalog-openai/unknown-model"].reasoning_efforts
    plane.update("catalog-openai/gpt-6.1-terra", "alias", reasoning_effort="high")
    assert plane.snapshot().reasoning_effort == "high"


def test_advisor_choices_use_actual_expert_model_names(monkeypatch, tmp_path):
    plane = subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json")
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    client = TestClient(app, client=("127.0.0.1", 50000))
    choices = {x["model_id"]: x["display_name"] for x in client.get("/api/routing-options").json()["advisor_models"]}
    assert choices["codex-gpt-6.1-sol-advisor"] == "gpt-6.1-sol"
    assert choices["codex-gpt-6.1-terra-advisor"] == "gpt-6.1-terra"
    assert choices["agy-opus-5-5-medium-advisor"] == "claude-opus-5-5-medium"
    assert choices["agy-sonnet-5-5-medium-advisor"] == "claude-sonnet-5-5-medium"
    assert choices["gemini-subscription"] == "gemini-3.8-flash-low"
    assert choices["agy-gemini-3.8-flash-medium-advisor"] == "gemini-3.8-flash-medium"
    plane.update_advisor("agy-sonnet-5-5-medium-advisor")
    assert subroute.plugins.dynamic_router.RoutingControlPlane(Path("config/litellm.yaml"), tmp_path / "active.json").snapshot().advisor_model == "agy-sonnet-5-5-medium-advisor"


def test_restart_with_failed_refresh_retains_current_and_saved_catalog_routes(monkeypatch, tmp_path):
    import asyncio
    from subroute.plugins.dynamic_router import RoutingControlPlane, DynamicRoutingPlugin
    path = tmp_path / "active.json"
    plane = RoutingControlPlane(Path("config/litellm.yaml"), path)
    plane.add_catalog_models("openai", ["previous-model"], {"previous-model": ["low"]})
    plane.update("catalog-openai/previous-model", "alias", reasoning_effort="low")
    plane.add_catalog_models("openai", ["current-model"], {"current-model": ["high"]})
    restarted = RoutingControlPlane(Path("config/litellm.yaml"), path)
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", restarted)
    monkeypatch.setattr(catalog, "_snapshot", None)
    async def failed(): raise RuntimeError("offline")
    monkeypatch.setattr(catalog, "_openai_models", failed)
    monkeypatch.setattr(catalog, "_gemini_models", failed)
    result = asyncio.run(catalog.refresh_catalog())
    assert result["openai"]["state"] == "stale"
    assert result["openai"]["models"] == ["current-model"]
    choices = {item["model_id"] for item in TestClient(app, client=("127.0.0.1", 50000)).get("/api/routing-options").json()["models"]}
    assert {"catalog-openai/current-model", "catalog-openai/previous-model"} <= choices
    for name in ("current-model", "previous-model"):
        data = {"model": "catalog-openai/" + name, "messages": [{"role": "user", "content": "hi"}]}
        routed = asyncio.run(DynamicRoutingPlugin(restarted).async_pre_call_hook(None, None, data, "acompletion"))
        assert routed["model"] == "catalog-openai/" + name


def test_refresh_replaces_current_model_efforts_and_retains_removed_history(tmp_path):
    from subroute.plugins.dynamic_router import RoutingControlPlane
    path = tmp_path / "active.json"
    plane = RoutingControlPlane(Path("config/litellm.yaml"), path)
    plane.add_catalog_models("openai", ["current-model", "old-model"], {"current-model": ["low", "high"], "old-model": ["high"]})
    plane.add_catalog_models("openai", ["current-model"], {"current-model": ["medium"]})
    restarted = RoutingControlPlane(Path("config/litellm.yaml"), path)
    choices = {item.model_id: item for item in restarted.choices}
    assert choices["catalog-openai/current-model"].reasoning_efforts == ("medium",)
    assert choices["catalog-openai/old-model"].reasoning_efforts == ("high",)


def test_refresh_rejects_effort_change_that_would_hide_saved_policy(monkeypatch, tmp_path):
    import asyncio
    from subroute.plugins.dynamic_router import RoutingControlPlane
    path = tmp_path / "active.json"
    plane = RoutingControlPlane(Path("config/litellm.yaml"), path)
    plane.add_catalog_models("openai", ["gpt-new"], {"gpt-new": ["low", "high"]})
    plane.update("catalog-openai/gpt-new", "force", reasoning_effort="high")
    before = plane.snapshot()
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    monkeypatch.setattr(catalog, "_snapshot", None)
    async def changed(): return catalog.ModelInventory(["gpt-new"], {"gpt-new": ["low"]})
    async def other(): return ["gemini-4-argon-medium"]
    monkeypatch.setattr(catalog, "_openai_models", changed)
    monkeypatch.setattr(catalog, "_gemini_models", other)
    result = asyncio.run(catalog.refresh_catalog())
    assert result["openai"]["state"] == "stale"
    assert "saved reasoning effort" in result["openai"]["detail"].lower()
    assert plane.snapshot() == before
    assert next(c for c in plane.choices if c.model_id == before.active_model).reasoning_efforts == ("low", "high")
    assert RoutingControlPlane(Path("config/litellm.yaml"), path).snapshot() == before
    plane.update(before.active_model, before.mode, reasoning_effort=None)
    result = asyncio.run(catalog.refresh_catalog())
    assert result["openai"]["state"] == "ready"
    assert next(c for c in plane.choices if c.model_id == before.active_model).reasoning_efforts == ("low",)


def test_static_codex_effort_conflict_is_reported_and_policy_preserved(monkeypatch, tmp_path):
    import asyncio
    from subroute.plugins.dynamic_router import RoutingControlPlane
    path = tmp_path / "active.json"
    plane = RoutingControlPlane(Path("config/litellm.yaml"), path)
    plane.update("codex-gpt-6.1-terra", "force", reasoning_effort="high")
    before = plane.snapshot()
    monkeypatch.setattr(subroute.plugins.dynamic_router, "control_plane", plane)
    monkeypatch.setattr(catalog, "_snapshot", None)
    async def initial(): return catalog.ModelInventory(["gpt-6.1-terra"], {"gpt-6.1-terra": ["low", "high"]})
    async def changed(): return catalog.ModelInventory(["gpt-6.1-terra"], {"gpt-6.1-terra": ["low"]})
    async def other(): return ["gemini-4-argon-medium"]
    monkeypatch.setattr(catalog, "_openai_models", initial)
    monkeypatch.setattr(catalog, "_gemini_models", other)
    assert asyncio.run(catalog.refresh_catalog())["openai"]["state"] == "ready"
    monkeypatch.setattr(catalog, "_openai_models", changed)
    item = asyncio.run(catalog.refresh_catalog())["openai"]
    assert item["state"] == "stale"
    assert item["models"] == ["gpt-6.1-terra"]
    assert "saved reasoning effort" in item["detail"].lower()
    assert plane.snapshot() == before
    assert RoutingControlPlane(Path("config/litellm.yaml"), path).snapshot() == before
