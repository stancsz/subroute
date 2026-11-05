"""Small, durable, environment-local numeric usage spool for master requests."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any


MAX_ROWS = 1000
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024
REPLAY_BATCH = 25


class UsageOutbox:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=1)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA max_page_count=4096")
        db.execute("""CREATE TABLE IF NOT EXISTS usage_outbox (
            attempt_id TEXT PRIMARY KEY, environment TEXT NOT NULL,
            receipt TEXT NOT NULL, revision INTEGER NOT NULL,
            outcome TEXT NOT NULL, updated_at INTEGER NOT NULL)""")
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _put(self, receipt: dict[str, Any], revision: int, outcome: str, updated_at: int):
        raw = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
        if len(raw.encode()) > MAX_RECORD_BYTES:
            raise OSError("usage outbox record limit exceeded")
        with self._lock, self._connection() as db:
            exists = db.execute("SELECT 1 FROM usage_outbox WHERE attempt_id=?", (receipt["attempt_id"],)).fetchone()
            rows, size = db.execute("SELECT count(*), coalesce(sum(length(receipt)),0) FROM usage_outbox").fetchone()
            if not exists and (rows >= MAX_ROWS or size + len(raw) > MAX_PAYLOAD_BYTES):
                raise OSError("usage outbox capacity exceeded")
            db.execute("""INSERT INTO usage_outbox VALUES(?,?,?,?,?,?)
                ON CONFLICT(attempt_id) DO UPDATE SET receipt=excluded.receipt,
                revision=excluded.revision,outcome=excluded.outcome,updated_at=excluded.updated_at""",
                (receipt["attempt_id"], receipt["environment"], raw, revision, outcome, updated_at))

    async def put(self, receipt: dict[str, Any], revision: int, outcome: str, updated_at: int):
        await asyncio.to_thread(self._put, receipt, revision, outcome, updated_at)

    def _update(self, attempt_id: str, facts: dict, outcome: str | None = None) -> bool:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT receipt,revision,outcome,updated_at FROM usage_outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
            if not row:
                return False
            # A late callback must not downgrade a completed receipt.
            if row[2] == "complete":
                return True
            receipt = json.loads(row[0])
            # Numeric facts and the two explicit accounting booleans only.
            for key, value in facts.items():
                if key.endswith("_tokens") or key == "provider_cost_micros":
                    if type(value) is int and value >= 0:
                        receipt[key] = max(receipt.get(key, value), value)
                elif key == "usage_complete" and type(value) is bool:
                    receipt[key] = value
            final = row[2] if row[2] != "pending" else (outcome or "pending")
            updated_at = __import__("time").time_ns() // 1_000_000
            raw = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
            if len(raw.encode()) > MAX_RECORD_BYTES:
                raise OSError("usage outbox record limit exceeded")
            total = db.execute("SELECT coalesce(sum(length(receipt)),0) FROM usage_outbox").fetchone()[0]
            if total - len(row[0]) + len(raw) > MAX_PAYLOAD_BYTES:
                raise OSError("usage outbox capacity exceeded")
            db.execute("UPDATE usage_outbox SET receipt=?,revision=?,outcome=?,updated_at=? WHERE attempt_id=?",
                       (raw, row[1] + 1, final, updated_at, attempt_id))
            return True

    async def update(self, attempt_id: str, facts: dict, outcome: str | None = None) -> bool:
        return await asyncio.to_thread(self._update, attempt_id, facts, outcome)

    def _batch(self, environment: str):
        with self._lock, self._connection() as db:
            return [(json.loads(r), rev, outcome, updated) for r, rev, outcome, updated in db.execute(
                "SELECT receipt,revision,outcome,updated_at FROM usage_outbox WHERE environment=? ORDER BY CASE WHEN outcome='pending' THEN 1 ELSE 0 END, updated_at LIMIT ?",
                (environment, REPLAY_BATCH))]

    async def batch(self, environment: str):
        return await asyncio.to_thread(self._batch, environment)

    def _ack(self, attempt_id: str, revision: int):
        with self._lock, self._connection() as db:
            db.execute("DELETE FROM usage_outbox WHERE attempt_id=? AND revision<=? AND outcome!='pending'", (attempt_id, revision))

    async def ack(self, attempt_id: str, revision: int):
        await asyncio.to_thread(self._ack, attempt_id, revision)

    def _interrupt_pending(self):
        with self._lock, self._connection() as db:
            rows = db.execute("SELECT attempt_id,receipt,revision FROM usage_outbox WHERE outcome='pending'").fetchall()
            for ident, raw, revision in rows:
                receipt = json.loads(raw)
                receipt["usage_complete"] = False
                db.execute("UPDATE usage_outbox SET receipt=?,revision=?,outcome='interrupted' WHERE attempt_id=?",
                           (json.dumps(receipt, separators=(",", ":"), sort_keys=True), revision + 1, ident))

    async def interrupt_pending(self):
        await asyncio.to_thread(self._interrupt_pending)
