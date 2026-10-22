import ast
import logging
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import dds_integration


def load_handler_namespace(writer, event_time):
    source = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
    selected = ast.Module(body=[
        node for node in source.body
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"write_dds_payment_parts", "handle_dds_standalone_message"}
        ) or (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "DDS_WALLETS_BY_USERNAME"
                    for target in node.targets)
        )
    ], type_ignores=[])
    namespace = dict(vars(dds_integration))
    namespace.update({
        "logging": logging,
        "Update": object,
        "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
        "write_dds_candidate": writer,
        "get_dds_event_time": lambda message: event_time,
        "dds_event_is_in_scope": lambda chat_id, event_time: (
            chat_id in dds_integration.DDS_CHAT_IDS or chat_id == -1003964698486
        ),
        "DDS_DEFAULT_CURRENCY_BY_CHAT": {
            **{chat_id: "KGS" for chat_id in dds_integration.DDS_CHAT_IDS},
            -1003964698486: "USD",
        },
        "CPP_DDS_CHAT_IDS": frozenset({-1003964698486}),
        "CPP_DDS_FIXED_WALLET": "Егор",
        "CPP_DDS_TRANSFER_WALLET_ALIASES": {},
        "CPP_DDS_SOURCE_WALLET_ALIASES": {},
        "DDS_OCR_ENABLED": False,
        "dds_linked_receipt_events": set(),
    })
    exec(compile(selected, "bot.py", "exec"), namespace)
    return namespace


class PaymentPartsHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_schedules_two_parts_without_ocr_or_foreground_write(self):
        writer = AsyncMock()
        event_time = datetime(2026, 8, 29, tzinfo=timezone.utc)
        namespace = load_handler_namespace(writer, event_time)
        jobs = []
        context = SimpleNamespace(application=SimpleNamespace(
            create_task=lambda coro, update: jobs.append(coro),
        ))
        update = SimpleNamespace(
            effective_message=SimpleNamespace(
                sender_chat=None, message_id=999,
                text="Ютуб 120к продюсер 180к съемки", caption=None,
                document=None, photo=None,
            ),
            effective_user=SimpleNamespace(id=7, username="payer", is_bot=False),
            effective_chat=SimpleNamespace(id=-1003806940668, username=None),
        )
        await namespace["handle_dds_standalone_message"](update, context)
        writer.assert_not_awaited()
        self.assertEqual(len(jobs), 1)
        await jobs[0]
        self.assertEqual(writer.await_count, 2)
        first, second = writer.await_args_list
        self.assertEqual(first.args[0].amount, -120000)
        self.assertEqual(second.args[0].amount, -180000)
        self.assertEqual(second.args[1], "message:-1003806940668:999:part:2")
        for call in (first, second):
            self.assertIn("https://t.me/c/3806940668/999", call.args[0].description)
            self.assertEqual(call.args[-2:], (7, "payer"))

    async def test_topup_applies_to_both_or_chats_only(self):
        for chat_id, expected_count in (
            (-1003806940668, 2), (-1003764038215, 2),
            (-1003964698486, 1), (-1001234567890, 0),
        ):
            with self.subTest(chat_id=chat_id):
                writer = AsyncMock(return_value={"status": "written"})
                event_time = datetime(2026, 10, 22, tzinfo=timezone.utc)
                namespace = load_handler_namespace(writer, event_time)
                jobs = []
                update = SimpleNamespace(
                    effective_message=SimpleNamespace(
                        sender_chat=None, message_id=123,
                        text="50 USD — пополнение в подотчет", caption=None,
                        document=None, photo=None,
                    ),
                    effective_user=SimpleNamespace(id=38038661, username="bulat_sufyanov", is_bot=False),
                    effective_chat=SimpleNamespace(id=chat_id, username=None),
                )
                context = SimpleNamespace(application=SimpleNamespace(
                    create_task=lambda coro, update: jobs.append(coro),
                ))
                await namespace["handle_dds_standalone_message"](update, context)
                writer.assert_not_awaited()
                for job in jobs:
                    await job
                self.assertEqual(writer.await_count, expected_count)
                if expected_count == 2:
                    expense, income = writer.await_args_list
                    self.assertEqual(expense.args[0].wallet, "Александр $")
                    self.assertEqual(expense.args[0].amount, -50)
                    self.assertEqual(income.args[0].amount, 50)
                    self.assertEqual(income.args[1], f"message:{chat_id}:123:part:2")
                    self.assertEqual(expense.args[0].description, income.args[0].description)
                    self.assertIn(f"https://t.me/c/{str(chat_id)[4:]}/123", expense.args[0].description)
                    self.assertEqual(expense.args[2:], income.args[2:])

    async def test_topup_does_not_write_income_if_expense_failed(self):
        writer = AsyncMock(return_value=None)
        event_time = datetime(2026, 10, 22, tzinfo=timezone.utc)
        namespace = load_handler_namespace(writer, event_time)
        candidates = dds_integration.parse_accountable_topup(
            "50 000 сом — пополнение в подотчет", {"KGS": "Александр KGS"},
        ).candidates
        await namespace["write_dds_payment_parts"](
            candidates, "https://t.me/c/3764038215/123", event_time,
            -1003764038215, 123, 1525565778, "KirillVorontcov", stop_on_failure=True,
        )
        writer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
