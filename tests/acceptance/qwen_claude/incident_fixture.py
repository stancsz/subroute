"""Generate deterministic stress-intake evidence outside the copied agent fixture.

The records are authored, not a natural incident corpus. No dependencies are needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random


def complete_incident_intake(directory: Path, reads: list[tuple[str, str]]) -> bool:
    """Require four ordered whole-file tool results, not merely four tool calls."""
    expected = [f"incident_{number}.jsonl" for number in range(1, 5)]
    relevant = [(name, output) for name, output in reads if name in expected]
    if [name for name, _ in relevant[:4]] != expected:
        return False
    return all(all(line in output for line in (directory / name).read_text(encoding="utf-8").splitlines())
               for name, output in relevant[:4])


def make_incident_evidence(directory: str | Path) -> None:
    """Write incident_1.jsonl through incident_4.jsonl, 160 records per file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(284731)
    origin = datetime(2026, 10, 15, 11, 45, tzinfo=timezone.utc)
    key_records = {
        1: {
            77: {"ts": "2026-10-15T12:00:10.000Z", "event": "request_started",
                 "incident_id": "INC-284", "request_id": "req-731", "route_id": "route-r17",
                 "change_id": "CHG-92", "service": "context-gateway", "method": "POST",
                 "path": "/v1/chat/completions"},
            94: {"ts": "2026-10-15T12:00:14.600Z", "event": "incident_opened",
                 "incident_id": "INC-284", "request_id": "req-731", "route_id": "route-r17",
                 "severity": "SEV-2", "detector": "gateway_completion_gap", "status": "investigating"},
        },
        2: {
            59: {"ts": "2026-10-15T11:30:00.000Z", "event": "route_policy_effective",
                 "route_id": "route-r17", "policy_id": "P61", "timeout_ms": 9000,
                 "effective_at": "2026-10-15T11:30:00.000Z", "change_id": "CHG-88",
                 "scope": "gateway_request_timeout"},
            83: {"ts": "2026-10-15T11:59:58.000Z", "event": "route_policy_effective",
                 "route_id": "route-r17", "policy_id": "P62", "timeout_ms": 4500,
                 "effective_at": "2026-10-15T11:59:58.000Z", "change_id": "CHG-92",
                 "supersedes": "P61", "scope": "gateway_request_timeout"},
            103: {"ts": "2026-10-15T11:59:59.000Z", "event": "route_policy_scheduled",
                  "route_id": "route-r17", "policy_id": "P63", "timeout_ms": 12000,
                  "effective_at": "2026-10-15T12:15:00.000Z", "change_id": "CHG-97",
                  "scope": "gateway_request_timeout"},
        },
        3: {
            73: {"ts": "2026-10-15T12:00:14.500Z", "event": "gateway_timeout",
                 "request_id": "req-731", "route_id": "route-r17", "policy_id": "P62",
                 "status": 504, "upstream_state": "running", "deadline_source": "route_policy",
                 "incident_id": "INC-284"},
            97: {"ts": "2026-10-15T12:00:16.200Z", "event": "upstream_completed",
                 "request_id": "req-731", "route_id": "route-r17", "upstream_status": 200,
                 "result": "success", "delivery": "discarded_after_gateway_timeout",
                 "incident_id": "INC-284"},
        },
        4: {
            87: {"ts": "2026-10-15T12:03:00.000Z", "event": "incident_mitigation_applied",
                 "incident_id": "INC-284", "route_id": "route-r17", "change_id": "CHG-92",
                 "action": "revert_route_policy", "from_policy": "P62", "to_policy": "P61",
                 "timeout_ms": 9000, "status": "applied"},
        },
    }
    for file_number in range(1, 5):
        rows = []
        for line in range(1, 161):
            if line in key_records[file_number]:
                record = key_records[file_number][line]
            else:
                timestamp = origin + timedelta(seconds=rng.randrange(2400), milliseconds=rng.randrange(1000))
                record = {
                    "ts": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "event": rng.choice(("request_checkpoint", "upstream_probe", "route_health")),
                    "request_id": f"req-{rng.randrange(1000, 9999)}",
                    "route_id": f"route-r{rng.choice((7, 11, 18, 27, 31))}",
                    "policy_id": rng.choice(("P61", "P62", "P63", "P64")),
                    "incident_id": f"INC-{rng.randrange(300, 900)}",
                    "status": rng.choice((200, 200, 200, 202, 504)),
                    "elapsed_ms": rng.randrange(120, 8000),
                    "node": f"gw-{rng.randrange(10, 90)}",
                }
            rows.append(json.dumps(record, separators=(",", ":")))
        (directory / f"incident_{file_number}.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
