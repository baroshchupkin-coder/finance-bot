import unittest
from datetime import date, datetime
from decimal import Decimal

from taxi_reimbursements import (
    format_taxi_amount,
    format_taxi_period,
    group_taxi_entries,
    is_taxi_summary_time,
    parse_taxi_amount,
    taxi_period_for_run_date,
    taxi_summary_key,
)


class TaxiPeriodTests(unittest.TestCase):
    def test_fifth_uses_previous_month_twentieth_through_current_fourth(self):
        start, end = taxi_period_for_run_date(date(2026, 8, 5))
        self.assertEqual(start, date(2026, 7, 20))
        self.assertEqual(end, date(2026, 8, 5))
        self.assertEqual(format_taxi_period(start, end), "20.07.2026-04.08.2026")

    def test_twentieth_uses_current_fifth_through_nineteenth(self):
        start, end = taxi_period_for_run_date(date(2026, 8, 20))
        self.assertEqual(start, date(2026, 8, 5))
        self.assertEqual(end, date(2026, 8, 20))
        self.assertEqual(format_taxi_period(start, end), "05.08.2026-19.08.2026")

    def test_fifth_handles_year_boundary(self):
        start, end = taxi_period_for_run_date(date(2027, 1, 5))
        self.assertEqual(start, date(2026, 12, 20))
        self.assertEqual(end, date(2027, 1, 5))

    def test_other_days_have_no_period(self):
        self.assertIsNone(taxi_period_for_run_date(date(2026, 8, 6)))

    def test_dispatch_starts_at_configured_time(self):
        self.assertFalse(is_taxi_summary_time(datetime(2026, 8, 5, 9, 59)))
        self.assertTrue(is_taxi_summary_time(datetime(2026, 8, 5, 10, 0)))
        self.assertFalse(is_taxi_summary_time(datetime(2026, 8, 6, 10, 0)))

    def test_summary_key_is_stable_for_project_spacing_and_case(self):
        first = taxi_summary_key(date(2026, 7, 20), date(2026, 8, 5), " ВЛ ", "123")
        second = taxi_summary_key(date(2026, 7, 20), date(2026, 8, 5), "вл", "123")
        self.assertEqual(first, second)


class TaxiAmountTests(unittest.TestCase):
    def test_parses_common_taxi_amount_formats(self):
        cases = {
            "500 сом": Decimal("500"),
            "1 000": Decimal("1000"),
            "1.000 сом": Decimal("1000"),
            "1,000 сом": Decimal("1000"),
            "1 250,50 сом": Decimal("1250.50"),
            "1.250,50 сом": Decimal("1250.50"),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_taxi_amount(value), expected)

    def test_rejects_missing_or_non_positive_amounts(self):
        for value in ("", "нет суммы", "0 сом", "500 + 200"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_taxi_amount(value)

    def test_formats_total(self):
        self.assertEqual(format_taxi_amount(Decimal("6000")), "6 000")
        self.assertEqual(format_taxi_amount(Decimal("1250.5")), "1 250,5")




class TaxiGroupingTests(unittest.TestCase):
    def test_groups_by_creator_and_project_and_sums_amounts(self):
        groups = group_taxi_entries([
            {
                "project": "ВЛ",
                "creator_chat_id": "123",
                "creator_username": "manager",
                "creator_name": "Manager",
                "request_id": "1",
                "sheet_row_number": 2,
                "amount": "1 000 сом",
            },
            {
                "project": "вл",
                "creator_chat_id": "123",
                "creator_username": "manager",
                "creator_name": "Manager",
                "request_id": "2",
                "sheet_row_number": 3,
                "amount": "2.000 сом",
            },
            {
                "project": "ОР",
                "creator_chat_id": "123",
                "creator_username": "manager",
                "creator_name": "Manager",
                "request_id": "3",
                "sheet_row_number": 4,
                "amount": "500 сом",
            },
        ])

        self.assertEqual(len(groups), 2)
        vl_group = groups[("вл", "123")]
        self.assertEqual(vl_group["total"], Decimal("3000"))
        self.assertEqual(vl_group["source_request_ids"], ["1", "2"])

    def test_invalid_amount_blocks_only_its_group(self):
        groups = group_taxi_entries([
            {
                "project": "ВЛ",
                "creator_chat_id": "123",
                "request_id": "1",
                "sheet_row_number": 2,
                "amount": "неизвестно",
            }
        ])

        group = groups[("вл", "123")]
        self.assertIsNone(group["total"])
        self.assertEqual(group["invalid_amounts"], [(2, "1", "неизвестно")])

if __name__ == "__main__":
    unittest.main()
