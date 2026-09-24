from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app
import subroute.plugins.dynamic_router  # registers the presentation route


def test_shared_control_desk_serves_assets_and_sanitized_sources():
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.get("/control")
    assert response.status_code == 200
    assert "YOUR LOCAL CONTROL DESK" in response.text
    assert "Refresh usage" in response.text
    assert client.get("/control/styles.css").status_code == 200
    script = client.get("/control/app.js")
    assert script.status_code == 200
    assert "window.subroute" in script.text
    assert "DeepSeek Harness" in script.text
    status = client.get("/api/source-status")
    assert status.status_code == 200
    assert {source["name"] for source in status.json()["sources"]} >= {"OpenAI Subscription", "Anthropic API", "DeepSeek API", "Zhipu GLM API", "Alibaba Qwen API"}
    assert all("credentials" not in source for source in status.json()["sources"])
