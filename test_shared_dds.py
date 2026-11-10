import ast
import asyncio
import logging
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from shared_dds import (
    SHARED_DDS_CHAT_ID,
    SHARED_DDS_OFFICE_CHAT_ID,
    build_shared_paid_invoice,
    format_shared_balance_report,
    resolve_shared_wallet,
)


class SharedDdsWalletTests(unittest.TestCase):
    def test_author_and_currency_choose_wallet_without_guessing(self):
        for username, currency, wallet in (
            ("@n0visad", "KGS", "Александр С"),
            ("roshchupkin_egor", "USD", "Александр D"),
            ("roshchupkin_egor", "USDT", "Александр U"),
            ("ba_roshchupkin", "KGS", "Константин С"),
            ("ba_roshchupkin", "USDT", "Константин U"),
            ("ba_roshchupkin", "USD", ""),
            ("unknown", "KGS", ""),
        ):
            with self.subTest(username=username, currency=currency):
                self.assertEqual(resolve_shared_wallet(SHARED_DDS_CHAT_ID, username, currency, "Аренда"), wallet)
        self.assertEqual(resolve_shared_wallet(SHARED_DDS_OFFICE_CHAT_ID, "unknown", "KGS", "Расход"), "Офис подотчет")
        self.assertEqual(resolve_shared_wallet(-1003806940668, "n0visad", "KGS", "Расход"), "")

    def test_explicit_accounts_take_precedence_and_ambiguous_account_stays_blank(self):
        for purpose, wallet in (
            ("Аренда с р/с ОР", "р/c ОР"),
            ("Оплата с R.S. ВЛ", "р/c ВЛ"),
            ("Оплата с RS СТ", "р/c СТ"),
            ("Оплата с расчётного счёта: СКП", "р/c СКП"),
            ("Расход с р/с", ""),
            ("Расход с р/с ОР или р/с СТ", ""),
        ):
            with self.subTest(purpose=purpose):
                self.assertEqual(resolve_shared_wallet(SHARED_DDS_CHAT_ID, "n0visad", "KGS", purpose), wallet)

    def test_paid_invoice_keeps_actual_currency_and_invoice_link(self):
        candidate = build_shared_paid_invoice(
            "951", "50 U", "Оплата тарифа №4", "За ноябрь", SHARED_DDS_CHAT_ID,
            "roshchupkin_egor", "https://t.me/c/3721730980/12",
        )
        self.assertEqual((candidate.amount, candidate.currency, candidate.wallet), (-50, "USDT", "Александр U"))
        self.assertIn("Оплата тарифа №4", candidate.description)
        self.assertTrue(candidate.description.endswith("https://t.me/c/3721730980/12"))

    def test_paid_invoice_cannot_create_income(self):
        with self.assertRaises(ValueError):
            build_shared_paid_invoice(
                "951", "+50 USDT", "Оплата тарифа", "", SHARED_DDS_CHAT_ID, "n0visad", "",
            )


class SharedDdsBalanceTests(unittest.TestCase):
    def balance_rows(self):
        return [
            ["Итого", "U0,00", "Александр", "U0,00", "Константин", "U12,50",
             "р/c ОР", "279 300,00", "р/c ВЛ", "103 800,00", "Офис подотчет", "29 971,74",
             "Материальные активы", "$100,00", "Кредитные деньги", "777,00"],
            ["$1 000,00", "5 000,00", "$1 000,00", "5 000,00", "", "0,00",
             "р/c СТ", "70 000,00", "р/c СКП", "-1 000,00", "Зарплатный фонд", "0,00",
             "Инвестиции фонда", "0,00", "", ""],
        ]
    def test_only_ten_wallets_and_their_currency_totals_are_included(self):
        text = format_shared_balance_report(self.balance_rows(), date(2026, 11, 6))
        lines = text.splitlines()
        self.assertEqual(len(lines), 17)
        self.assertEqual(lines[:2], ["Остатки на 06.11.2026", ""])
        self.assertIn("Александр U: 0,00 USDT", lines)
        self.assertIn("Александр D: 1 000,00 $", lines)
        self.assertIn("р/c СКП: -1 000,00 сом", lines)
        self.assertIn("Офис подотчет: 29 971,74 сом", lines)
        self.assertNotIn("Кредитные деньги", text)
        self.assertEqual(lines[-5:], ["", "Итого:", "487 071,74 сом", "1 000,00 $", "12,50 USDT"])

    def test_total_is_unavailable_if_a_wallet_is_missing_or_has_wrong_currency(self):
        for invalid in ("", "U279 300,00", "#REF!", "12,3,4"):
            with self.subTest(invalid=invalid):
                rows = self.balance_rows()
                rows[0][7] = invalid
                lines = format_shared_balance_report(rows, date(2026, 11, 6)).splitlines()
                self.assertEqual(lines[-3:], ["сом: нет данных", "1 000,00 $", "12,50 USDT"])

    def test_totals_accept_nonbreaking_group_separators_and_keep_decimal_precision(self):
        rows = self.balance_rows()
        rows[0][3], rows[0][5] = "U1\u00a0000,10", "U2\u202f000,20"
        lines = format_shared_balance_report(rows, date(2026, 11, 6)).splitlines()
        self.assertEqual(lines[-1], "3 000,30 USDT")


class SharedDdsReportJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_waits_until_evening_and_does_not_send_twice(self):
        source = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
        node = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "send_scheduled_shared_dds_balances")
        writer = SimpleNamespace(log_entries={}, dds_sheet=SimpleNamespace(get=Mock(return_value=[])),
                                 record_diagnostic=Mock())
        clock = Mock()
        context = SimpleNamespace(application=SimpleNamespace(bot_data={}),
                                  bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42))))
        namespace = {
            "shared_dds_writer": writer, "datetime": clock, "SHARED_DDS_REPORT_TZ": timezone.utc,
            "asyncio": asyncio, "logging": logging, "SHARED_DDS_CHAT_ID": SHARED_DDS_CHAT_ID,
            "format_shared_balance_report": format_shared_balance_report,
            "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), "bot.py", "exec"), namespace)
        send = namespace["send_scheduled_shared_dds_balances"]
        clock.now.return_value = datetime(2026, 11, 7, 19, 59, tzinfo=timezone.utc)
        await send(context)
        context.bot.send_message.assert_not_awaited()
        clock.now.return_value = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
        await send(context)
        await send(context)
        context.bot.send_message.assert_awaited_once()
        self.assertEqual(context.bot.send_message.await_args.kwargs["chat_id"], SHARED_DDS_CHAT_ID)
        self.assertEqual(writer.record_diagnostic.call_args.args[0], "balance_report:2026-11-07")
        context.application.bot_data.clear()
        writer.log_entries["balance_report:2026-11-07"] = {"status": "sent"}
        await send(context)
        context.bot.send_message.assert_awaited_once()
