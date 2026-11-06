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


class SharedDdsBalanceTests(unittest.TestCase):
    def test_only_ten_wallets_are_included_with_their_formatted_currency(self):
        rows = [
            ["Итого", "U0,00", "Александр", "U0,00", "Константин", "U12,50",
             "р/c ОР", "279 300,00", "р/c ВЛ", "103 800,00", "Офис подотчет", "29 971,74",
             "Материальные активы", "$100,00", "Кредитные деньги", "777,00"],
            ["$1 000,00", "5 000,00", "$1 000,00", "5 000,00", "", "0,00",
             "р/c СТ", "70 000,00", "р/c СКП", "-1 000,00", "Зарплатный фонд", "0,00",
             "Инвестиции фонда", "0,00", "", ""],
        ]
        text = format_shared_balance_report(rows, date(2026, 11, 6))
        lines = text.splitlines()
        self.assertEqual(len(lines), 12)
        self.assertIn("Александр U: 0,00 USDT", lines)
        self.assertIn("Александр D: 1 000,00 $", lines)
        self.assertIn("р/c СКП: -1 000,00 сом", lines)
        self.assertIn("Офис подотчет: 29 971,74 сом", lines)
        self.assertNotIn("Кредитные деньги", text)
        self.assertNotIn("Итого", text)


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
