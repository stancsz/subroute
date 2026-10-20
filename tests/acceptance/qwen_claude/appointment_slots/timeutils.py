"""Strict local wall-time conversion for appointment scheduling."""

from datetime import datetime


class ScheduleError(ValueError):
    pass


def local_to_utc(value: str, zone: str) -> datetime:
    """Convert an unambiguous existing local minute to an aware UTC datetime."""
    raise NotImplementedError
