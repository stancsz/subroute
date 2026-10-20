import copy
import unittest

from availability import available_starts
from timeutils import ScheduleError, local_to_utc


class AppointmentTests(unittest.TestCase):
    def test_summer_workday_and_busy_boundary(self):
        busy = [('2026-07-15T16:00:00Z', '2026-07-15T17:00:00Z')]
        before = copy.deepcopy(busy)
        self.assertEqual(available_starts('2026-07-15', 'America/Denver', '09:00', '12:00', busy, 60),
                         ['2026-07-15T15:00Z', '2026-07-15T17:00Z'])
        self.assertEqual(busy, before)

    def test_spring_forward_uses_elapsed_time(self):
        self.assertEqual(available_starts('2026-03-08', 'America/Denver', '01:30', '03:30', [], 30),
                         ['2026-03-08T08:30Z', '2026-03-08T09:00Z'])

    def test_rejects_nonexistent_or_ambiguous_wall_time(self):
        with self.assertRaises(ScheduleError):
            local_to_utc('2026-03-08T02:30', 'America/Denver')
        with self.assertRaises(ScheduleError):
            local_to_utc('2026-11-01T01:30', 'America/Denver')

    def test_bad_busy_interval_and_duration(self):
        with self.assertRaises(ScheduleError):
            available_starts('2026-07-15', 'America/Denver', '09:00', '12:00',
                             [('2026-07-15T10:00', '2026-07-15T11:00')], 30)
        with self.assertRaises(ScheduleError):
            available_starts('2026-07-15', 'America/Denver', '09:00', '12:00', [], True)


if __name__ == '__main__':
    unittest.main()
