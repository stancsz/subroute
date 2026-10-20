import copy
import json
import unittest

from build_manifest import BuildError, Target, load_manifest
from incremental import plan_build


def d(character):
    return character * 64


class BuildHoldout(unittest.TestCase):
    def test_diamond_change_and_stable_order(self):
        raw = {"targets": [
            {"name": "final", "command": "link", "sources": [], "deps": ["left", "right"]},
            {"name": "right", "command": "right", "sources": ["src/right"], "deps": ["core"]},
            {"name": "left", "command": "left", "sources": ["src/left"], "deps": ["core"]},
            {"name": "core", "command": "core", "sources": ["src/core"], "deps": []},
        ]}
        targets = load_manifest(json.dumps(raw))
        sources = {"src/core": d("a"), "src/left": d("b"), "src/right": d("c")}
        first = plan_build(targets, sources, {})
        self.assertEqual(first.order, ("core", "left", "right", "final"))
        changed = plan_build(targets, {**sources, "src/core": d("e")}, first.fingerprints)
        self.assertEqual(changed.rebuild, first.order)
        self.assertEqual(plan_build(targets, sources, first.fingerprints).rebuild, ())

    def test_command_change_rebuilds_dependents_only(self):
        base = load_manifest(json.dumps({"targets": [
            {"name": "lib", "command": "compile -O1", "sources": ["lib.c"], "deps": []},
            {"name": "app", "command": "link", "sources": ["app.c"], "deps": ["lib"]},
            {"name": "docs", "command": "render", "sources": ["docs.md"], "deps": []},
        ]}))
        sources = {"lib.c": d("a"), "app.c": d("b"), "docs.md": d("c")}
        old = plan_build(base, sources, {})
        changed = dict(base)
        changed["lib"] = Target("lib", "compile -O2", ("lib.c",), ())
        result = plan_build(changed, sources, old.fingerprints)
        self.assertEqual(result.rebuild, ("lib", "app"))
        self.assertEqual(result.fingerprints["docs"], old.fingerprints["docs"])

    def test_direct_target_validation_and_inputs_unchanged(self):
        source = {"a": Target("a", "run", ("a.txt",), ())}
        digests, previous = {"a.txt": d("a")}, {"a": d("b")}
        before = copy.deepcopy((source, digests, previous))
        plan_build(source, digests, previous)
        self.assertEqual((source, digests, previous), before)
        for bad in ({"x": Target("different", "run", (), ())},
                    {"x": Target("x", "run", (), ("missing",))},
                    {"x": Target("x", "run", ("../escape",), ())}):
            with self.subTest(bad=bad), self.assertRaises(BuildError):
                plan_build(bad, {}, {})

    def test_rejects_ambiguous_manifest_and_invalid_previous(self):
        with self.assertRaises(BuildError):
            load_manifest('{"targets":[{"name":"a","command":"x","sources":[],"deps":[],"command":"y"}]}')
        targets = load_manifest('{"targets":[{"name":"a","command":"x","sources":[],"deps":[]}]}')
        with self.assertRaises(BuildError):
            plan_build(targets, {}, {"a": True})

    def test_fingerprint_encoding_is_unambiguous(self):
        source_digest = d("a")
        injected = {"app": Target("app", "build\x1fsource\x1fsrc/a.py\x1f" + source_digest,
                                  (), ())}
        structured = {"app": Target("app", "build", ("src/a.py",), ())}
        digests = {"src/a.py": source_digest}
        self.assertNotEqual(
            plan_build(injected, digests, {}).fingerprints["app"],
            plan_build(structured, digests, {}).fingerprints["app"],
        )


if __name__ == "__main__":
    unittest.main()
