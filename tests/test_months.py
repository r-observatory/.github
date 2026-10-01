import datetime as dt
import unittest

from harness import acl, at


class Months(unittest.TestCase):
    def test_months_due_are_closed_months_whose_first_day_is_inside_retention(self):
        self.assertEqual(acl.months_due(at("2026-10-06T03:41:00Z")), ["2026-08", "2026-09"])
        self.assertEqual(acl.months_due(at("2026-09-28T23:59:59Z")), ["2026-07", "2026-08"])
        self.assertEqual(acl.months_due(at("2026-09-29T00:00:00Z")), ["2026-08"])
        self.assertEqual(acl.months_due(at("2027-01-04T03:41:00Z")), ["2026-11", "2026-12"])
        self.assertEqual(acl.months_due(at("2026-10-02T23:59:59Z")), ["2026-08"])

    def test_an_open_or_malformed_month_is_refused(self):
        now = at("2026-10-06T03:41:00Z")
        self.assertEqual(acl.check_months(["2026-09", "2026-07", "2026-09"], now), ["2026-07", "2026-09"])
        for bad in ("2026-10", "2026-11", "2026-13", "2026-7", ""):
            with self.assertRaises(acl.UsageError):
                acl.check_months([bad], now)
        with self.assertRaisesRegex(acl.UsageError, "2026-09 is not closed yet"):
            acl.check_months(["2026-09"], at("2026-10-02T23:59:59Z"))
        self.assertEqual(acl.check_months(["2026-09"], at("2026-10-03T00:00:00Z")), ["2026-09"])

    def test_a_month_is_listed_in_seven_day_slices(self):
        d = dt.date
        self.assertEqual(acl.day_slices("2026-08"), [
            (d(2026, 8, 1), d(2026, 8, 7)), (d(2026, 8, 8), d(2026, 8, 14)),
            (d(2026, 8, 15), d(2026, 8, 21)), (d(2026, 8, 22), d(2026, 8, 28)),
            (d(2026, 8, 29), d(2026, 8, 31))])
        self.assertEqual(len(acl.day_slices("2027-02")), 4)
        self.assertEqual(acl.day_slices("2026-12")[-1], (d(2026, 12, 29), d(2026, 12, 31)))

    def test_errors_stop_holding_a_month_back_from_day_80(self):
        self.assertFalse(acl.upload_anyway("2026-08", at("2026-10-19T23:59:59Z")))
        self.assertTrue(acl.upload_anyway("2026-08", at("2026-10-20T00:00:00Z")))


if __name__ == "__main__":
    unittest.main()
