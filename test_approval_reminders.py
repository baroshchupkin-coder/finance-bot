import ast
import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


NOVOSIBIRSK = timezone(timedelta(hours=7))


class ReminderSheet:
    def __init__(self, row, events):
        self.row = row
        self.events = events
        self.fail_claim = False
        self.save_failure = ""

    def row_values(self, row_number):
        return list(self.row)

    def update_cell(self, row_number, column, value):
        if self.fail_claim:
            raise TimeoutError("Could not reserve reminder")
        self.events.append("claim")
        self.row[column - 1] = value

    def batch_update(self, updates):
        self.events.append("save")
        if self.save_failure == "before_commit":
            raise TimeoutError("Could not save message IDs")
        self.row[20:22] = updates[0]["values"][0]
        if self.save_failure == "after_commit":
            raise TimeoutError("Lost save response")


def load_reminder_namespace(sheet, send):
    tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
    functions = {
        "get_cell", "parse_int", "parse_iso_datetime", "format_approval_reminder_timestamp",
        "get_approval_last_sent_at", "is_approval_reminder_due", "resend_pending_approval_invoice",
    }
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in functions]
    namespace = {
        "asyncio": asyncio, "logging": logging, "datetime": datetime,
        "sheet": sheet, "send_pending_approval_invoice": send, "approval_resend_claims": set(),
        "REQUEST_ID_COL": 0, "STATUS_COL": 7, "APPROVER_CHAT_ID_COL": 8,
        "LAST_INVOICE_MESSAGE_CHAT_ID_COL": 20, "LAST_INVOICE_MESSAGE_ID_COL": 21,
        "APPROVAL_LAST_SENT_AT_COL": 26, "STATUS_PENDING_APPROVAL": "На согласовании",
        "APPROVAL_REMINDER_TIMESTAMP_PREFIX": "approval-reminder:",
        "APPROVAL_REMINDER_TZ": NOVOSIBIRSK, "APPROVAL_REMINDER_HOUR": 11,
        "APPROVAL_REMINDER_MINUTE": 0,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


class ApprovalReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 11, 1, tzinfo=NOVOSIBIRSK)
        self.row = [""] * 27
        self.row[0], self.row[7], self.row[8] = "992", "На согласовании", "-1001"
        self.row[20], self.row[21] = "-1001", "100"
        self.row[26] = "approval-reminder:2026-11-09T15:42:48+07:00"
        self.events = []
        self.sheet = ReminderSheet(list(self.row), self.events)
        self.message = SimpleNamespace(chat_id=-1001, message_id=101)
        self.send = AsyncMock(side_effect=self.send_message)
        self.bot = SimpleNamespace(delete_message=AsyncMock(side_effect=self.delete_message))

    def send_message(self, *args):
        self.events.append("send")
        return self.message

    def delete_message(self, **kwargs):
        self.events.append(f"delete:{kwargs['message_id']}")

    async def resend(self, now=None):
        namespace = load_reminder_namespace(self.sheet, self.send)
        return await namespace["resend_pending_approval_invoice"](self.bot, 993, self.row, now or self.now)

    async def test_claim_precedes_send_and_replaces_old_message_after_saving(self):
        self.assertIs(await self.resend(), self.message)
        self.assertEqual(self.events, ["claim", "send", "save", "delete:100"])
        self.assertEqual(self.sheet.row[20:22], ["-1001", "101"])
        self.assertIsNone(await self.resend(self.now + timedelta(minutes=5)))
        self.send.assert_awaited_once()

    async def test_sheet_failure_after_send_keeps_original_and_does_not_resend_same_day(self):
        self.sheet.save_failure = "before_commit"
        with self.assertRaises(TimeoutError):
            await self.resend()
        self.assertEqual(self.events, ["claim", "send", "save", "delete:101"])
        self.assertEqual(self.sheet.row[20:22], ["-1001", "100"])
        self.assertIsNone(await self.resend(self.now + timedelta(minutes=5)))
        self.send.assert_awaited_once()

    async def test_lost_save_response_does_not_delete_the_current_message(self):
        self.sheet.save_failure = "after_commit"
        self.assertIs(await self.resend(), self.message)
        self.bot.delete_message.assert_awaited_once_with(chat_id=-1001, message_id=100)

    async def test_failed_claim_does_not_send_a_message(self):
        self.sheet.fail_claim = True
        with self.assertRaises(TimeoutError):
            await self.resend()
        self.send.assert_not_awaited()
        self.bot.delete_message.assert_not_awaited()

    async def test_approved_request_is_skipped_even_when_scheduler_snapshot_is_pending(self):
        self.sheet.row[7] = "Согласован"
        self.assertIsNone(await self.resend())
        self.assertEqual(self.events, [])

    async def test_legacy_and_same_day_requests_remain_ineligible(self):
        for marker in ("2026-11-09T15:42:48+07:00", "approval-reminder:2026-11-10T09:00:00+07:00"):
            self.row[26] = self.sheet.row[26] = marker
            self.assertIsNone(await self.resend())
        self.send.assert_not_awaited()
