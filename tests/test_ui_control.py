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
    gemini = next(source for source in status.json()["sources"] if source["id"] == "gemini-subscription")
    assert "Gemini 3.8 Flash" in gemini["models"]
    assert "Gemini 4 Argon" in gemini["models"]
    assert all(name not in gemini["models"] for name in ("Gemini 3.7 Flash", "Gemini 3.6 Flash", "Gemini 3.1 Pro"))


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
    assert "saved expert is the default" in markup
    assert "set its Advisor tool’s model to another configured expert" in markup
    assert "one expert is used per consultation" in markup
    for removed in ["route-advisor-effort", "route-advisor-frequency", "route-advisor-analytics-window"]:
        assert removed not in parsed.by_id
    assert "/api/advisor-analytics" not in markup


def test_request_policy_labels_match_routing_modes():
    markup = TestClient(app, client=("127.0.0.1", 50000)).get("/control").text
    assert 'value="alias">Selected target for aliases and unknown models' in markup
    assert 'value="force">Force selected target for text requests' in markup
    assert 'value="off">Let LiteLLM resolve the requested model' in markup
    assert 'Alias mode sends `default`, unknown and ambiguous model names to the selected target' in markup
    assert '`auto` uses LiteLLM’s fallback group' in markup
    assert 'Force also sends `auto` to the selected target' in markup
    assert "bypasses the saved target" in markup
    assert "unknown and ambiguous model names" in markup
    assert "protocol-compatibility routes may use a compatible route" in markup
