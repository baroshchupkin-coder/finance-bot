import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from payment_schedule import (
    parse_payment_date,
    payment_dispatch_date,
    should_dispatch_payment,
)


class PaymentDateParsingTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 7, 13)

    def test_accepts_full_and_iso_dates(self):
        self.assertEqual(
            parse_payment_date("15.07.2026", self.today),
            date(2026, 7, 15),
        )
        self.assertEqual(
            parse_payment_date("2026-07-15", self.today),
            date(2026, 7, 15),
        )

    def test_short_date_rolls_to_next_year_when_needed(self):
        self.assertEqual(
            parse_payment_date("12.07", self.today),
            date(2027, 7, 12),
        )

    def test_bare_day_uses_next_available_month(self):
        self.assertEqual(parse_payment_date("15", self.today), date(2026, 7, 15))
        self.assertEqual(parse_payment_date("12", self.today), date(2026, 8, 12))

    def test_short_leap_day_uses_next_valid_year(self):
        self.assertEqual(parse_payment_date("29.02", self.today), date(2028, 2, 29))

    def test_rejects_past_full_date(self):
        with self.assertRaises(ValueError):
            parse_payment_date("12.07.2026", self.today)


class PaymentDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Asia/Bishkek")

    def test_weekend_due_dates_move_to_friday(self):
        self.assertEqual(payment_dispatch_date(date(2026, 7, 18)), date(2026, 7, 17))
        self.assertEqual(payment_dispatch_date(date(2026, 7, 19)), date(2026, 7, 17))

    def test_dispatches_at_configured_time(self):
        due = date(2026, 7, 15)
        before = datetime(2026, 7, 15, 9, 59, tzinfo=self.tz)
        at_time = datetime(2026, 7, 15, 10, 0, tzinfo=self.tz)
        self.assertFalse(should_dispatch_payment(due, before))
        self.assertTrue(should_dispatch_payment(due, at_time))

    def test_dispatches_overdue_invoice_as_catch_up(self):
        due = date(2026, 7, 15)
        now = datetime(2026, 7, 16, 8, 0, tzinfo=self.tz)
        self.assertTrue(should_dispatch_payment(due, now))


if __name__ == "__main__":
    unittest.main()
