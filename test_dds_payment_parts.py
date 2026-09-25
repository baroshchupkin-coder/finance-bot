import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import dds_integration


class PaymentPartsHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_schedules_two_parts_without_ocr_or_foreground_write(self):
        # Load only the handler functions, avoiding bot.py's live startup effects.
        source = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
        selected = ast.Module(body=[
            node for node in source.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"write_dds_payment_parts", "handle_dds_standalone_message"}
        ], type_ignores=[])
        writer = AsyncMock()
        event_time = datetime(2026, 8, 29, tzinfo=timezone.utc)
        namespace = dict(vars(dds_integration))
        namespace.update({
            "Update": object,
            "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
            "write_dds_candidate": writer,
            "get_dds_event_time": lambda message: event_time,
            "dds_event_is_in_scope": lambda chat_id, event_time: True,
            "DDS_DEFAULT_CURRENCY_BY_CHAT": {-1003806940668: "KGS"},
            "CPP_DDS_CHAT_IDS": frozenset(),
            "CPP_DDS_FIXED_WALLET": "Егор",
            "CPP_DDS_TRANSFER_WALLET_ALIASES": {},
            "CPP_DDS_SOURCE_WALLET_ALIASES": {},
            "dds_linked_receipt_events": set(),
        })
        exec(compile(selected, "bot.py", "exec"), namespace)
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


if __name__ == "__main__":
    unittest.main()
