"""Idempotent charge and refund reconciliation."""

from collections.abc import Iterable


class LedgerError(ValueError):
    pass


def reconcile(events: Iterable[dict]) -> dict[str, int]:
    """Return account balances after applying valid chronological events."""
    raise NotImplementedError
