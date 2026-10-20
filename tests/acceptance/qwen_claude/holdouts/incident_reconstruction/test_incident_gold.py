"""Independent conclusions fixed before either client's first incident run."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class IncidentGoldTests(unittest.TestCase):
    def test_conclusions(self):
        result = json.loads((ROOT / "findings.json").read_text(encoding="utf-8"))
        expected = {
            "incident_id": "INC-284", "request_id": "req-731",
            "route_id": "route-r17", "change_id": "CHG-92",
            "policy_at_incident": "P62", "timeout_ms": 4500,
            "upstream_elapsed_ms": 6200,
            "cause": "gateway_timeout_below_upstream_duration",
            "mitigation_policy": "P61", "mitigation_timeout_ms": 9000,
        }
        self.assertEqual({key: result.get(key) for key in expected}, expected)

    def test_citations_support_the_complete_chain(self):
        result = json.loads((ROOT / "findings.json").read_text(encoding="utf-8"))
        cited = []
        for citation in result.get("evidence", []):
            path = ROOT / citation["file"]
            self.assertIn(path.name, {f"incident_{i}.jsonl" for i in range(1, 5)})
            line = path.read_text(encoding="utf-8").splitlines()[citation["line"] - 1]
            self.assertEqual(citation["quote"], line)
            cited.append(json.loads(line))
        required = (
            {"event": "request_started", "request_id": "req-731", "change_id": "CHG-92"},
            {"event": "incident_opened", "incident_id": "INC-284"},
            {"event": "route_policy_effective", "policy_id": "P62", "timeout_ms": 4500},
            {"event": "gateway_timeout", "request_id": "req-731", "status": 504},
            {"event": "upstream_completed", "request_id": "req-731", "upstream_status": 200},
            {"event": "incident_mitigation_applied", "incident_id": "INC-284", "to_policy": "P61"},
        )
        for claim in required:
            self.assertTrue(any(all(row.get(key) == value for key, value in claim.items())
                                for row in cited), claim)


if __name__ == "__main__":
    unittest.main()
