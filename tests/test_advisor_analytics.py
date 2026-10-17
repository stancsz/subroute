import asyncio
import logging

import pytest
from fastapi import HTTPException

from subroute import advisor_analytics as analytics


class Store:
    def __init__(self, row=None):
        self.calls = []
        self.row = row or {"total": 5, "eligible": 4, "selected": 3, "invoked": 2,
                           "successful": 1, "failed": 1, "provider_attempts": 3,
                           "coverage_start_ms": 1_000}

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        await asyncio.sleep(0)

    async def query(self, sql, *args):
        self.calls.append((sql, args))
        return [self.row]


def test_schema_is_lazy_singleflight_and_ids_are_parameterized():
    store = Store()
    service = analytics.AdvisorAnalytics(store)
    assert store.calls == []
    async def scenario():
        assert all(await asyncio.gather(*(service.record("request-'-%s" % i) for i in range(12))))
    asyncio.run(scenario())
    assert sum(sql == analytics.SCHEMA for sql, _ in store.calls) == 1
    assert sum(sql.startswith("CREATE INDEX") for sql, _ in store.calls) == 1
    inserts = [(sql, args) for sql, args in store.calls if sql == analytics.UPSERT]
    assert len(inserts) == 12
    assert all(args[0] not in sql for sql, args in inserts)
    assert all(args[2:] == (False, False, False, False, False, 0) for _, args in inserts)


def test_markers_are_monotone_and_attempt_counts_are_absolute():
    store = Store()
    service = analytics.AdvisorAnalytics(store)
    async def scenario():
        assert await service.record("same", successful=True)
        assert await service.record("same", provider_attempts=2, failed=True)
        assert await service.record("same")
    asyncio.run(scenario())
    entries = [args for sql, args in store.calls if sql == analytics.UPSERT]
    assert entries[0][2:] == (True, True, True, True, False, 1)
    assert entries[1][2:] == (True, True, True, False, True, 2)
    assert entries[2][2:] == (False, False, False, False, False, 0)
    # PostgreSQL preserves the initial timestamp, ORs booleans, and takes an
    # absolute maximum, so duplicate hooks never add another provider attempt.
    assert "started_at=" not in analytics.UPSERT
    assert "GREATEST(" in analytics.UPSERT
    for field in ("eligible", "selected", "invoked", "successful", "failed"):
        assert f"{field}=subroute_advisor_requests.{field} OR EXCLUDED.{field}" in analytics.UPSERT


def test_failure_before_provider_does_not_invent_an_invocation():
    store = Store()
    assert asyncio.run(analytics.AdvisorAnalytics(store).record("rank-failed", eligible=True, failed=True))
    args = store.calls[-1][1]
    assert args[2:] == (True, False, False, False, True, 0)


@pytest.mark.parametrize("minutes", [30, 60, 120])
def test_window_counts_invoked_requests_separately_from_attempts(monkeypatch, minutes):
    monkeypatch.setattr(analytics.time, "time", lambda: 10_000)
    store = Store()
    result = asyncio.run(analytics.AdvisorAnalytics(store).summary(minutes))
    assert result["invoked"] == 2
    assert result["provider_attempts"] == 3
    assert result["total"] == 5
    assert result["partial_window"] is True
    assert result["coverage_verified_start_ms"] == 10_000_000
    assert result["coverage_start_ms"] == 1_000
    assert store.calls[-1][1] == (10_000_000 - minutes * 60_000, 10_000_000)
    assert "COUNT(*) FILTER (WHERE invoked)" in store.calls[-1][0]


def test_coverage_never_backfills_unknown_prior_requests(monkeypatch):
    monkeypatch.setattr(analytics.time, "time", lambda: 10_000)
    store = Store()
    store.row["coverage_start_ms"] = 9_999_000
    result = asyncio.run(analytics.AdvisorAnalytics(store).summary(30))
    assert result["partial_window"] is True
    assert result["coverage_start_ms"] == 9_999_000
    store.row = {**dict.fromkeys(analytics.COUNT_FIELDS, 0), "coverage_start_ms": None}
    result = asyncio.run(analytics.AdvisorAnalytics(store).summary(30))
    assert result["total"] == 0
    assert result["partial_window"] is True
    assert result["coverage_start_ms"] is None


@pytest.mark.parametrize("minutes", [0, 29, 31, 121, True, "30", None])
def test_unsupported_window_is_rejected_before_database_access(minutes):
    store = Store()
    with pytest.raises(ValueError):
        asyncio.run(analytics.AdvisorAnalytics(store).summary(minutes))
    assert store.calls == []


def test_record_failure_is_open_but_summary_is_503_without_leaking_error(caplog):
    class Broken(Store):
        async def execute(self, *args):
            raise RuntimeError("private connection credentials")
    service = analytics.AdvisorAnalytics(Broken())
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(service.record("private-request-id")) is False
        with pytest.raises(HTTPException) as caught:
            asyncio.run(service.summary(30))
    assert caught.value.status_code == 503
    assert "private" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_recording_error_remains_visible_after_store_recovery():
    store = Store()
    service = analytics.AdvisorAnalytics(store)
    assert asyncio.run(service.record("", selected=True)) is False
    result = asyncio.run(service.summary(30))
    assert result["recording_errors_since_process_start"] == 1
    assert result["counts_may_be_incomplete"] is True


def test_one_second_budget_includes_schema_and_admission(monkeypatch):
    monkeypatch.setattr(analytics, "TIMEOUT_SECONDS", .02)
    class Slow(Store):
        async def execute(self, *args):
            await asyncio.sleep(1)
    async def scenario():
        service = analytics.AdvisorAnalytics(Slow())
        assert await asyncio.wait_for(service.record("one"), .2) is False
        with pytest.raises(HTTPException) as caught:
            await asyncio.wait_for(service.summary(30), .2)
        assert caught.value.status_code == 503
    asyncio.run(scenario())


def test_cancellation_propagates_and_invalidates_coverage(monkeypatch):
    class Cancelled(Store):
        async def execute(self, *args):
            raise asyncio.CancelledError
    service = analytics.AdvisorAnalytics(Cancelled())
    monkeypatch.setattr(analytics.time, "time", lambda: service._verified_since_ms / 1000 + 5)
    previous = service._verified_since_ms
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.record("one"))
    assert service._recording_errors == 1
    assert service._verified_since_ms == previous + 5000


@pytest.mark.parametrize("bad", [[], [{"total": 0}], [None], [{
    "total": 1, "eligible": 0, "selected": 0, "invoked": 1,
    "successful": 0, "failed": 0, "provider_attempts": 1, "coverage_start_ms": 1,
}]])
def test_invalid_database_response_is_503_not_fabricated_zero(bad):
    class Invalid(Store):
        async def query(self, *args):
            return bad
    with pytest.raises(HTTPException) as caught:
        asyncio.run(analytics.AdvisorAnalytics(Invalid()).summary(30))
    assert caught.value.status_code == 503


def test_restart_counts_persisted_history_but_does_not_certify_previous_process(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(analytics.time, "time", lambda: now[0])
    store = Store()
    service = analytics.AdvisorAnalytics(store)
    result = asyncio.run(service.summary(30))
    assert result["total"] == 5  # Restart must not filter persisted rows.
    assert result["coverage_start_ms"] == 1_000
    assert result["verified_since_ms"] == 10_000_000
    assert result["coverage_verified_start_ms"] == 10_000_000
    assert result["partial_window"] is True
    assert result["counts_may_be_incomplete"] is True
    now[0] += 30 * 60
    complete = asyncio.run(service.summary(30))
    assert complete["total"] == 5
    assert complete["partial_window"] is False
    assert complete["counts_may_be_incomplete"] is False
    longer = asyncio.run(service.summary(60))
    assert longer["partial_window"] is True


def test_record_error_resets_verification_and_ages_out_of_requested_window(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(analytics.time, "time", lambda: now[0])
    service = analytics.AdvisorAnalytics(Store())
    now[0] += 31 * 60
    assert asyncio.run(service.summary(30))["partial_window"] is False
    assert asyncio.run(service.record("")) is False
    failure_time = int(now[0] * 1000)
    result = asyncio.run(service.summary(30))
    assert result["recording_errors_since_process_start"] == 1
    assert result["coverage_verified_start_ms"] == failure_time
    assert result["counts_may_be_incomplete"] is True
    now[0] += 30 * 60
    result = asyncio.run(service.summary(30))
    assert result["recording_errors_since_process_start"] == 1
    assert result["partial_window"] is False
    assert result["counts_may_be_incomplete"] is False


def test_clock_rollback_cannot_move_verification_epoch_backwards(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(analytics.time, "time", lambda: now[0])
    service = analytics.AdvisorAnalytics(Store())
    now[0] -= 10
    assert asyncio.run(service.record("")) is False
    assert service._verified_since_ms == 10_000_000
