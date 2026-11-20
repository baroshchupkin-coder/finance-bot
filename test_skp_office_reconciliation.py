import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from dds_integration import telegram_message_link
from shared_dds import OFFICE_WALLET, SHARED_DDS_CHAT_ID, SHARED_DDS_OFFICE_CHAT_ID, SHARED_DDS_SPREADSHEET_ID
from skp_ai import DEFAULT_MODEL, Entry
from skp_ai_sheet import SHEETS_EPOCH, SolDdsWriter
from skp_ai_worker import Queue, SolWorker
from skp_office_reconciliation import OFFICE_REPORTER_ID, OfficeAmountEdit, read_office_report


DAY = date(2026, 11, 20)
SERIAL = (DAY - SHEETS_EPOCH).days
CHAT = SHARED_DDS_OFFICE_CHAT_ID
SOURCE = 101
CAPTION = "- 301 сом — покупка бумаги\nОстаток: 47 697,00 сом"
CORRECTED = CAPTION.replace("301", "303")
EDIT = OfficeAmountEdit(DAY, Decimal("-301"), Decimal("-303"))


class ReportParserTests(unittest.TestCase):
    def test_balance_accepts_office_number_formats(self):
        for raw, amount in (
            ("49,697", "49697"),
            ("3.111,74 сом", "3111.74"),
            ("1.949.74 сом", "1949.74"),
            ("3\u202f111,74", "3111.74"),
            ("0.74 сома", "0.74"),
            ("на карте: -15,50 сом", "-15.50"),
            ("— 100", "100"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(read_office_report(f"- 334 сом, доставка, остаток {raw}").balance, Decimal(amount))

    def test_unrelated_amounts_are_not_used_as_the_balance(self):
        for tail in ("600 $", "3-4 сом", "1 200 или 1 300 сом", "30210 после доставки", "100 сом\nостаток: 200"):
            with self.subTest(tail=tail):
                self.assertIsNone(read_office_report(f"- 334 сом\nОстаток: {tail}").balance)

    def test_amount_only_edit_keeps_operation_shape(self):
        original = read_office_report(CAPTION)
        corrected = read_office_report(CORRECTED)
        self.assertEqual((original.amount, corrected.amount), (Decimal("-301"), Decimal("-303")))
        self.assertEqual(original.shape, corrected.shape)
        self.assertNotEqual(original.shape, read_office_report(CORRECTED.replace("бумаги", "посуды")).shape)
        self.assertEqual(
            read_office_report("13.11 - + 1.500 сом - пополнение\nОстаток: 2.216,74 сом").amount,
            Decimal("1500"),
        )


class LedgerBookDouble:
    id = SHARED_DDS_SPREADSHEET_ID

    def __init__(self):
        self.sheet = SimpleNamespace(id=0, row_count=100, get=self.sheet_get)
        self.source_link = telegram_message_link(CHAT, SOURCE)
        self.rows = [
            [11, SERIAL, -2000, OFFICE_WALLET, "", "Ранее"],
            [11, SERIAL, -301, OFFICE_WALLET, "Фирма", f"Бумага\n{self.source_link}"],
            [11, SERIAL, -6000, OFFICE_WALLET, "", "Позже"],
            [11, SERIAL, -100, "Александр С", "", "Другой кошелёк"],
            [10, SERIAL - 31, -100, OFFICE_WALLET, "", "За пределами периода"],
        ]
        self.writes = []
        self.replaced_with_formula = False
        self.fail_after_update = False
        self.balance_missing = False
        self.delay_formula = False
        self.stale_total = None

    def worksheet(self, name):
        assert name == "ДДС: месяц"
        return self.sheet

    def total(self):
        return 50000 + sum(row[2] for row in self.rows if row[0] >= 11 and row[3] == OFFICE_WALLET)

    def values_batch_get(self, ranges, params):
        assert ranges == ["'ДДС: месяц'!G1:P2", "'ДДС: месяц'!C4:H100", "'ДДС: настройки (для ввода сальдо)'!B1"]
        assert params == {"valueRenderOption": "UNFORMATTED_VALUE"}
        total = self.stale_total if self.stale_total is not None else self.total()
        self.stale_total = None
        header = ["Р/c", 100, "", "", OFFICE_WALLET, "" if self.balance_missing else total + 1e-11]
        return {"valueRanges": [{"values": [header]}, {"values": self.rows}, {"values": [[11]]}]}

    def sheet_get(self, range_name, *, value_render_option):
        assert range_name == "D5:H5" and value_render_option == "FORMULA"
        values = self.rows[1][1:]
        if self.replaced_with_formula:
            values[1] = "=-301"
        return [values]

    def batch_update(self, body):
        self.writes.append(body)
        update = body["requests"][0]["updateCells"]
        assert update["start"] == {"sheetId": 0, "rowIndex": 4, "columnIndex": 4}
        assert update["fields"] == "userEnteredValue"
        assert len(update["rows"]) == 1 and len(update["rows"][0]["values"]) == 1
        if self.delay_formula:
            self.stale_total = self.total()
        self.rows[1][2] = update["rows"][0]["values"][0]["userEnteredValue"]["numberValue"]
        if self.fail_after_update:
            self.fail_after_update = False
            raise TimeoutError("write may have succeeded")


class OfficeWriterTests(unittest.TestCase):
    def setUp(self):
        self.book = LedgerBookDouble()
        self.writer = SolDdsWriter(self.book, production=True)

    def test_uses_balance_after_the_linked_row_not_current_balance(self):
        self.assertEqual(self.writer.office_balance_at(CHAT, SOURCE), ("checked", Decimal("47699.00")))

    def test_edits_only_amount_preserving_later_operations_and_labels(self):
        before = [row.copy() for row in self.book.rows]
        self.assertEqual(self.writer.office_balance_at(CHAT, SOURCE, correction=EDIT), ("updated", Decimal("47697.00")))
        before[1][2] = -303
        self.assertEqual(self.book.rows, before)
        self.assertEqual(len(self.book.writes), 1)

    def test_repeated_correction_after_timeout_does_not_write_twice(self):
        self.book.fail_after_update = True
        with self.assertRaises(TimeoutError):
            self.writer.office_balance_at(CHAT, SOURCE, correction=EDIT)
        self.assertEqual(self.writer.office_balance_at(CHAT, SOURCE, correction=EDIT), ("updated", Decimal("47697.00")))
        self.assertEqual(len(self.book.writes), 1)

    def test_does_not_confirm_until_balance_formula_recalculates(self):
        self.book.delay_formula = True
        with self.assertRaisesRegex(RuntimeError, "balance has not settled"):
            self.writer.office_balance_at(CHAT, SOURCE, correction=EDIT)
        self.assertEqual(self.writer.office_balance_at(CHAT, SOURCE, correction=EDIT), ("updated", Decimal("47697.00")))
        self.assertEqual(len(self.book.writes), 1)

    def test_manual_changes_and_formula_are_not_overwritten(self):
        self.book.rows[1][2] = -302
        self.assertEqual(self.writer.office_balance_at(CHAT, SOURCE, correction=EDIT), ("conflict", None))
        self.book.rows[1][2] = -301
        self.book.replaced_with_formula = True
        self.assertEqual(self.writer.office_balance_at(CHAT, SOURCE, correction=EDIT), ("conflict", None))
        self.assertFalse(self.book.writes)

    def test_multirow_operation_is_not_edited(self):
        self.book.rows[2][5] = self.book.source_link
        self.assertEqual(self.writer.office_balance_at(CHAT, SOURCE, correction=EDIT), ("conflict", None))
        self.assertFalse(self.book.writes)

    def test_missing_or_wrong_scope_is_not_a_verified_match(self):
        self.book.balance_missing = True
        self.assertEqual(self.writer.office_balance_at(CHAT, SOURCE), ("checked", None))
        with self.assertRaises(ValueError):
            self.writer.office_balance_at(SHARED_DDS_CHAT_ID, SOURCE, correction=EDIT)


class OfficeWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.queue = Queue(":memory:")
        self.addCleanup(self.queue.db.close)
        self.writer = Mock()
        self.writer.write.return_value = ("written", [210])
        self.writer.office_balance_at.return_value = ("checked", Decimal("47699.00"))
        self.bot = SimpleNamespace(_post=AsyncMock(return_value=True), send_message=AsyncMock())
        self.context = SimpleNamespace(bot=self.bot)
        self.worker = SolWorker(self.queue, self.writer, "unused", DEFAULT_MODEL, CHAT, CHAT)

    async def write(self, text=CAPTION, msg=SOURCE, owner=OFFICE_REPORTER_ID, entries=None, write_note=""):
        if entries is None:
            entries = (Entry(DAY, Decimal("-301"), OFFICE_WALLET, "KGS", "", "Бумага", "", ""),)
        self.queue.enqueue(msg, owner, "office-example", DAY, text)
        self.queue.stage_write(msg, entries, write_note)
        await self.worker.write_staged(msg, entries, self.bot)

    async def edited(self, text=CORRECTED, *, owner=OFFICE_REPORTER_ID, version=501):
        message = SimpleNamespace(
            message_id=SOURCE, text=None, caption=text,
            date=datetime(2026, 11, 20, 7, tzinfo=timezone.utc),
            edit_date=datetime(2026, 11, 20, 8, tzinfo=timezone.utc),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            update_id=version, edited_message=message, effective_message=message,
            effective_chat=SimpleNamespace(id=CHAT), effective_user=SimpleNamespace(id=owner, is_bot=False),
        )
        await self.worker.handle(update, self.context)
        await self.worker.process(self.context)
        return message

    async def test_notifies_difference_then_confirms_amount_edit(self):
        await self.write()
        self.bot._post.assert_not_awaited()
        alert = self.bot.send_message.call_args
        self.assertEqual(alert.kwargs["reply_to_message_id"], SOURCE)
        self.assertIn("не сходится на 2,00 сом", alert.args[1])
        self.writer.office_balance_at.return_value = ("updated", Decimal("47697.00"))
        await self.edited()
        self.writer.office_balance_at.assert_called_with(CHAT, SOURCE, correction=EDIT)
        self.assertEqual(self.queue.office_report(SOURCE)["amount"], "-303")
        self.assertIn("Теперь остаток сходится", self.bot.send_message.call_args.args[1])
        self.bot._post.assert_awaited_once()
        await self.edited()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.writer.office_balance_at.call_count, 2)
        self.writer.write.assert_called_once()

    async def test_exact_match_gets_only_a_reaction(self):
        self.writer.office_balance_at.return_value = ("checked", Decimal("47697.00"))
        await self.write()
        self.bot._post.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_balance_check_preserves_the_models_write_note(self):
        self.writer.office_balance_at.return_value = ("checked", Decimal("47697.00"))
        await self.write(write_note="Другая операция не проводилась.")
        self.assertIn("Другая операция не проводилась.", self.bot.send_message.call_args.args[1])
        self.bot._post.assert_awaited_once()

    async def test_warns_only_when_the_difference_appears_or_changes(self):
        await self.write()
        await self.write(msg=SOURCE + 1)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.writer.office_balance_at.return_value = ("checked", Decimal("47699.01"))
        await self.write(msg=SOURCE + 2)
        self.assertIn("2,01 сом", self.bot.send_message.call_args.args[1])
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_unrelated_field_edit_is_not_applied(self):
        await self.write()
        await self.edited(CORRECTED.replace("бумаги", "мебели"))
        self.writer.office_balance_at.assert_called_once()
        self.assertIn("Строку в ДДС не менял", self.bot.send_message.call_args.args[1])

    async def test_does_not_apply_a_multirow_edit(self):
        entries = (
            Entry(DAY, Decimal("301"), OFFICE_WALLET, "KGS", "", "Перевод", "", ""),
            Entry(DAY, Decimal("-301"), "Александр С", "KGS", "", "Перевод", "", ""),
        )
        await self.write(CAPTION.replace("- 301", "+ 301"), entries=entries)
        await self.edited(CORRECTED.replace("- 303", "+ 303"))
        self.assertIsNone(self.queue.office_report(SOURCE)["day"])
        self.assertIn("Не могу безопасно изменить сумму", self.bot.send_message.call_args.args[1])
        self.writer.office_balance_at.assert_called_once()

    async def test_other_authors_are_not_enrolled_for_edits_or_balance_checks(self):
        await self.write(owner=7)
        self.assertIsNone(self.queue.office_report(SOURCE))
        self.writer.office_balance_at.assert_not_called()
        message = await self.edited(owner=7)
        self.writer.office_balance_at.assert_not_called()
        self.assertNotIn("тестовой", message.reply_text.call_args.args[0])

    async def test_missing_table_balance_is_not_a_verified_match(self):
        self.writer.office_balance_at.return_value = ("checked", None)
        await self.write()
        self.assertIn("Не удалось получить остаток", self.bot.send_message.call_args.args[1])
        self.bot._post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
