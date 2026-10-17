"""Offline synthetic-trained advisor ranker comparison. Historical labels never fit models.

Historical inputs/outputs belong outside the repository. Top-budget metrics evaluate
ranking only, not a deployable online frequency guarantee or measured advice benefit.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import time
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import FeatureUnion
from sklearn.preprocessing import normalize
from sklearn.svm import LinearSVC


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def vectorizer():
    return FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), max_features=2000, sublinear_tf=True)),
        ("char", TfidfVectorizer(analyzer="char", ngram_range=(3, 5), max_features=5000, sublinear_tf=True)),
    ])


def rank_metrics(ids, labels, scores):
    order = sorted(range(len(ids)), key=lambda i: (-float(scores[i]), ids[i]))
    ideal = sorted(labels, reverse=True)
    def dcg(values):
        return sum((2 ** float(v) - 1) / math.log2(i + 2) for i, v in enumerate(values))
    denominator = dcg(ideal)
    result = {"n": len(ids), "ndcg": dcg([labels[i] for i in order]) / denominator if denominator else 1.0}
    for rate in (.3, .5, .7):
        k = max(1, math.ceil(len(ids) * rate))
        selected = [labels[i] for i in order[:k]]
        result[str(int(rate * 100))] = {"k": k, "actual_selection_rate": k / len(ids),
            "mean_graded_gain": float(np.mean(selected)),
            "precision_high": float(np.mean(np.asarray(selected) == 2)),
            "recall_high": sum(v == 2 for v in selected) / max(1, sum(v == 2 for v in labels))}
    return result


def fitted_scores(train_x, train_y, test_x, kind):
    if kind == "prototype":
        centroids = np.vstack([np.asarray(train_x[np.asarray(train_y) == c].mean(axis=0)).reshape(-1) for c in (0, 1, 2)])
        model = normalize(centroids)
        return head_scores(model, test_x, kind), model
    model = (LogisticRegression(C=1.0, max_iter=2000, random_state=17)
             if kind == "logistic" else LinearSVC(C=1.0, random_state=17))
    model.fit(train_x, train_y)
    return head_scores(model, test_x, kind), model


def head_scores(model, features, kind):
    if kind == "prototype":
        similarities = np.asarray(features @ model.T)
        return similarities[:, 2] - similarities[:, 0]
    if kind == "logistic":
        return model.predict_proba(features) @ (model.classes_ / 2)
    margins = model.decision_function(features)
    return (margins[:, 2] - margins[:, 0]) / 2


def embedding_model(cache):
    # A specific cached snapshot is mandatory; no network download or text egress.
    from fastembed import TextEmbedding
    root = Path(cache) / "models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
    revision = (root / "refs" / "main").read_text().strip()
    snapshot = root / "snapshots" / revision
    weights = snapshot / "model_optimized.onnx"
    if not weights.is_file():
        raise ValueError("Pinned cached embedding weights are missing")
    model = TextEmbedding("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                          specific_model_path=str(snapshot), local_files_only=True, threads=2)
    return model, {"repository": "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
                   "revision": revision, "weights_sha256": sha(weights),
                   "tokenizer_sha256": sha(snapshot / "tokenizer.json"), "local_files_only": True}


def export_artifact(path, vect, model, train_path):
    parts = []
    for name, item in vect.transformer_list:
        parts.append({"name": name, "analyzer": item.analyzer,
            "ngram_range": list(item.ngram_range), "max_features": item.max_features,
            "lowercase": item.lowercase, "strip_accents": item.strip_accents,
            "token_pattern": item.token_pattern, "sublinear_tf": item.sublinear_tf,
            "smooth_idf": item.smooth_idf, "norm": item.norm,
            "vocabulary": {k: int(v) for k, v in item.vocabulary_.items()}, "idf": item.idf_.tolist()})
    artifact = {"schema_version": 1, "algorithm": "tfidf-word-char-multinomial-logistic",
        "version": "synthetic120-v1", "training_sha256": sha(train_path),
        "label_provenance": "AI-authored synthetic policy preferences; not outcome benefit probabilities",
        "class_labels": model.classes_.tolist(), "score": "sum(probability[class] * class / 2)",
        "vectorizers": parts, "feature_combination": "concatenate separately L2-normalized word then char",
        "coefficients": model.coef_.tolist(), "intercepts": model.intercept_.tolist(),
        "training_parameters": {"C": 1.0, "max_iter": 2000, "random_state": 17},
        "scikit_learn_version": importlib.metadata.version("scikit-learn")}
    Path(path).write_text(json.dumps(artifact, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def export_embedding_artifact(path, model, embedder, provenance, train_path):
    runtime = embedder.model
    artifact = {"schema_version": 1, "algorithm": "multilingual-minilm-multinomial-logistic",
        "version": "synthetic120-v1", "training_sha256": sha(train_path),
        "label_provenance": "AI-authored synthetic policy preferences; not measured outcome benefit",
        "embedding": {**provenance, "pooling": "attention-masked token mean then L2 normalize",
            "dimension": 384, "onnx_filename": "model_optimized.onnx", "tokenizer_filename": "tokenizer.json",
            "truncation": runtime.tokenizer.truncation, "padding": runtime.tokenizer.padding,
            "add_special_tokens": True, "input_dtype": "int64",
            "input_names": [x.name for x in runtime.model.get_inputs()],
            "output_name": runtime.model.get_outputs()[0].name,
            "source_url": "https://huggingface.co/" + provenance["repository"] + "/tree/" + provenance["revision"]},
        "class_labels": model.classes_.tolist(), "coefficients": model.coef_.tolist(),
        "intercepts": model.intercept_.tolist(), "score": "softmax logits weighted by class / 2",
        "training_parameters": {"C": 1.0, "max_iter": 2000, "random_state": 17},
        "scikit_learn_version": importlib.metadata.version("scikit-learn"),
        "fastembed_evaluation_version": importlib.metadata.version("fastembed")}
    Path(path).write_text(json.dumps(artifact, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("train", "cases", "labels", "replay", "output"):
        parser.add_argument("--" + arg, required=True, type=Path)
    parser.add_argument("--embedding-cache", type=Path)
    parser.add_argument("--export", type=Path)
    parser.add_argument("--export-embedding", type=Path)
    parser.add_argument("--litellm-scores", type=Path, help="Optional offline deployed-LiteLLM score receipt")
    parser.add_argument("--split", default="diagnostic25", choices=("diagnostic25", "final38"))
    parser.add_argument("--allow-final", action="store_true", help="Explicitly unlock previously untouched final38")
    args = parser.parse_args()
    if args.split == "final38" and not args.allow_final:
        parser.error("final38 is locked; explicit --allow-final required")
    training = load(args.train)["records"]
    all_cases = {x["id"]: x for x in load(args.cases)}
    label_split = "final_unseen38" if args.split == "final38" else args.split
    labels = [x for x in load(args.labels)["labels"] if x["split"] == label_split and x["need"] is not None]
    if not labels:
        parser.error("No graded labels found for the requested split")
    ids = [x["id"] for x in labels]
    test_y = np.asarray([x["need"] for x in labels])
    texts = [all_cases[i]["text"] for i in ids]
    train_text = [x["text"] for x in training]
    train_y = np.asarray([x["need"] for x in training])
    groups = [x["family"] for x in training]
    report = {"schema_version": 1, "split": args.split, "training_n": len(training),
        "excluded_null_labels": sum(x["split"] == label_split and x["need"] is None for x in load(args.labels)["labels"]),
        "training_sha256": sha(args.train), "labels_sha256": sha(args.labels),
        "limitations": ["AI-authored synthetic training; independent AI-proxy historical labels, not human ground truth",
            "Ranking at retrospective quotas does not calibrate online invocation rates",
            "Historical examples and labels never fit vectorizers, classifiers, prototypes, or thresholds",
            "No measured downstream marginal advisor benefit; small nonrepresentative traffic sample"],
        "methods": {}, "synthetic_group_cv": {}}
    def record(name, scores, latency=None):
        report["methods"][name] = rank_metrics(ids, test_y, scores)
        report["methods"][name]["per_query_ms"] = latency
        report["methods"][name]["scores"] = {i: float(s) for i, s in zip(ids, scores)}
    random_scores = [int(hashlib.sha256(("random-seed17:" + i).encode()).hexdigest()[:12], 16) / 16**12 for i in ids]
    record("deterministic_random", random_scores)
    random_metrics = [rank_metrics(ids, test_y, np.random.default_rng(seed).random(len(ids))) for seed in range(1000)]
    report["random_baseline_1000_seeds"] = {"seeds": "0..999", "ndcg_mean": float(np.mean([x["ndcg"] for x in random_metrics]))}
    for budget in ("30", "50", "70"):
        report["random_baseline_1000_seeds"][budget] = {}
        for metric in ("mean_graded_gain", "precision_high", "recall_high"):
            values = [x[budget][metric] for x in random_metrics]
            report["random_baseline_1000_seeds"][budget][metric] = {
                "mean": float(np.mean(values)), "p05": float(np.percentile(values, 5)), "p95": float(np.percentile(values, 95))}
    replay = {x["id"]: x for x in load(args.replay)}
    for field in ("score_no_history", "score_reconstructed_history"):
        if all(i in replay and field in replay[i] for i in ids):
            record("existing_rules_" + field, [replay[i][field] for i in ids], float(np.mean([replay[i]["ms"] for i in ids])))
    if args.litellm_scores:
        for method, receipt in load(args.litellm_scores).items():
            record("litellm_" + method, [receipt["scores"][i] for i in ids], receipt["per_query_ms"])
    vect = vectorizer()
    train_x = vect.fit_transform(train_text)
    test_x = vect.transform(texts)
    fitted_lexical = None
    for kind in ("prototype", "logistic", "svc"):
        scores, model = fitted_scores(train_x, train_y, test_x, kind)
        start = time.perf_counter()
        for text in texts:
            x = vect.transform([text])
            head_scores(model, x, kind)
        latency = (time.perf_counter() - start) * 1000 / len(texts)
        record("tfidf_" + kind, scores, latency)
        report["methods"]["tfidf_" + kind]["latency_scope"] = "serial transform and scoring; excludes fitting"
        if kind == "logistic":
            fitted_lexical = model
    folds = list(GroupKFold(n_splits=min(4, len(set(groups)))).split(train_text, train_y, groups))
    for kind in ("prototype", "logistic", "svc"):
        predictions = np.zeros(len(training))
        for train, valid in folds:
            v = vectorizer()
            a = v.fit_transform([train_text[i] for i in train])
            b = v.transform([train_text[i] for i in valid])
            predictions[valid], _ = fitted_scores(a, train_y[train], b, kind)
        report["synthetic_group_cv"]["tfidf_" + kind] = rank_metrics([x["id"] for x in training], train_y, predictions)
    if args.export:
        export_artifact(args.export, vect, fitted_lexical, args.train)
        report["export_sha256"] = sha(args.export)
    if args.embedding_cache:
        embedder, provenance = embedding_model(args.embedding_cache)
        report["embedding_model"] = provenance
        embedded_train = normalize(np.asarray(list(embedder.embed(train_text))))
        start = time.perf_counter()
        embedded_test = normalize(np.asarray(list(embedder.embed(texts))))
        batch_per_query = (time.perf_counter() - start) * 1000 / len(texts)
        for kind in ("prototype", "logistic"):
            scores, model = fitted_scores(embedded_train, train_y, embedded_test, kind)
            if kind == "logistic" and args.export_embedding:
                export_embedding_artifact(args.export_embedding, model, embedder, provenance, args.train)
                report["embedding_export_sha256"] = sha(args.export_embedding)
            timings = []
            for text in texts:
                start = time.perf_counter()
                features = normalize(np.asarray(list(embedder.embed([text]))))
                head_scores(model, features, kind)
                timings.append((time.perf_counter() - start) * 1000)
            record("embedding_" + kind, scores, float(np.mean(timings)))
            report["methods"]["embedding_" + kind].update({
                "latency_scope": "serial embedding and scoring; excludes model loading",
                "per_query_p95_ms": float(np.percentile(timings, 95)),
                "batched_embedding_amortized_ms": batch_per_query})
            predictions = np.zeros(len(training))
            for train, valid in folds:
                predictions[valid], _ = fitted_scores(embedded_train[train], train_y[train], embedded_train[valid], kind)
            report["synthetic_group_cv"]["embedding_" + kind] = rank_metrics([x["id"] for x in training], train_y, predictions)
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({name: {k: v for k, v in result.items() if k != "scores"} for name, result in report["methods"].items()}, indent=2))


if __name__ == "__main__":
    main()
