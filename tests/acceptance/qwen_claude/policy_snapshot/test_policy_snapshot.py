import json
from pathlib import Path
import tempfile
import threading
import unittest

from policy_snapshot import Policy, PolicyStore


class PolicySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.default = Policy("qwen3.8-27b", "alias", None, 0)

    def test_routing_and_advisor_from_one_revision(self):
        store = PolicyStore(self.root / "staging.json", self.default)
        self.assertEqual(store.resolve("default").model, "qwen3.8-27b")
        changed = store.update(active_model="minimax", mode="force", advisor_model="advisor")
        self.assertEqual(changed.revision, 1)
        for requested, expected in (("default", "minimax"), ("other", "minimax"), ("auto", "auto")):
            decision = store.resolve(requested)
            self.assertEqual((decision.model, decision.advisor_model, decision.revision),
                             (expected, "advisor", 1))
        store.update(active_model="openrouter", mode="alias", advisor_model=None)
        self.assertEqual(store.resolve("other").model, "other")
        self.assertEqual(store.resolve("default").model, "openrouter")

    def test_persistence_and_environment_isolation(self):
        staging = PolicyStore(self.root / "staging.json", self.default)
        production = PolicyStore(self.root / "production.json", self.default)
        staging.update(active_model="minimax", mode="force", advisor_model="reviewer")
        self.assertEqual(production.snapshot(), self.default)
        loaded = PolicyStore(self.root / "staging.json", self.default)
        self.assertEqual(loaded.snapshot(), staging.snapshot())
        self.assertEqual(json.loads((self.root / "staging.json").read_text())["revision"], 1)

    def test_invalid_update_keeps_prior_state_and_file(self):
        path = self.root / "policy.json"
        store = PolicyStore(path, self.default)
        store.update(active_model="minimax", mode="force", advisor_model=None)
        before = path.read_bytes()
        for model, mode in (("", "force"), ("qwen", "mystery")):
            with self.assertRaises(ValueError):
                store.update(active_model=model, mode=mode, advisor_model=None)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(store.snapshot().revision, 1)

    def test_corrupt_file_is_rejected_without_overwrite(self):
        path = self.root / "policy.json"
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            PolicyStore(path, self.default)
        self.assertEqual(path.read_text(encoding="utf-8"), "not json")

    def test_invalid_persisted_values_are_rejected_without_overwrite(self):
        path = self.root / "policy.json"
        invalid_policies = (
            {"active_model": "", "mode": "alias", "advisor_model": None, "revision": 1},
            {"active_model": "qwen", "mode": "mystery", "advisor_model": None, "revision": 1},
            {"active_model": "qwen", "mode": "alias", "advisor_model": [], "revision": 1},
            {"active_model": "qwen", "mode": "alias", "advisor_model": " ", "revision": 1},
            {"active_model": "qwen", "mode": "alias", "advisor_model": None, "revision": -1},
            {"active_model": "qwen", "mode": "alias", "advisor_model": None, "revision": True},
            {"active_model": "qwen", "mode": "alias", "advisor_model": None, "revision": 1.5},
        )
        for invalid in invalid_policies:
            with self.subTest(policy=invalid):
                original = json.dumps(invalid)
                path.write_text(original, encoding="utf-8")
                with self.assertRaises(ValueError):
                    PolicyStore(path, self.default)
                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_concurrent_resolves_never_mix_revisions(self):
        store = PolicyStore(self.root / "policy.json", self.default)
        observed = []
        errors = []
        start = threading.Barrier(4)
        def read_many():
            try:
                start.wait()
                for _ in range(500):
                    observed.append(store.resolve("default"))
            except BaseException as error:
                errors.append(error)
        readers = [threading.Thread(target=read_many) for _ in range(3)]
        for thread in readers:
            thread.start()
        start.wait()
        store.update(active_model="minimax", mode="force", advisor_model="reviewer")
        for thread in readers:
            thread.join()
        self.assertFalse(errors, f"reader thread failed: {errors!r}")
        valid = {("qwen3.8-27b", None, 0), ("minimax", "reviewer", 1)}
        self.assertTrue(observed)
        self.assertTrue(all((item.model, item.advisor_model, item.revision) in valid for item in observed))


if __name__ == "__main__":
    unittest.main()
