"""Search bookable appointment start times."""

from timeutils import ScheduleError, local_to_utc


def available_starts(
    day: str, zone: str, work_start: str, work_end: str,
    busy: list[tuple[str, str]], duration_minutes: int, step_minutes: int = 30,
) -> list[str]:
    """Return valid UTC appointment starts. See README.md."""
    raise NotImplementedError
