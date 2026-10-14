import asyncio
import json

import httpx
import pytest

from subroute.plugins import advisor_classifier


@pytest.mark.parametrize("done_reason,content,expected_error", [
    ("stop", '{"level":2}', None),
    ("length", '{"level":2}', "did not finish normally"),
    ("stop", '{"level":4}', "invalid level"),
])
def test_local_classifier_validates_terminal_json(monkeypatch, done_reason, content, expected_error):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "done": True, "done_reason": done_reason,
            "message": {"role": "assistant", "content": content},
        })

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(advisor_classifier.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=transport, **kwargs,
    ))
    monkeypatch.setenv("ADVISOR_CLASSIFIER_MODEL", "qwen3.5:4b")
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")

    call = advisor_classifier.LocalComplexityClassifier().classify("Fix the route race", True)
    if expected_error:
        with pytest.raises(ValueError, match=expected_error):
            asyncio.run(call)
    else:
        assert asyncio.run(call) == (2, "qwen3.5:4b")
    assert len(captured) == 1
    assert captured[0]["messages"][1]["content"].endswith("Fix the route race")
    assert captured[0]["think"] is False
    assert captured[0]["stream"] is False
