import unittest
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from payroll_reports import (
    PROJECT_OR,
    PROJECT_VL,
    build_payroll_report,
    collect_sent_report_keys,
    due_dates_for_payroll_report,
    format_payroll_report,
    normalize_project_group,
    next_report_log_row,
    parse_report_amount,
    payroll_report_date,
)


class PayrollReportScheduleTests(unittest.TestCase):
    def test_report_is_two_working_days_before_due_date(self):
        expected = {
            date(2026, 9, 25): date(2026, 9, 23),  # Friday -> Wednesday
            date(2026, 7, 25): date(2026, 7, 23),  # Saturday -> Thursday
            date(2026, 10, 25): date(2026, 10, 22),  # Sunday -> Thursday
            date(2026, 8, 10): date(2026, 8, 6),  # Monday -> Thursday
        }
        for due_date, report_date in expected.items():
            with self.subTest(due_date=due_date):
                self.assertEqual(payroll_report_date(due_date), report_date)

    def test_report_becomes_due_at_noon_and_can_catch_up(self):
        tz = ZoneInfo("Asia/Novosibirsk")
        self.assertNotIn(
            date(2026, 9, 25),
            due_dates_for_payroll_report(datetime(2026, 9, 23, 11, 59, tzinfo=tz)),
        )
        self.assertIn(
            date(2026, 9, 25),
            due_dates_for_payroll_report(datetime(2026, 9, 23, 12, 0, tzinfo=tz)),
        )
        self.assertIn(
            date(2026, 9, 25),
            due_dates_for_payroll_report(datetime(2026, 9, 24, 9, 0, tzinfo=tz)),
        )


class PayrollAmountTests(unittest.TestCase):
    def test_recognizes_kgs_spellings_and_separators(self):
        cases = {
            "54 250 сомов": Decimal("54250"),
            "54 250 s": Decimal("54250"),
            "14.750,5 KGS": Decimal("14750.5"),
            "сом 10 000": Decimal("10000"),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                parsed = parse_report_amount(value)
                self.assertEqual(parsed.currency, "KGS")
                self.assertEqual(parsed.amount, expected)

    def test_recognizes_usd_usdt_and_cents(self):
        cases = {
            "115 000,15 долларов": Decimal("115000.15"),
            "$14.750,5": Decimal("14750.5"),
            "1,250.50 USDT": Decimal("1250.50"),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                parsed = parse_report_amount(value)
                self.assertEqual(parsed.currency, "USD")
                self.assertEqual(parsed.amount, expected)

    def test_currency_free_or_unsupported_amount_is_unrecognized(self):
        self.assertIsNone(parse_report_amount("150 000"))
        self.assertIsNone(parse_report_amount("15 000 руб"))
        self.assertIsNone(parse_report_amount("примерно 15 000 сом"))


class PayrollReportBuildingTests(unittest.TestCase):
    def test_reads_current_and_legacy_misplaced_report_keys(self):
        rows = [
            ["report_key"],
            ["payroll-report|2026-09-25|VL"],
            ["", "", "", "", "", "", "", "payroll-report|2026-09-25|OR"],
        ]
        self.assertEqual(
            collect_sent_report_keys(rows),
            {
                "payroll-report|2026-09-25|VL",
                "payroll-report|2026-09-25|OR",
            },
        )

    def test_next_log_row_uses_column_a_only(self):
        rows = [
            ["report_key"],
            ["first", "", "", "", "", "", "", "recipient"],
            ["", "", "", "", "", "", "", "legacy misplaced log"],
        ]
        self.assertEqual(next_report_log_row(rows), 3)

    def test_project_aliases(self):
        for value in ("VL", "ВЛ"):
            self.assertEqual(normalize_project_group(value), PROJECT_VL)
        for value in ("OR", "ОР", "OR KG", "ОР КГ", "ORKG", "ОРКГ"):
            self.assertEqual(normalize_project_group(value), PROJECT_OR)

    def test_totals_and_unrecognized_payee(self):
        due_date = date(2026, 9, 25)
        invoices = [
            {"project": "ОР", "due_date": due_date, "payee": "Булат", "amount": "175 000 сом"},
            {"project": "OR KG", "due_date": due_date, "payee": "Сервис", "amount": "500 USDT"},
            {"project": "OR", "due_date": due_date, "payee": "Иван", "amount": "150 000"},
            {"project": "VL", "due_date": due_date, "payee": "Другой", "amount": "999 сом"},
        ]
        report = build_payroll_report(invoices, PROJECT_OR, due_date)
        self.assertEqual(report.kgs_total, Decimal("175000"))
        self.assertEqual(report.usd_total, Decimal("500"))
        self.assertEqual(report.unrecognized, (("Иван", "150 000"),))
        self.assertIn("В сомах: 175 000 сом", format_payroll_report(report))
        self.assertIn("Иван — 150 000", format_payroll_report(report))


if __name__ == "__main__":
    unittest.main()
