from __future__ import annotations

from datetime import datetime
import unittest

from core.logs import timestamp_log_text


class LogTests(unittest.TestCase):
    def test_every_line_gets_one_full_timestamp(self) -> None:
        now = datetime(2099, 1, 2, 3, 4, 5)
        stamped = timestamp_log_text("first\nsecond", now)
        self.assertEqual(
            stamped,
            "2099/01/02 03:04:05 first\n2099/01/02 03:04:05 second",
        )
        self.assertEqual(timestamp_log_text(stamped, now), stamped)
        self.assertEqual(
            timestamp_log_text("2000/01/01 00:00:00 external", now, preserve_existing=False),
            "2099/01/02 03:04:05 2000/01/01 00:00:00 external",
        )
