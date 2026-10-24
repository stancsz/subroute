from fastapi.testclient import TestClient
from litellm.proxy.proxy_server import app
import subroute.plugins.dynamic_router  # registers the presentation route
from html.parser import HTMLParser


def test_shared_control_desk_serves_assets_and_sanitized_sources():
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.get("/control")
    assert response.status_code == 200
    assert "Subroute by Badlands" in response.text
    assert "Refresh usage" in response.text
    assert client.get("/control/styles.css").status_code == 200
    script = client.get("/control/app.js")
    assert script.status_code == 200
    assert "window.subroute" in script.text
    assert "DeepSeek Harness" in script.text
    status = client.get("/api/source-status")
    assert status.status_code == 200
    assert {source["name"] for source in status.json()["sources"]} >= {"OpenAI Subscription", "Anthropic API", "DeepSeek API", "Zhipu GLM API", "Alibaba Qwen API"}
    assert not {"openai", "gemini"}.intersection(source["id"] for source in status.json()["sources"])
    assert all("credentials" not in source for source in status.json()["sources"])


def test_advisor_panel_describes_optional_expert_consultation_without_frequency_controls():
    client = TestClient(app, client=("127.0.0.1", 50000))
    markup = client.get("/control").text

    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.by_id = {}

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if "id" in attributes:
                self.by_id[attributes["id"]] = (tag, attributes)

    parsed = Controls()
    parsed.feed(markup)
    assert parsed.by_id["route-advisor"][0] == "select"
    assert 'aria-label="Advisor model"' in markup
    assert "optional Advisor tool" in markup
    assert "expert service 4040" in markup
    assert "4040" in markup
    assert "25.6K estimated input tokens" in markup
    assert "output limits depend on the selected provider" in markup
    for removed in ["route-advisor-effort", "route-advisor-frequency", "route-advisor-analytics-window"]:
        assert removed not in parsed.by_id
    assert "/api/advisor-analytics" not in markup
