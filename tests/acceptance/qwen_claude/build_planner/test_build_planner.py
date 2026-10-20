import json
import unittest

from build_manifest import BuildError, Target, load_manifest
from incremental import plan_build


def digest(character):
    return character * 64


def graph():
    return load_manifest(json.dumps({"targets": [
        {"name": "bundle", "command": "bundle", "sources": [], "deps": ["api"]},
        {"name": "api", "command": "compile-api", "sources": ["src/api.py"], "deps": ["leaf"]},
        {"name": "docs", "command": "render-docs", "sources": ["docs/readme.md"], "deps": []},
        {"name": "leaf", "command": "compile-leaf", "sources": ["src/leaf.py"], "deps": []},
    ]}))


class BuildPlannerTests(unittest.TestCase):
    def test_initial_build_then_noop_then_transitive_change(self):
        targets = graph()
        sources = {"src/leaf.py": digest("a"), "src/api.py": digest("b"),
                   "docs/readme.md": digest("c")}
        first = plan_build(targets, sources, {})
        self.assertEqual(first.order, ("docs", "leaf", "api", "bundle"))
        self.assertEqual(first.rebuild, first.order)
        self.assertEqual(plan_build(targets, sources, first.fingerprints).rebuild, ())
        changed = {**sources, "src/leaf.py": digest("d")}
        second = plan_build(targets, changed, first.fingerprints)
        self.assertEqual(second.rebuild, ("leaf", "api", "bundle"))
        self.assertEqual(second.fingerprints["docs"], first.fingerprints["docs"])

    def test_manifest_order_does_not_change_fingerprints(self):
        first = graph()
        reversed_entries = [
            {"name": target.name, "command": target.command,
             "sources": list(reversed(target.sources)), "deps": list(reversed(target.deps))}
            for target in reversed(list(first.values()))
        ]
        second = load_manifest(json.dumps({"targets": reversed_entries}))
        sources = {"src/leaf.py": digest("a"), "src/api.py": digest("b"),
                   "docs/readme.md": digest("c")}
        prior = plan_build(first, sources, {})
        self.assertEqual(plan_build(second, sources, prior.fingerprints).rebuild, ())

    def test_invalid_manifest_paths_and_duplicates(self):
        template = {"name": "one", "command": "compile", "sources": ["src/one.py"], "deps": []}
        for source in ("/abs", "../secret", "src//file", "src/./file", "src\\file", "C:/file", "src/\x00file"):
            with self.subTest(source=source), self.assertRaises(BuildError):
                load_manifest(json.dumps({"targets": [{**template, "sources": [source]}]}))
        with self.assertRaises(BuildError):
            load_manifest(json.dumps({"targets": [template, template]}))
        with self.assertRaises(BuildError):
            load_manifest(json.dumps({"targets": [{**template, "deps": ["missing"]}]}))

    def test_duplicate_json_key_and_invalid_types(self):
        with self.assertRaises(BuildError):
            load_manifest('{"targets": [], "targets": []}')
        for value in (True, 5, None, ""):
            with self.subTest(value=value), self.assertRaises(BuildError):
                load_manifest(json.dumps({"targets": [{"name": value, "command": "x", "sources": [], "deps": []}]}))

    def test_cycles_and_missing_digests(self):
        targets = graph()
        sources = {"src/leaf.py": digest("a"), "src/api.py": digest("b"),
                   "docs/readme.md": digest("c")}
        with self.assertRaises(BuildError):
            plan_build(targets, {**sources, "src/api.py": "bad"}, {})
        with self.assertRaises(BuildError):
            plan_build(targets, {key: value for key, value in sources.items() if key != "src/api.py"}, {})
        cyclic = {"a": Target("a", "build a", (), ("b",)),
                  "b": Target("b", "build b", (), ("a",))}
        with self.assertRaises(BuildError):
            plan_build(cyclic, {}, {})

    def test_does_not_mutate_inputs(self):
        targets = graph()
        sources = {"src/leaf.py": digest("a"), "src/api.py": digest("b"),
                   "docs/readme.md": digest("c")}
        previous = {"leaf": digest("f")}
        before_targets, before_sources, before_previous = dict(targets), dict(sources), dict(previous)
        plan = plan_build(targets, sources, previous)
        plan.fingerprints["leaf"] = digest("0")
        self.assertEqual((targets, sources, previous),
                         (before_targets, before_sources, before_previous))


if __name__ == "__main__":
    unittest.main()
