# Appointment availability task

Implement timezone-aware appointment search with only the Python standard library. `local_to_utc(value, zone)` converts a local `YYYY-MM-DDTHH:MM` wall time in an IANA zone to an aware UTC `datetime`. Reject nonexistent spring-forward times and ambiguous fall-back times with `ScheduleError`; silently choosing a fold would book the wrong hour. Reject invalid zone and malformed input.

`available_starts(day, zone, work_start, work_end, busy, duration_minutes, step_minutes=30)` returns UTC start timestamps as `YYYY-MM-DDTHH:MMZ`, ordered ascending. `day` is `YYYY-MM-DD`; start/end are local `HH:MM` on that day and must form a valid positive work interval. `busy` contains pairs of offset-aware ISO timestamps (including `Z`) and must contain valid positive intervals. A candidate starts at work_start, then steps forward by real elapsed minutes in UTC, fits entirely in work hours, and does not overlap a busy interval. Touching a busy boundary is allowed. Reject boolean, zero, or negative duration/step. Do not mutate inputs. Use half-open intervals, so `[09:00,10:00)` does not overlap `[10:00,11:00)`.

Edit only `timeutils.py` and `availability.py`. Run `python -m unittest -q` before and after.
