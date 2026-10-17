import asyncio
import hashlib
import json
import threading
from pathlib import Path

import pytest

from subroute.advisor_ranker import AdvisorRankerUnavailable, AdvisorRequestRanker
import subroute.advisor_ranker as ranker_module


def artifact(tmp_path):
    for name in ("model_optimized.onnx", "tokenizer.json"):
        (tmp_path / name).write_bytes(b"fixture")
    digest = hashlib.sha256(b"fixture").hexdigest()
    data = {"schema_version": 1, "algorithm": "multilingual-minilm-multinomial-logistic",
        "class_labels": [0, 1, 2], "coefficients": [[0.] * 384 for _ in range(3)], "intercepts": [0., 0., 0.],
        "embedding": {"dimension": 384, "pooling": "attention-masked token mean then L2 normalize",
            "truncation": {"max_length": 128, "stride": 0, "strategy": "longest_first", "direction": "right"},
            "padding": {"length": None, "pad_to_multiple_of": None, "pad_id": 0, "pad_token": "<pad>", "pad_type_id": 0, "direction": "right"},
            "add_special_tokens": True, "input_dtype": "int64",
            "input_names": ["input_ids", "attention_mask", "token_type_ids"], "output_name": "last_hidden_state",
            "weights_sha256": digest, "tokenizer_sha256": digest}}
    path = tmp_path / "head.json"
    path.write_text(json.dumps(data))
    return path, data


def test_missing_model_is_explicit(tmp_path):
    path, _ = artifact(tmp_path)
    (tmp_path / "model_optimized.onnx").unlink()
    with pytest.raises(AdvisorRankerUnavailable, match="load failed") as caught:
        AdvisorRequestRanker(path, tmp_path).score("hello")
    assert isinstance(caught.value.__cause__, FileNotFoundError)


def test_weights_integrity_checked_before_optional_imports(tmp_path):
    path, _ = artifact(tmp_path)
    (tmp_path / "model_optimized.onnx").write_bytes(b"tampered")
    with pytest.raises(AdvisorRankerUnavailable) as caught:
        AdvisorRequestRanker(path, tmp_path).score("hello")
    assert "integrity mismatch" in str(caught.value.__cause__)


@pytest.mark.parametrize("field,value", [("class_labels", [0, 2, 1]), ("intercepts", [0., float("nan"), 0.]), ("coefficients", [[0.], [0.], [0.]])])
def test_invalid_head_rejected(tmp_path, field, value):
    path, data = artifact(tmp_path)
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(AdvisorRankerUnavailable) as caught:
        AdvisorRequestRanker(path, tmp_path).score("hello")
    assert "artifact contract" in str(caught.value.__cause__)


def test_attention_mask_normalization_and_finite_score(monkeypatch):
    np = pytest.importorskip("numpy")
    ranker = AdvisorRequestRanker()
    ranker._loaded = True
    ranker._np = np
    ranker._coefficients = np.zeros((3, 384))
    ranker._coefficients[2, 0] = 3
    ranker._intercepts = np.zeros(3)

    class Tokenizer:
        def encode(self, prompt, **kwargs):
            assert len(prompt) == 6000
            return type("Encoded", (), {"ids": [1, 0], "attention_mask": [1, 0], "type_ids": [0, 0]})()

    class Session:
        def run(self, outputs, inputs):
            hidden = np.zeros((1, 2, 384), dtype=np.float32)
            hidden[0, 0, 0] = 7
            hidden[0, 1, 1] = 10000  # Padding must have zero influence.
            return [hidden]

    ranker._tokenizer, ranker._session = Tokenizer(), Session()
    score = ranker.score("x" * 7000)
    expected = (.5 + np.exp(3)) / (2 + np.exp(3))
    assert score == pytest.approx(expected)
    assert isinstance(score, float)


def test_cancelled_request_does_not_admit_second_inference():
    async def scenario():
        ranker = AdvisorRequestRanker()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def score(prompt):
            calls.append(prompt)
            entered.set()
            assert release.wait(3)
            return .75
        ranker.score = score
        first = asyncio.create_task(ranker.rank("first"))
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(ranker.rank("second"))
        await asyncio.sleep(.03)
        assert calls == ["first"]
        release.set()
        assert await asyncio.wait_for(second, 2) == .75
        assert calls == ["first", "second"]
    asyncio.run(scenario())


def test_inference_timeout_keeps_capacity_and_bounds_queued_requests(monkeypatch):
    monkeypatch.setattr(ranker_module, "CAPACITY_WAIT_SECONDS", .03)
    monkeypatch.setattr(ranker_module, "INFERENCE_WAIT_SECONDS", .03)

    async def scenario():
        ranker = AdvisorRequestRanker()
        release, finished = threading.Event(), threading.Event()
        calls = []
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))

        def score(prompt):
            calls.append(prompt)
            if prompt == "first":
                assert release.wait(2)
                finished.set()
                raise ValueError("worker failed after original request timed out")
            return .5

        ranker.score = score
        try:
            with pytest.raises(AdvisorRankerUnavailable, match="inference timed out"):
                await ranker.rank("first")
            outcomes = await asyncio.gather(*(ranker.rank(str(i)) for i in range(3)), return_exceptions=True)
            assert all(isinstance(x, AdvisorRankerUnavailable) and "admission timed out" in str(x) for x in outcomes)
            assert calls == ["first"]
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        # Completion retrieves the late worker exception and releases exactly one slot.
        await asyncio.sleep(.01)
        assert await ranker.rank("recovered") == .5
        assert calls == ["first", "recovered"]
        assert ranker._capacity._value == 1
        assert not errors

    asyncio.run(scenario())
