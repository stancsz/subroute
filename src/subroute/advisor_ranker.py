"""Local, integrity-checked advisor ranking. Models are provisioned at deploy time."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading
from pathlib import Path

CAPACITY_WAIT_SECONDS = 1.0
INFERENCE_WAIT_SECONDS = 8.5


class AdvisorRankerUnavailable(RuntimeError):
    """The configured learned ranker cannot produce a valid score."""


class AdvisorRequestRanker:
    def __init__(self, artifact_path: Path | None = None, model_dir: Path | None = None):
        self.artifact_path = artifact_path or Path(__file__).parent / "data" / "advisor_ranker.json"
        self.model_dir = model_dir or Path(os.getenv("ADVISOR_RANKER_MODEL_DIR", "/opt/subroute/advisor-ranker"))
        self._lock = threading.Lock()
        self._capacity = asyncio.Semaphore(1)
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        try:
            manifest = json.loads(self.artifact_path.read_text(encoding="utf-8"))
            embedding = manifest["embedding"]
            coefficients, intercepts = manifest["coefficients"], manifest["intercepts"]
            if (manifest["schema_version"] != 1
                    or manifest["algorithm"] != "multilingual-minilm-multinomial-logistic"
                    or manifest["class_labels"] != [0, 1, 2]
                    or embedding["dimension"] != 384
                    or embedding["pooling"] != "attention-masked token mean then L2 normalize"
                    or embedding["truncation"] != {"max_length": 128, "stride": 0, "strategy": "longest_first", "direction": "right"}
                    or embedding["padding"] != {"length": None, "pad_to_multiple_of": None, "pad_id": 0, "pad_token": "<pad>", "pad_type_id": 0, "direction": "right"}
                    or embedding["add_special_tokens"] is not True
                    or embedding["input_dtype"] != "int64"
                    or embedding["input_names"] != ["input_ids", "attention_mask", "token_type_ids"]
                    or embedding["output_name"] != "last_hidden_state"
                    or len(coefficients) != 3 or len(intercepts) != 3
                    or any(len(row) != 384 for row in coefficients)
                    or any(not isinstance(v, (float, int)) or isinstance(v, bool) or not math.isfinite(v)
                           for row in [*coefficients, intercepts] for v in row)):
                raise ValueError("Unsupported ranker artifact contract")
            for key, filename in (("weights_sha256", "model_optimized.onnx"), ("tokenizer_sha256", "tokenizer.json")):
                expected = embedding[key]
                if not isinstance(expected, str) or len(expected) != 64:
                    raise ValueError("Invalid model integrity digest")
                with (self.model_dir / filename).open("rb") as model_file:
                    actual = hashlib.file_digest(model_file, "sha256").hexdigest()
                if actual != expected:
                    raise ValueError("Model integrity mismatch: " + filename)
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer

            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            session = ort.InferenceSession(str(self.model_dir / "model_optimized.onnx"),
                                         sess_options=options, providers=["CPUExecutionProvider"])
            if ([item.name for item in session.get_inputs()] != embedding["input_names"]
                    or session.get_outputs()[0].name != embedding["output_name"]):
                raise ValueError("Unexpected ONNX input/output contract")
            tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
            tokenizer.enable_truncation(**embedding["truncation"])
            tokenizer.enable_padding(**embedding["padding"])
            self._np, self._session, self._tokenizer = np, session, tokenizer
            self._coefficients = np.asarray(coefficients, dtype=np.float64)
            self._intercepts = np.asarray(intercepts, dtype=np.float64)
            self._loaded = True
        except AdvisorRankerUnavailable:
            raise
        except Exception as exc:
            raise AdvisorRankerUnavailable("Advisor ranker load failed: " + type(exc).__name__) from exc

    def score(self, prompt: str) -> float:
        """Rank a bounded copy of the ask; no network access or implicit fallback."""
        if not isinstance(prompt, str):
            raise AdvisorRankerUnavailable("Advisor ranker requires text")
        with self._lock:
            self._load()
            try:
                np = self._np
                encoded = self._tokenizer.encode(prompt[:6000], add_special_tokens=True)
                inputs = {"input_ids": np.asarray([encoded.ids], dtype=np.int64),
                          "attention_mask": np.asarray([encoded.attention_mask], dtype=np.int64),
                          "token_type_ids": np.asarray([encoded.type_ids], dtype=np.int64)}
                hidden = self._session.run(["last_hidden_state"], inputs)[0]
                if hidden.shape != (1, len(encoded.ids), 384) or not np.isfinite(hidden).all():
                    raise ValueError("Invalid encoder output")
                mask = inputs["attention_mask"][..., None]
                pooled = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1e-9)
                pooled = pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
                logits = pooled @ self._coefficients.T + self._intercepts
                probabilities = np.exp(logits - logits.max(axis=1, keepdims=True))
                probabilities /= probabilities.sum(axis=1, keepdims=True)
                result = float(probabilities[0] @ np.asarray([0, .5, 1]))
                if not math.isfinite(result) or not 0 <= result <= 1:
                    raise ValueError("Invalid classifier score")
                return result
            except Exception as exc:
                raise AdvisorRankerUnavailable("Advisor ranker inference failed: " + type(exc).__name__) from exc

    async def rank(self, prompt: str) -> float:
        # Shield preserves the worker. Only its completion releases capacity, even
        # when the original request is cancelled while native inference continues.
        try:
            await asyncio.wait_for(self._capacity.acquire(), CAPACITY_WAIT_SECONDS)
        except TimeoutError as exc:
            raise AdvisorRankerUnavailable("Advisor ranker admission timed out") from exc
        try:
            worker = asyncio.create_task(asyncio.to_thread(self.score, prompt))
        except BaseException:
            self._capacity.release()
            raise

        def finished(task):
            self._capacity.release()
            if not task.cancelled():
                task.exception()  # Retrieve errors even when the caller was cancelled.

        worker.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(worker), INFERENCE_WAIT_SECONDS)
        except TimeoutError as exc:
            raise AdvisorRankerUnavailable("Advisor ranker inference timed out") from exc
