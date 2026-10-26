import unittest
from datetime import datetime

from sleep_assistant import schedule_near
from sleep_metrics import summarize


class ScheduleTests(unittest.TestCase):
    def test_evening_schedule_crosses_midnight(self):
        bed, wake = schedule_near(datetime(2026, 10, 26, 22, 30), "23:00", "07:00", 60)
        self.assertEqual(bed, datetime(2026, 10, 26, 23, 0))
        self.assertEqual(wake, datetime(2026, 10, 27, 7, 0))

    def test_after_midnight_uses_previous_bedtime(self):
        bed, wake = schedule_near(datetime(2026, 10, 27, 1, 0), "23:00", "07:00", 60)
        self.assertEqual(bed, datetime(2026, 10, 26, 23, 0))
        self.assertEqual(wake, datetime(2026, 10, 27, 7, 0))


class SummaryTests(unittest.TestCase):
    def test_derived_sleep_metrics(self):
        entries = [
            {
                "t": "2026-10-26T22:00:00",
                "type": "session_start",
                "target_bedtime": "2026-10-26T23:00:00",
            },
            {
                "t": "2026-10-26T22:45:00",
                "type": "sample",
                "person_visible": True,
                "phone_visible": True,
                "lying_down": True,
                "pose_motion": None,
            },
            {
                "t": "2026-10-26T22:45:30",
                "type": "sample",
                "person_visible": True,
                "phone_visible": False,
                "lying_down": True,
                "pose_motion": 0.03,
            },
            {
                "t": "2026-10-26T22:50:30",
                "type": "sample",
                "person_visible": True,
                "phone_visible": False,
                "lying_down": True,
                "pose_motion": 0.02,
            },
            {
                "t": "2026-10-26T23:00:30",
                "type": "sample",
                "person_visible": True,
                "phone_visible": False,
                "lying_down": True,
                "pose_motion": 0.5,
            },
        ]
        result = summarize(entries)
        self.assertEqual(result.bedtime, "2026-10-26T22:45:00")
        self.assertTrue(result.on_time)
        self.assertEqual(result.phone_put_away, "2026-10-26T22:45:30")
        self.assertEqual(result.movement_events, 1)
        self.assertEqual(result.longest_still_seconds, 300)


if __name__ == "__main__":
    unittest.main()
