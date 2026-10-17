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


def test_advisor_frequency_control_preserves_accessible_native_and_legacy_controls():
    client = TestClient(app, client=("127.0.0.1", 50000))
    markup = client.get("/control").text

    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.by_id = {}
            self.labels = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if "id" in attributes:
                self.by_id[attributes["id"]] = (tag, attributes)
            if tag == "label":
                self.labels.append(attributes)

    parsed = Controls()
    parsed.feed(markup)
    tag, slider = parsed.by_id["route-advisor-frequency"]
    assert tag == "input"
    assert {key: slider[key] for key in ["type", "min", "max", "step"]} == {
        "type": "range", "min": "0", "max": "2", "step": "1",
    }
    assert any(label.get("for") == slider["id"] for label in parsed.labels)
    assert slider["aria-valuetext"] == "Balanced, about 50 percent"
    for description in slider["aria-describedby"].split():
        assert description in parsed.by_id
    assert parsed.by_id["route-advisor-advanced"][0] == "details"
    assert parsed.by_id["route-advisor-sensitivity"][0] == "select"
    assert 'value="ranked">Learned ranking' in markup
    assert "eligible API requests" in markup
    assert "higher estimated review priority" in markup
    assert markup.index('id="route-advisor-trigger"') < markup.index('id="route-advisor-classifier"') < markup.index('id="route-advisor-frequency"') < markup.index('id="route-advisor-frequency-stats"') < markup.index('id="route-advisor-advanced"')
