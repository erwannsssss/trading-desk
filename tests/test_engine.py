import unittest
from datetime import datetime

from desk.trading.engine import r_multiple, time_stop_due, trailed_stop


class EngineTests(unittest.TestCase):
    def test_r_multiple(self):
        self.assertEqual(r_multiple("long", 100, 95, 110), 2)
        self.assertEqual(r_multiple("short", 100, 105, 95), 1)

    def test_no_trail_before_1r(self):
        self.assertIsNone(trailed_stop("long", 100, 95, 95, 104))

    def test_breakeven_at_1r(self):
        self.assertEqual(trailed_stop("long", 100, 95, 95, 105.5), 100)
        self.assertEqual(trailed_stop("short", 100, 105, 105, 94), 100)

    def test_trail_at_2r(self):
        self.assertEqual(trailed_stop("long", 100, 95, 100, 112), 107)
        self.assertEqual(trailed_stop("short", 100, 105, 100, 88), 93)

    def test_stop_never_loosens(self):
        self.assertIsNone(trailed_stop("long", 100, 95, 108, 112))   # trail would be 107 < 108
        self.assertIsNone(trailed_stop("long", 100, 95, 100, 105.2))  # already at breakeven

    def test_time_stops(self):
        opened = "2026-10-15T10:00:00-04:00"
        self.assertFalse(time_stop_due("intraday", opened, datetime.fromisoformat("2026-10-15T15:00:00-04:00")))
        self.assertTrue(time_stop_due("intraday", opened, datetime.fromisoformat("2026-10-15T15:51:00-04:00")))
        self.assertFalse(time_stop_due("1-5 days", opened, datetime.fromisoformat("2026-10-20T10:00:00-04:00")))
        self.assertTrue(time_stop_due("1-5 days", opened, datetime.fromisoformat("2026-10-22T10:00:00-04:00")))


if __name__ == "__main__":
    unittest.main()
