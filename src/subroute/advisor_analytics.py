"""Numeric advisor request counts in the gateway's existing PostgreSQL database.

Recording is best effort and never changes routing. Counts cover recorded requests,
not historical requests predating deployment, and never retain prompt contents.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import HTTPException

from subroute.usage_accounting import ReceiptStore

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 1.0
SCHEMA = """CREATE TABLE IF NOT EXISTS subroute_advisor_requests (
    request_id TEXT PRIMARY KEY,
    started_at BIGINT NOT NULL,
    eligible BOOLEAN NOT NULL DEFAULT FALSE,
    selected BOOLEAN NOT NULL DEFAULT FALSE,
    invoked BOOLEAN NOT NULL DEFAULT FALSE,
    successful BOOLEAN NOT NULL DEFAULT FALSE,
    failed BOOLEAN NOT NULL DEFAULT FALSE,
    provider_attempts INTEGER NOT NULL DEFAULT 0
)"""
UPSERT = """INSERT INTO subroute_advisor_requests
    (request_id,started_at,eligible,selected,invoked,successful,failed,provider_attempts)
    VALUES($1,$2,$3,$4,$5,$6,$7,$8)
    ON CONFLICT(request_id) DO UPDATE SET
      eligible=subroute_advisor_requests.eligible OR EXCLUDED.eligible,
      selected=subroute_advisor_requests.selected OR EXCLUDED.selected,
      invoked=subroute_advisor_requests.invoked OR EXCLUDED.invoked,
      successful=subroute_advisor_requests.successful OR EXCLUDED.successful,
      failed=subroute_advisor_requests.failed OR EXCLUDED.failed,
      provider_attempts=GREATEST(subroute_advisor_requests.provider_attempts,EXCLUDED.provider_attempts)"""
SUMMARY = """SELECT COUNT(*) AS total,
    COUNT(*) FILTER (WHERE eligible) AS eligible,
    COUNT(*) FILTER (WHERE selected) AS selected,
    COUNT(*) FILTER (WHERE invoked) AS invoked,
    COUNT(*) FILTER (WHERE successful) AS successful,
    COUNT(*) FILTER (WHERE failed) AS failed,
    COALESCE(SUM(provider_attempts),0) AS provider_attempts,
    (SELECT MIN(started_at) FROM subroute_advisor_requests) AS coverage_start_ms
    FROM subroute_advisor_requests WHERE started_at >= $1 AND started_at <= $2"""
COUNT_FIELDS = ("total", "eligible", "selected", "invoked", "successful", "failed", "provider_attempts")


class AdvisorAnalytics:
    def __init__(self, store=None):
        self.store = store if store is not None else ReceiptStore()
        self._ready = False
        self._schema_lock = asyncio.Lock()
        self._recording_errors = 0
        # A restart cannot certify writes made by earlier processes. Persisted
        # rows remain counted, while complete coverage needs an error-free epoch.
        self._verified_since_ms = int(time.time() * 1000)

    async def _ensure(self):
        if self._ready:
            return
        async with self._schema_lock:
            if not self._ready:
                await self.store.execute(SCHEMA)
                await self.store.execute("CREATE INDEX IF NOT EXISTS subroute_advisor_requests_started ON subroute_advisor_requests(started_at)")
                self._ready = True

    async def record(self, request_id: str, *, eligible=False, selected=False,
                     invoked=False, successful=False, failed=False, provider_attempts=0) -> bool:
        """Monotone, duplicate-safe markers. Attempts is an absolute cumulative count.

        failed may include a ranking failure before any provider invocation.
        Cancellation propagates; other analytics failures only log their type.
        """
        try:
            if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
                raise ValueError("Invalid server request ID")
            if any(type(value) is not bool for value in (eligible, selected, invoked, successful, failed)):
                raise ValueError("Analytics markers must be boolean")
            if type(provider_attempts) is not int or not 0 <= provider_attempts <= 100:
                raise ValueError("Invalid provider attempt count")
            invoked = invoked or successful or provider_attempts > 0
            selected = selected or invoked
            eligible = eligible or selected
            provider_attempts = max(provider_attempts, int(invoked))
            started_at = int(time.time() * 1000)
            async with asyncio.timeout(TIMEOUT_SECONDS):
                await self._ensure()
                await self.store.execute(UPSERT, request_id, started_at, eligible, selected,
                                         invoked, successful, failed, provider_attempts)
            return True
        except asyncio.CancelledError:
            self._recording_errors += 1
            self._verified_since_ms = max(self._verified_since_ms, int(time.time() * 1000))
            raise
        except Exception as exc:
            self._recording_errors += 1
            self._verified_since_ms = max(self._verified_since_ms, int(time.time() * 1000))
            logger.warning("advisor analytics record unavailable error_type=%s", type(exc).__name__)
            return False

    async def summary(self, minutes: int) -> dict:
        """Return recorded counts and coverage; storage failure is never a zero."""
        if type(minutes) is not int or minutes not in (30, 60, 120):
            raise ValueError("Window must be 30, 60, or 120 minutes")
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - minutes * 60_000
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                await self._ensure()
                rows = await self.store.query(SUMMARY, cutoff, now_ms)
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise ValueError("Invalid analytics aggregate")
            row = rows[0]
            counts = {key: int(row[key]) for key in COUNT_FIELDS}
            if any(value < 0 for value in counts.values()):
                raise ValueError("Invalid analytics count")
            coverage = row.get("coverage_start_ms")
            coverage = int(coverage) if coverage is not None else None
            if (not counts["successful"] <= counts["invoked"] <= counts["selected"] <= counts["eligible"] <= counts["total"]
                    or counts["failed"] > counts["total"] or counts["provider_attempts"] < counts["invoked"]
                    or (coverage is not None and coverage > now_ms)):
                raise ValueError("Inconsistent analytics aggregate")
            verified_start = max(coverage if coverage is not None else self._verified_since_ms,
                                 self._verified_since_ms)
            partial = coverage is None or verified_start > cutoff
            return {"window_minutes": minutes, "window_start_ms": cutoff, "as_of_ms": now_ms,
                    "coverage_start_ms": coverage,
                    "coverage_verified_start_ms": verified_start,
                    "verified_since_ms": self._verified_since_ms,
                    "partial_window": partial,
                    "recording_errors_since_process_start": self._recording_errors,
                    "counts_may_be_incomplete": partial,
                    **counts}
        except Exception as exc:
            logger.warning("advisor analytics summary unavailable error_type=%s", type(exc).__name__)
            raise HTTPException(503, "Advisor analytics storage unavailable") from None
