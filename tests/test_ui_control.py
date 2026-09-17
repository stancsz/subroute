from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app
import unified_llm_gateway.plugins.dynamic_router  # registers the presentation route


def test_source_desk_is_read_only_and_lists_configured_sources():
    response = TestClient(app, client=("127.0.0.1", 50000)).get("/control")
    assert response.status_code == 200
    assert "NO ROUTING WRITES" in response.text
    assert "Provider quota data unavailable" in response.text
    assert "Credential required" in response.text
    assert "DeepSeek Harness" in response.text
    status = TestClient(app, client=("127.0.0.1", 50000)).get("/api/source-status")
    assert status.status_code == 200
    assert {source["name"] for source in status.json()["sources"]} >= {"OpenAI Subscription", "Anthropic API", "DeepSeek API", "Zhipu GLM API", "Alibaba Qwen API"}
