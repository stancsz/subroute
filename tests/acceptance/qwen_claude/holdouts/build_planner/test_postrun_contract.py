"""Added after the 2026-10-15 20:18Z Codex run by independent review.

That run passed the original five holdouts. These two checks did not form its
predeclared test denominator; they enforce additional clauses already in README.
"""

import unittest

from build_manifest import BuildError, Target
from incremental import plan_build


class AdditionalContractTests(unittest.TestCase):
    def test_source_and_dependency_are_distinct_fingerprint_domains(self):
        leaf = Target("leaf", "compile", (), ())
        digest = plan_build({"leaf": leaf}, {}, {}).fingerprints["leaf"]
        source = plan_build({"root": Target("root", "build", ("leaf",), ())}, {"leaf": digest}, {})
        dependency = plan_build({"root": Target("root", "build", (), ("leaf",)), "leaf": leaf}, {}, {})
        self.assertNotEqual(source.fingerprints["root"], dependency.fingerprints["root"])

    def test_malformed_direct_targets_are_rejected(self):
        for target, digests in (
            (Target("bad name", "build", (), ()), {}),
            (Target("root", "build", ("../escape",), ()), {"../escape": "a" * 64}),
            (Target("root", "build", ("source", "source"), ()), {"source": "a" * 64}),
        ):
            with self.subTest(target=target), self.assertRaises(BuildError):
                plan_build({target.name: target}, digests, {})
