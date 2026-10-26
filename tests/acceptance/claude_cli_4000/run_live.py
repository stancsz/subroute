"""Provision short-lived local inference keys and run the real CLI tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[3]
API = "http://127.0.0.1:4000"
TESTS = Path(__file__).with_name("test_live_claude.py")
NAMES = (
    "test_default_model_build_planner_coding",
    "test_claude_model_card_billing_coding",
    "test_unknown_model_falls_back_sse_coding",
)


def master_key() -> str:
    value = os.getenv("GATEWAY_MASTER_KEY", "").strip()
    if value:
        return value
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        name, separator, raw = line.partition("=")
        if separator and name.strip() == "GATEWAY_MASTER_KEY":
            return raw.strip().strip("\"'")
    raise RuntimeError("GATEWAY_MASTER_KEY is missing")


def request(path: str, master: str, body: dict | None = None) -> dict:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": "Bearer " + master}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = Request(API + path, data=payload, headers=headers,
                  method="GET" if payload is None else "POST")
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"{path} returned HTTP {error.code}") from None


def main() -> int:
    master = master_key()
    names = tuple(sys.argv[1:]) or NAMES
    if any(name not in NAMES for name in names):
        raise SystemExit(f"choose from: {', '.join(NAMES)}")
    status = 0
    for name in names:
        alias = "claude-acceptance-" + uuid.uuid4().hex[:12]
        # This key exists solely for one local test. Its short lifetime and
        # budget cap bound an interrupted run; deletion is still mandatory.
        grant = request("/key/generate", master, {
            "key_alias": alias, "duration": "20m", "budget_duration": "20m",
            "max_budget": 5,
            "models": [],
        })
        key = grant.get("key")
        if not isinstance(key, str) or not key:
            raise RuntimeError("/key/generate returned no inference key")
        try:
            env = dict(os.environ, RUN_SUBROUTE_CLAUDE_ACCEPTANCE="1",
                       SUBROUTE_ACCEPTANCE_API_KEY=key)
            env.pop("GATEWAY_MASTER_KEY", None)
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", f"{TESTS}::{name}"],
                                    cwd=ROOT, env=env, timeout=950)
            if result.returncode:
                status = result.returncode
        finally:
            request("/key/delete", master, {"keys": [key]})
            listing = request("/key/list", master)
            if alias in json.dumps(listing):
                raise RuntimeError(f"temporary inference key alias {alias} still exists")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
