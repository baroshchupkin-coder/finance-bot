import ast
import asyncio
import logging
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from invoice_text import (
    TELEGRAM_INVOICE_KEY_PREFIX,
    amount_from_invoice_comment,
    is_invoice_amount,
    parse_invoice_text,
)
from payment_schedule import format_payment_date, parse_payment_date
from taxi_reimbursements import parse_taxi_amount


QUICK_TEXT = (
    "Проект: VL\nСтатья: Сервисы\nКому платим: Иван Петров\n"
    "Сумма: 15 250,50 сом\nДата оплаты: 25.11.2026\n"
    "Комментарий: Подписка на сервис\nРеквизиты: указаны в PDF."
)
OLD_COPY = (
    "Новый счет #892\n\nДата оплаты: 25.11.2026\n\nФб\n\n"
    "Бюджет рекламного кабинета Facebook Ads\n"
    "проект - Орионикс АИ\n550$ - бюджет до 25 ноября\n"
    "пополнить карту ···· 3253\n\nОтправлено: @old_author"
)
COPY_WITH_FIELDS = "Проект: VL\nСтатья: Рекламный бюджет\n" + OLD_COPY
APPROVED_COPY = (
    "Счет #892 — Согласован✅\n\nДата оплаты: 17.11.2026\n\n"
    "Тестовый сервис\n\n"
    "3 400 сом - оплата за 01.11-14.11\n\nОплата по QR коду"
)
APPENDED_FIELDS = "\n\nПроект: VL\nСтатья: команда\nДата: 19.11.2026"


class InvoiceTextTests(unittest.TestCase):
    def test_labels_in_any_order_before_the_comment(self):
        text = (
            "СУММА: 14.750,5 USD\nFIO — Иван Петров\nДата: 25.11\n"
            "Проект: OR OFFICE\nСтатья расхода: team\nКомментарий: Услуги\nДата оплаты: не менять"
        )
        self.assertEqual(parse_invoice_text(text).fields, {
            "project": "OR OFFICE", "expense_category": "team", "target": "Иван Петров",
            "amount": "14.750,5 USD", "payment_due_date": "25.11",
            "comment": "Услуги\nДата оплаты: не менять",
        })

    def test_old_copy_ignores_author_and_keeps_project_in_comment(self):
        parsed = parse_invoice_text(OLD_COPY)
        self.assertEqual(parsed.copied_request_id, "892")
        self.assertEqual(parsed.fields["target"], "Фб")
        self.assertNotIn("project", parsed.fields)
        self.assertNotIn("old_author", parsed.fields["comment"])
        self.assertEqual(amount_from_invoice_comment(parsed.fields["comment"]), "550$")
        no_header = OLD_COPY.split("\n\n", 1)[1]
        self.assertEqual(parse_invoice_text(no_header).fields, parsed.fields)

    def test_copy_of_payment_message_ignores_old_payer_and_status(self):
        text = (
            "@old_payer\nПроект: VL\nСчет #99 — К оплате\n\n"
            "Дата оплаты: 25.11.2026\nСтатья: Сервисы\nКому платим: Подписка\n"
            "Сумма: $250\nКомментарий: Оплата за ноябрь\n\n"
            "Отправлено: @old_author\nСогласовано: @old_approver"
        )
        parsed = parse_invoice_text(text)
        self.assertEqual(parsed.copied_request_id, "99")
        self.assertEqual(parsed.fields["comment"], "Оплата за ноябрь")
        self.assertEqual(parsed.fields["amount"], "$250")

    def test_fields_appended_after_copied_footer_override_the_old_date(self):
        with_footer = APPROVED_COPY + "\n\nСогласовано: @old_approver\n\nОтправлено: @old_author"
        for copy in (APPROVED_COPY, with_footer, APPROVED_COPY.split("\n\n", 1)[1]):
            with self.subTest(copy=copy):
                parsed = parse_invoice_text(copy + APPENDED_FIELDS)
                self.assertEqual(parsed.fields, {
                    "payment_due_date": "19.11.2026", "project": "VL", "expense_category": "команда",
                    "target": "Тестовый сервис", "comment": "3 400 сом - оплата за 01.11-14.11\n\nОплата по QR коду",
                })

    def test_conflicting_appended_dates_require_a_correction(self):
        parsed = parse_invoice_text(APPROVED_COPY + APPENDED_FIELDS + "\nДата оплаты: 20.11.2026")
        self.assertEqual(parsed.fields["payment_due_date"], "")

    def test_unrelated_messages_and_conflicting_fields(self):
        self.assertIsNone(parse_invoice_text("ФИО: Иван Петров"))
        self.assertIsNone(parse_invoice_text("Добрый день, оплатите 5000"))
        self.assertEqual(parse_invoice_text("Дата: 25", partial=True).fields, {"payment_due_date": "25"})
        parsed = parse_invoice_text("Проект: OR\nСумма: 250$\nСумма: 50$\nСумма: 250$")
        self.assertEqual(parsed.fields["amount"], "")

    def test_implicit_total_beats_components_but_never_guesses_between_them(self):
        comment = "5 000 сом — фикс\n300 сом — премия\n"
        self.assertEqual(amount_from_invoice_comment(comment), "")
        self.assertEqual(amount_from_invoice_comment(comment + "5 300 сом - итоговая сумма к оплате"), "5 300 сом")
        self.assertEqual(amount_from_invoice_comment(comment + "Итого: 5 300 сом"), "5 300 сом")
        self.assertEqual(amount_from_invoice_comment("Итого: 100 сом\nИтого: 200 сом"), "")
        self.assertEqual(amount_from_invoice_comment("Адрес: ул. Пушкина 15, офис 42"), "")
        self.assertEqual(amount_from_invoice_comment("0 сом — не оплачивать"), "")

    def test_structured_amount_keeps_currency_and_discards_invalid_values(self):
        for value in ("$550", "14.750,5 USD", "54 250 s", "1\u202f150 USDT", "4.000,50 ₽", "150000"):
            with self.subTest(value=value):
                self.assertTrue(is_invoice_amount(value))
        for value in ("-500 сом", "0,00 сом", "100$ и 450 сом", "12.11.2026", "1000000 на р/с"):
            with self.subTest(value=value):
                self.assertFalse(is_invoice_amount(value))


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def get_all_values(self):
        return [row.copy() for row in self.rows]

    def append_row(self, row):
        self.rows.append(row.copy())

    def batch_update(self, updates):
        self.updates.extend(updates)


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 19, 10, tzinfo=tz)


def load_bot_functions(sheet, sender):
    tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
    constants = {
        "EXPENSE_CATEGORIES", "EXPENSE_CATEGORY_BY_KEY", "EXPENSE_CATEGORY_LABELS", "TAXI_EXPENSE_CATEGORY",
        "STATUS_PENDING_APPROVAL", "APPROVAL_REMINDER_TIMESTAMP_PREFIX", "PAYMENT_PROJECT_LABEL_KEYS",
    }
    functions = {
        "get_cell", "set_cell", "parse_int", "parse_iso_date", "append_invoice_request", "find_request_row",
        "copied_invoice_date", "prepare_quick_invoice", "handle_quick_invoice", "handle_message", "handle_file",
        "submit_telegram_invoice", "get_payment_due_date", "get_payment_date_text",
        "get_expense_category", "is_taxi_invoice", "format_approval_reminder_timestamp",
        "format_invoice_creator", "format_user_tag", "get_invoice_payer_tag",
        "build_invoice_details", "build_pending_approval_invoice_text", "build_payment_invoice_text",
    }
    nodes = [
        node for node in tree.body
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions
        ) or (
            isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and (target.id in constants or target.id.endswith("_COL"))
                for target in node.targets
            )
        )
    ]
    env = {
        "asyncio": asyncio, "datetime": FixedClock, "logging": logging, "re": re,
        "REMINDER_TZ": timezone.utc, "APPROVAL_REMINDER_TZ": timezone.utc,
        "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object), "Update": object,
        "parse_payment_date": parse_payment_date, "format_payment_date": format_payment_date,
        "user_state": {}, "payment_state": {}, "reject_state": {}, "sheet": sheet,
        "get_project_settings": Mock(side_effect=lambda project: {
            "VL": {"approval_chat_id": -100101, "payment_chat_id": -100102, "payer_tag": "@payer"},
            "TAXI": {"approval_chat_id": -100103, "payment_chat_id": None, "payer_tag": ""},
        }.get(project.upper())),
        "send_pending_approval_invoice": sender, "request_append_lock": Lock(),
        "parse_invoice_text": parse_invoice_text, "parse_taxi_amount": parse_taxi_amount,
        "is_invoice_amount": is_invoice_amount, "amount_from_invoice_comment": amount_from_invoice_comment,
        "TELEGRAM_INVOICE_KEY_PREFIX": TELEGRAM_INVOICE_KEY_PREFIX,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), env)
    return env


class QuickInvoiceWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        old = [""] * 27
        old[0], old[10], old[23] = "892", "42", "2026-11-25"
        self.sheet = FakeSheet([["ID"], old])
        self.approval = AsyncMock(return_value=SimpleNamespace(chat_id=-100101, message_id=355))
        self.env = load_bot_functions(self.sheet, self.approval)
        self.bot = object()

    async def send(self, text, message_id=100, *, chat_type="private", document=None, edited=False):
        message = SimpleNamespace(
            text=text if not document else None, caption=text if document else None,
            message_id=message_id, reply_text=AsyncMock(), document=document, photo=(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=42, type=chat_type),
            effective_user=SimpleNamespace(id=42, username="current_author", first_name="Иван"),
            message=None if edited else message,
        )
        handler = self.env["handle_file" if document else "handle_message"]
        await handler(update, SimpleNamespace(bot=self.bot))
        return message.reply_text

    async def test_complete_invoice_is_sent_once_via_existing_approval_route(self):
        reply = await self.send(QUICK_TEXT)
        self.assertEqual(len(self.sheet.rows), 3)
        row = self.sheet.rows[2]
        self.assertEqual((row[0], row[2], row[10], row[13]), ("893", "current_author", "42", "@payer"))
        self.assertEqual(row[3:9], [
            "VL", "Иван Петров", "15 250,50 сом", "Подписка на сервис\nРеквизиты: указаны в PDF.",
            "На согласовании", "-100101",
        ])
        self.assertEqual(row[19:24], ["telegram-invoice|42:100", "", "", "Сервисы", "2026-11-25"])
        self.approval.assert_awaited_once_with(self.bot, -100101, row)
        self.assertEqual(self.sheet.updates[-1]["values"], [["-100101", "355"]])
        self.assertIn("Счёт #893 принят", reply.call_args.args[0])
        rendered = self.env["build_pending_approval_invoice_text"](row)
        self.assertIn("Сумма к оплате: 15 250,50 сом", rendered)
        copied = parse_invoice_text(rendered)
        self.assertEqual(copied.fields["target"], "Иван Петров")
        self.assertEqual(amount_from_invoice_comment(copied.fields["comment"]), "15 250,50 сом")

    async def test_pasted_invoice_submits_once_and_uses_original_message_layouts(self):
        self.sheet.rows[1][23] = "2026-11-17"
        reply = await self.send(APPROVED_COPY + APPENDED_FIELDS)
        self.assertIn("Счёт #893 принят", reply.call_args.args[0])
        row = self.sheet.rows[-1]
        body = (
            "Дата оплаты: 19.11.2026\n\nТестовый сервис\n\n"
            "3 400 сом - оплата за 01.11-14.11\n\nОплата по QR коду"
        )
        self.assertEqual(row[3:6], ["VL", "Тестовый сервис", "3 400 сом"])
        self.assertEqual(row[22:24], ["Команда", "2026-11-19"])
        self.approval.assert_awaited_once_with(self.bot, -100101, row)
        self.assertEqual(
            self.env["build_pending_approval_invoice_text"](row),
            f"Новый счет #893\n\n{body}\n\nОтправлено: @current_author",
        )
        row[12] = "test_approver"
        self.assertEqual(
            self.env["build_payment_invoice_text"](row),
            f"@payer\nПроект: VL\nСчет #893 — К оплате\n\n{body}\n\n"
            "Отправлено: @current_author\nСогласовано: @test_approver",
        )

    async def test_pasted_invoice_with_unchanged_appended_date_still_requires_confirmation(self):
        copy = APPROVED_COPY.replace("17.11.2026", "25.11.2026")
        reply = await self.send(copy + APPENDED_FIELDS.replace("19.11.2026", "25.11.2026"))
        self.assertIn("Дата совпадает со старым счётом", reply.call_args.args[0])
        self.approval.assert_not_awaited()

    async def test_old_id_and_unchanged_future_date_require_explicit_date_reply(self):
        reply = await self.send(COPY_WITH_FIELDS)
        self.assertIn("Дата совпадает со старым счётом", reply.call_args.args[0])
        await self.send(COPY_WITH_FIELDS, 101)
        self.approval.assert_not_awaited()
        self.assertEqual(len(self.sheet.rows), 2)
        await self.send("25.11.2026", 102)
        row = self.sheet.rows[-1]
        self.assertEqual((row[0], row[5], row[23]), ("893", "550$", "2026-11-25"))
        self.assertEqual(row[19], "telegram-invoice|42:100")
        self.approval.assert_awaited_once()

    async def test_copy_with_updated_date_is_sent_without_confirmation(self):
        await self.send(COPY_WITH_FIELDS.replace("25.11.2026", "01.12.2026"))
        row = self.sheet.rows[-1]
        self.assertEqual((row[0], row[23]), ("893", "2026-12-01"))
        rendered = self.env["build_pending_approval_invoice_text"](row)
        self.assertNotIn("#892", rendered)
        self.assertNotIn("old_author", rendered)
        self.assertIn("Отправлено: @current_author", rendered)

    async def test_past_date_rejected_even_with_number_removed(self):
        prompt = await self.send(QUICK_TEXT.replace("25.11.2026", "18.11.2026"))
        self.assertIn("Дата оплаты не может быть в прошлом", prompt.call_args.args[0])
        self.approval.assert_not_awaited()
        await self.send("21.11.2026", 101)
        self.approval.assert_awaited_once()
        self.assertEqual(self.sheet.rows[-1][23], "2026-11-21")

    async def test_legacy_copy_asks_only_for_missing_fields_and_stale_date(self):
        reply = await self.send(OLD_COPY)
        prompt = reply.call_args.args[0]
        self.assertIn("Проект:", prompt)
        self.assertIn("Статья:", prompt)
        self.assertIn("Дата совпадает со старым счётом", prompt)
        self.assertNotIn("Сумма:", prompt)
        await self.send("Проект: VL\nСтатья: Рекламный бюджет\nДата оплаты: 26.11.2026", 101)
        self.approval.assert_awaited_once()
        self.assertEqual(self.sheet.rows[-1][4:6], ["Фб", "550$"])

    async def test_attachment_with_caption_and_taxi_without_due_date(self):
        text = QUICK_TEXT.replace("Проект: VL", "Проект: TAXI").replace("Сервисы", "Такси")
        text = text.replace("Дата оплаты: 25.11.2026\n", "")
        await self.send(text, document=SimpleNamespace(file_id="file-test"))
        row = self.sheet.rows[-1]
        self.assertEqual((row[9], row[22], row[23]), ("file-test", "Такси", ""))
        self.approval.assert_awaited_once_with(self.bot, -100103, row)

    async def test_same_telegram_message_never_appends_or_sends_again(self):
        await self.send(QUICK_TEXT)
        reply = await self.send(QUICK_TEXT)
        self.assertIn("Счёт #893 уже сохранён", reply.call_args.args[0])
        self.assertEqual(len(self.sheet.rows), 3)
        self.approval.assert_awaited_once()

    async def test_delivery_failure_does_not_turn_resubmission_into_another_invoice(self):
        self.approval.side_effect = RuntimeError("telegram test failure")
        with self.assertLogs(level="ERROR"):
            reply = await self.send(QUICK_TEXT)
        self.assertIn("отправка на согласование не подтверждена", reply.call_args.args[0])
        await self.send(QUICK_TEXT)
        self.assertEqual(len(self.sheet.rows), 3)
        self.approval.assert_awaited_once()

    async def test_group_and_edited_updates_do_not_create_invoices(self):
        await self.send(QUICK_TEXT, chat_type="supergroup")
        await self.send(QUICK_TEXT, edited=True)
        self.approval.assert_not_awaited()
        self.assertEqual(len(self.sheet.rows), 2)

    async def test_new_private_invoice_does_not_become_rejection_reason_in_another_chat(self):
        self.env["reject_state"][42] = {"chat_id": -100150, "request_id": "892"}
        await self.send(QUICK_TEXT)
        self.approval.assert_awaited_once()
        self.assertEqual(self.env["reject_state"][42]["request_id"], "892")

    async def test_clearly_a_new_invoice_attachment_is_not_taken_as_a_pending_receipt(self):
        self.env["payment_state"][42] = {"chat_id": -100102, "request_id": "892"}
        reply = await self.send(QUICK_TEXT, document=SimpleNamespace(file_id="file-test"))
        self.assertIn("не принять этот файл за чек", reply.call_args.args[0])
        self.approval.assert_not_awaited()
        self.assertEqual(self.env["payment_state"][42]["request_id"], "892")

    async def test_existing_wizard_does_not_treat_a_multiline_comment_as_new_invoice(self):
        self.env["user_state"][42] = {
            "project": "VL", "expense_category": "Команда", "target": "Бухгалтер",
            "amount": "5000 сом", "payment_due_date": "2026-11-25", "file_step_done": True,
            "approval_chat_id": -100101, "payment_chat_id": -100102,
        }
        await self.send(QUICK_TEXT)
        row = self.sheet.rows[-1]
        self.assertEqual(row[4:7], ["Бухгалтер", "5000 сом", QUICK_TEXT])
        self.assertEqual(row[19], "")
        self.approval.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
