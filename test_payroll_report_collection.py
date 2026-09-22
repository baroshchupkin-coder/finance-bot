import ast
import asyncio
import logging
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from payroll_reports import (
    REPORT_PROJECTS,
    build_payroll_report,
    format_payroll_report,
    normalize_project_group,
    payroll_report_key,
)


def load_collection_function():
    source = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
    function_node = next(
        node for node in source.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "collect_payroll_report_invoices"
    )
    namespace = {
        "STATUS_COL": 7,
        "STATUS_PENDING_APPROVAL": "На согласовании",
        "STATUS_APPROVED": "Согласован",
        "get_cell": lambda row, index, default="": (
            row[index].strip() if len(row) > index and row[index] else default
        ),
        "get_expense_category": lambda row: row[22] if len(row) > 22 else "Без статьи",
        "get_payment_due_date": lambda row: (
            date.fromisoformat(row[23]) if len(row) > 23 and row[23] else None
        ),
        "normalize_project_group": normalize_project_group,
    }
    exec(
        compile(ast.Module(body=[function_node], type_ignores=[]), "bot.py", "exec"),
        namespace,
    )
    return namespace["collect_payroll_report_invoices"]


def make_row(status, project="VL", category="Команда", due_date="2026-09-25"):
    row = [""] * 24
    row[3] = project
    row[4] = "Получатель"
    row[5] = "100 000 сом"
    row[7] = status
    row[22] = category
    row[23] = due_date
    return row


class PayrollReportCollectionTests(unittest.TestCase):
    def test_includes_pending_and_approved_team_invoices_only(self):
        collect = load_collection_function()
        rows = [
            ["header"],
            make_row("На согласовании"),
            make_row("Согласован", project="OR KG"),
            make_row("Оплачено"),
            make_row("Отклонен"),
            make_row("Согласован", category="Сервисы"),
            make_row("Согласован", project="ЮБ"),
            make_row("Согласован", due_date=""),
        ]

        invoices = collect(rows)

        self.assertEqual(len(invoices), 2)
        self.assertEqual({invoice["project"] for invoice in invoices}, {"VL", "OR KG"})


class FakeReportSheet:
    def __init__(self):
        self.rows = [
            ["report_key", "sent_at", "recipient_id", "project", "payment_date", "message_id", "", "recipient_id"],
            ["", "", "", "", "", "", "", "1493294973"],
        ]

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def append_row(self, row):
        self.rows.append(list(row))


class FakeRequestSheet:
    def get_all_values(self):
        return [["header"]]


class PayrollReportSendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_each_project_once_and_persists_keys(self):
        source = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
        function_node = next(
            node for node in source.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "send_scheduled_payroll_reports"
        )
        report_sheet = FakeReportSheet()
        namespace = {
            "asyncio": asyncio,
            "logging": logging,
            "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
            "datetime": SimpleNamespace(
                now=lambda timezone: __import__("datetime").datetime(2026, 9, 23, 12, 0, tzinfo=timezone)
            ),
            "PAYROLL_REPORT_TZ": __import__("datetime").timezone.utc,
            "PAYROLL_REPORT_HOUR": 12,
            "PAYROLL_REPORT_MINUTE": 0,
            "due_dates_for_payroll_report": lambda *args: (date(2026, 9, 25),),
            "ensure_payroll_report_sheet": lambda: report_sheet,
            "get_cell": lambda row, index, default="": row[index] if len(row) > index and row[index] else default,
            "get_payroll_report_recipient_id": lambda rows: 1493294973,
            "sheet": FakeRequestSheet(),
            "collect_payroll_report_invoices": lambda rows: [],
            "REPORT_PROJECTS": REPORT_PROJECTS,
            "payroll_report_key": payroll_report_key,
            "build_payroll_report": build_payroll_report,
            "format_payroll_report": format_payroll_report,
        }
        exec(
            compile(ast.Module(body=[function_node], type_ignores=[]), "bot.py", "exec"),
            namespace,
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[SimpleNamespace(message_id=101), SimpleNamespace(message_id=102)]
            )
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={}),
            bot=bot,
        )

        await namespace["send_scheduled_payroll_reports"](context)
        await namespace["send_scheduled_payroll_reports"](context)

        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(
            {row[0] for row in report_sheet.rows[2:]},
            {
                "payroll-report|2026-09-25|VL",
                "payroll-report|2026-09-25|OR",
            },
        )


if __name__ == "__main__":
    unittest.main()
