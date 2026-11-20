from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app

import subroute.plugins.dynamic_router  # Registers the selector routes.


def test_control_serves_selector_and_assets_without_provider_status_endpoints():
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.get("/control")
    assert response.status_code == 200
    assert "Save model" in response.text
    assert "Refresh subscription models" in response.text
    assert client.get("/control/styles.css").status_code == 200
    assert client.get("/control/app.js").status_code == 200
    assert client.get("/api/source-status").status_code == 404
    assert client.get("/api/provider-usage").status_code == 404
