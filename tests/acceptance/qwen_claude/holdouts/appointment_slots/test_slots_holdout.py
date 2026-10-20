import unittest

from availability import available_starts
from timeutils import ScheduleError, local_to_utc


class Holdout(unittest.TestCase):
    def test_second_timezone_and_offset_busy(self):
        busy = [('2026-12-01T10:30:00+01:00', '2026-12-01T11:00:00+01:00')]
        self.assertEqual(available_starts('2026-12-01', 'Europe/Berlin', '09:00', '12:00', busy, 30),
                         ['2026-12-01T08:00Z', '2026-12-01T08:30Z', '2026-12-01T09:00Z',
                          '2026-12-01T10:00Z', '2026-12-01T10:30Z'])

    def test_fall_back_ambiguous_work_boundary_rejected(self):
        with self.assertRaises(ScheduleError):
            available_starts('2026-11-01', 'America/Denver', '01:00', '03:00', [], 30)

    def test_touching_end_is_allowed_and_bad_zone_rejected(self):
        self.assertEqual(available_starts('2026-07-15', 'America/Denver', '09:00', '10:00', [], 60),
                         ['2026-07-15T15:00Z'])
        with self.assertRaises(ScheduleError):
            local_to_utc('2026-07-15T09:00', 'Mars/Olympus')


if __name__ == '__main__':
    unittest.main()
