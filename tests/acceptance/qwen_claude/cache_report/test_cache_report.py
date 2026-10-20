import json
import unittest

from cache_report import summarize_events


def event(**values):
    return json.dumps(values, ensure_ascii=False)


class CacheReportTests(unittest.TestCase):
    def test_aggregates_models_and_cache_hits(self):
        lines = [
            event(model="qwen3.8-27b", prompt_tokens=100, cached_tokens=40, latency_ms=10),
            event(model="qwen3.8-27b", prompt_tokens=50, cached_tokens=0, latency_ms=21),
            event(model="模型", prompt_tokens=20, cached_tokens=5, latency_ms=5),
        ]
        self.assertEqual(summarize_events(lines), {
            "qwen3.8-27b": {
                "requests": 2, "cache_hits": 1, "prompt_tokens": 150,
                "cached_tokens": 40, "mean_latency_ms": 15.5,
            },
            "模型": {
                "requests": 1, "cache_hits": 1, "prompt_tokens": 20,
                "cached_tokens": 5, "mean_latency_ms": 5.0,
            },
        })

    def test_skips_malformed_or_invalid_records(self):
        lines = [
            "not json",
            event(model="qwen3.8-27b", prompt_tokens=10, cached_tokens=11, latency_ms=3),
            event(model="qwen3.8-27b", prompt_tokens=-1, cached_tokens=0, latency_ms=3),
            event(model="qwen3.8-27b", prompt_tokens=10, cached_tokens=0, latency_ms=-3),
            event(model="", prompt_tokens=10, cached_tokens=0, latency_ms=3),
            event(model="qwen3.8-27b", prompt_tokens=True, cached_tokens=0, latency_ms=3),
            event(model="qwen3.8-27b", prompt_tokens=10, cached_tokens=0, latency_ms=float("inf")),
            event(model="qwen3.8-27b", prompt_tokens=10, cached_tokens=0, latency_ms=3),
        ]
        self.assertEqual(summarize_events(lines)["qwen3.8-27b"]["requests"], 1)

    def test_empty_input(self):
        self.assertEqual(summarize_events([]), {})


if __name__ == "__main__":
    unittest.main()
