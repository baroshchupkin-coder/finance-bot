import ast
import asyncio
import logging
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from callback_safety import CallbackDebouncer


def load_button_namespace(sheet, events):
    source = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
    button_node = next(
        node for node in source.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "button"
    )
    namespace = {
        "asyncio": asyncio,
        "logging": logging,
        "Update": object,
        "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
        "BadRequest": RuntimeError,
        "sheet": sheet,
        "callback_debouncer": CallbackDebouncer(15),
        "answer_callback_safely": AsyncMock(
            side_effect=lambda *args, **kwargs: events.append("answer") or True
        ),
        "remove_stale_callback_keyboard": AsyncMock(),
        "restore_approval_keyboard_after_error": AsyncMock(),
        "callback_matches_message": lambda *args: True,
        "get_cell": lambda row, column, default="": (
            row[column] if column < len(row) and row[column] != "" else default
        ),
        "set_cell": lambda row, column, value: row.__setitem__(column, value),
        "STATUS_COL": 7,
        "REQUEST_ID_COL": 0,
        "APPROVED_AT_COL": 14,
        "APPROVER_NAME_COL": 12,
        "STATUS_PENDING_APPROVAL": "На согласовании",
        "STATUS_APPROVED": "Согласован",
        "REMINDER_TZ": timezone.utc,
        "datetime": datetime,
        "build_approval_keyboard": lambda request_id: f"keyboard:{request_id}",
        "build_approved_approval_text": lambda row: "approved",
        "edit_invoice_message": AsyncMock(
            side_effect=lambda *args, **kwargs: events.append("edit")
        ),
        "notify_creator_invoice_approved": AsyncMock(),
        "send_due_payment_invoice": AsyncMock(),
        "user_state": {},
        "EXPENSE_CATEGORY_BY_KEY": {},
        "reject_state": {},
        "payment_state": {},
    }
    exec(compile(ast.Module(body=[button_node], type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace


class FakeSheet:
    def __init__(self, row, events, fail_read=False, fail_write=False):
        self.row = row
        self.events = events
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.reads = 0
        self.batch_updates = []

    def get_all_values(self):
        self.events.append("read")
        self.reads += 1
        if self.fail_read:
            raise TimeoutError("read failed")
        return [["header"], self.row]

    def batch_update(self, updates):
        self.events.append("write")
        if self.fail_write:
            raise TimeoutError("write failed")
        self.batch_updates.append(updates)


class FakeQuery:
    def __init__(self, events):
        self.data = "approve_696"
        self.id = "callback"
        self.from_user = SimpleNamespace(username="approver", first_name="Approver")
        self.message = SimpleNamespace(
            chat_id=-1001,
            message_id=500,
            reply_markup=object(),
            reply_text=AsyncMock(side_effect=AssertionError("No public service message")),
        )
        self.events = events

    async def edit_message_reply_markup(self, reply_markup=None):
        self.events.append("hide" if reply_markup is None else "restore")


class TelegramApprovalCallbackTests(unittest.IsolatedAsyncioTestCase):
    def make_row(self, status="На согласовании"):
        row = [""] * 27
        row[0] = "696"
        row[7] = status
        return row

    async def test_approval_hides_keyboard_before_sheet_and_batches_updates(self):
        events = []
        sheet = FakeSheet(self.make_row(), events)
        namespace = load_button_namespace(sheet, events)
        query = FakeQuery(events)

        await namespace["button"](
            SimpleNamespace(callback_query=query),
            SimpleNamespace(bot=object()),
        )

        self.assertLess(events.index("answer"), events.index("hide"))
        self.assertLess(events.index("hide"), events.index("read"))
        self.assertLess(events.index("read"), events.index("write"))
        self.assertEqual(len(sheet.batch_updates), 1)
        self.assertEqual(len(sheet.batch_updates[0]), 3)
        self.assertEqual(sheet.row[7], "Согласован")

    async def test_immediate_double_click_does_not_read_sheet_twice(self):
        events = []
        sheet = FakeSheet(self.make_row(), events)
        namespace = load_button_namespace(sheet, events)

        await namespace["button"](
            SimpleNamespace(callback_query=FakeQuery(events)),
            SimpleNamespace(bot=object()),
        )
        await namespace["button"](
            SimpleNamespace(callback_query=FakeQuery(events)),
            SimpleNamespace(bot=object()),
        )

        self.assertEqual(sheet.reads, 1)
        self.assertEqual(len(sheet.batch_updates), 1)

    async def test_competing_approval_actions_do_not_read_sheet_twice(self):
        events = []
        sheet = FakeSheet(self.make_row(), events)
        namespace = load_button_namespace(sheet, events)
        approve_query = FakeQuery(events)
        reject_query = FakeQuery(events)
        reject_query.data = "reject_696"

        await namespace["button"](
            SimpleNamespace(callback_query=approve_query),
            SimpleNamespace(bot=object()),
        )
        await namespace["button"](
            SimpleNamespace(callback_query=reject_query),
            SimpleNamespace(bot=object()),
        )

        self.assertEqual(sheet.reads, 1)
        self.assertEqual(len(sheet.batch_updates), 1)
        reject_query.message.reply_text.assert_not_awaited()

    async def test_stale_click_does_not_post_message_to_group(self):
        events = []
        sheet = FakeSheet(self.make_row(status="Согласован"), events)
        namespace = load_button_namespace(sheet, events)
        query = FakeQuery(events)

        await namespace["button"](
            SimpleNamespace(callback_query=query),
            SimpleNamespace(bot=object()),
        )

        query.message.reply_text.assert_not_awaited()
        namespace["remove_stale_callback_keyboard"].assert_awaited_once()

    async def test_failed_write_restores_keyboard_and_allows_retry(self):
        events = []
        sheet = FakeSheet(self.make_row(), events, fail_write=True)
        namespace = load_button_namespace(sheet, events)
        query = FakeQuery(events)
        namespace["restore_approval_keyboard_after_error"] = AsyncMock()

        with self.assertRaises(TimeoutError):
            await namespace["button"](
                SimpleNamespace(callback_query=query),
                SimpleNamespace(bot=object()),
            )

        namespace["restore_approval_keyboard_after_error"].assert_awaited_once()
        self.assertTrue(namespace["callback_debouncer"].claim("approval", "696", now=10**12))


if __name__ == "__main__":
    unittest.main()
