import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent
FIELDS = {
    "incident_id", "request_id", "route_id", "change_id", "policy_at_incident",
    "timeout_ms", "upstream_elapsed_ms", "cause", "mitigation_policy",
    "mitigation_timeout_ms", "evidence",
}
CAUSES = {
    "gateway_timeout_below_upstream_duration", "upstream_failure",
    "route_misconfiguration", "capacity_exhaustion", "client_cancelled",
}
FILES = {f"incident_{i}.jsonl" for i in range(1, 5)}


def findings():
    return json.loads((ROOT / "findings.json").read_text(encoding="utf-8"))


class IncidentReconstructionTests(unittest.TestCase):
    def test_exact_schema_and_field_types(self):
        result = findings()
        self.assertIsInstance(result, dict)
        self.assertEqual(set(result), FIELDS)
        for key in FIELDS - {"timeout_ms", "upstream_elapsed_ms", "mitigation_timeout_ms", "evidence"}:
            self.assertIsInstance(result[key], str, key)
            self.assertTrue(result[key].strip(), key)
        for key in ("timeout_ms", "upstream_elapsed_ms", "mitigation_timeout_ms"):
            self.assertIs(type(result[key]), int, key)
            self.assertGreater(result[key], 0, key)
        self.assertIn(result["cause"], CAUSES)

    def test_exact_citations_cover_every_file(self):
        result = findings()
        self.assertIn("evidence", result)
        evidence = result["evidence"]
        self.assertIsInstance(evidence, list)
        self.assertGreaterEqual(len(evidence), len(FILES))
        covered = set()
        for item in evidence:
            self.assertIsInstance(item, dict)
            self.assertEqual(set(item), {"file", "line", "quote"})
            self.assertIn(item["file"], FILES)
            self.assertIs(type(item["line"]), int)
            self.assertIsInstance(item["quote"], str)
            lines = (ROOT / item["file"]).read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(item["line"], 1)
            self.assertLessEqual(item["line"], len(lines))
            self.assertEqual(item["quote"], lines[item["line"] - 1])
            covered.add(item["file"])
        self.assertEqual(covered, FILES)


if __name__ == "__main__":
    unittest.main()
