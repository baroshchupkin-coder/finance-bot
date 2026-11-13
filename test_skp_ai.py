import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from shared_dds import SHARED_DDS_CHAT_ID, SHARED_DDS_OFFICE_CHAT_ID, SHARED_DDS_SPREADSHEET_ID
from skp_ai import (
    DEFAULT_MODEL, EXCHANGE_ARTICLES, FINANCING_ARTICLES, NO_PROJECT,
    TRANSFER_ARTICLES, WALLET_CURRENCIES, Catalog, request_decision, validate_response,
)
from skp_ai_pilot import CALLS_PER_MONTH, Queue
from skp_ai_sheet import PILOT_SPREADSHEET_ID, PilotWriter, build_entry_requests, existing_rows


DAY = date(2026, 11, 13)
PROJECT = "ВЛ — команда Алексея"
ARTICLES = {
    name: {"group": "Поступление" if name.startswith(("Доход", "Объем")) else "Выбытие"}
    for name in (*TRANSFER_ARTICLES, *EXCHANGE_ARTICLES, *FINANCING_ARTICLES, "1. Построения", "Объем продаж")
}
CATALOG = Catalog(WALLET_CURRENCIES, {
    NO_PROJECT: (*TRANSFER_ARTICLES, *EXCHANGE_ARTICLES),
    PROJECT: ("1. Построения", "Объем продаж", *FINANCING_ARTICLES),
}, ARTICLES)


def entry(amount, evidence, wallet="Александр С", *, side="debit", project=PROJECT,
          article="1. Построения", currency="KGS", day=DAY):
    return {"date": day.isoformat(), "side": side, "amount": amount, "amount_source": evidence,
            "currency": currency, "wallet": wallet, "counterparty": "", "purpose": "Пилотная операция",
            "project": project, "article": article}


def decision(kind, *entries):
    return {"action": "write", "kind": kind, "missing": "none", "entries": list(entries)}


def check(raw, text, *, chat=SHARED_DDS_CHAT_ID, user="n0visad", replies=()):
    return validate_response(raw, text, DAY, CATALOG, chat, user, replies)


def transfer(amount="800000", quote="800 000 сом", target="р/c СКП"):
    return decision("transfer",
                    entry(amount, quote, project=NO_PROJECT, article=TRANSFER_ARTICLES[0]),
                    entry(amount, quote, target, side="credit", project=NO_PROJECT, article=TRANSFER_ARTICLES[1]))


class ExtractionTests(unittest.TestCase):
    def test_explicit_bank_account_overrides_sender(self):
        result = check(decision("expense", entry("51000", "51 000 сом", "р/c ОР")),
                       "Оплатили 51 000 сом с расчётного счёта OR", user="ba_roshchupkin")
        self.assertEqual(result.entries[0].amount, -51000)
        self.assertEqual(result.entries[0].wallet, "р/c ОР")
        self.assertEqual(check(decision("expense", entry("51000", "51 000 сом")),
                               "Оплатили 51 000 сом с расчётного счёта OR").reason, "wallet")

    def test_ambiguous_account_requires_reply_and_plans_are_not_paid(self):
        raw = decision("expense", entry("51000", "51 000", "р/c ВЛ"))
        self.assertEqual(check(raw, "−51 000, счёт ВЛ на зарплаты").reason, "wallet")
        self.assertEqual(check(raw, "−51 000, счёт ВЛ на зарплаты", replies=("С р/с ВЛ",)).action, "write")
        self.assertEqual(check(raw, "Нужно оплатить 51 000 с р/с ВЛ").reason, "settled")
        self.assertEqual(check(raw, "Нужно оплатить 51 000 с р/с ВЛ", replies=("Оплатили",)).action, "write")

    def test_transfer_and_office_topup(self):
        good = check(transfer(), "Перевели 800 000 сом из наличных на СКП")
        self.assertEqual([(e.amount, e.wallet) for e in good.entries],
                         [(-800000, "Александр С"), (800000, "р/c СКП")])
        self.assertEqual(check(transfer(target="р/c ВЛ"), "Перевели 800 000 сом из наличных на СКП").reason,
                         "wallet")
        office = check(transfer("1500", "+ 1.500 сом", "Офис подотчет"),
                       "13.11 - + 1.500 сом - пополнение в под отчёт\nОстаток: 2.216,74 сом",
                       chat=SHARED_DDS_OFFICE_CHAT_ID, user="office_member")
        self.assertEqual([e.amount for e in office.entries], [-1500, 1500])

    def test_conversion_and_financing(self):
        exchange = decision(
            "exchange",
            entry("94300", "94 300 сом", project=NO_PROJECT, article=EXCHANGE_ARTICLES[0]),
            entry("1070", "1 070 USDT", "Александр U", currency="USDT", side="credit",
                  project=NO_PROJECT, article=EXCHANGE_ARTICLES[1]),
        )
        self.assertEqual([e.amount for e in check(exchange, "Обменяли 94 300 сом на 1 070 USDT").entries],
                         [-94300, 1070])
        financing = decision(
            "financing", entry("30000", "30 000 сом", article=FINANCING_ARTICLES[0]),
            entry("4000", "4 000 сом", article=FINANCING_ARTICLES[1]),
        )
        self.assertEqual(check(financing, "Выплатили 30 000 сом тело и 4 000 сом проценты").action, "write")
        self.assertEqual(check(financing, "Выплатили всего 34 000 сом").reason, "split")
        self.assertEqual(check(financing, "Выплатили 30 000 сом проценты и 4 000 сом тело").reason, "split")

    def test_thousand_suffix_cannot_be_ignored(self):
        text = "Оплатили 1,5к сом за материалы"
        self.assertEqual(check(decision("expense", entry("1500", "1,5к сом")), text).action, "write")
        self.assertEqual(check(decision("expense", entry("1.5", "1,5")), text).reason, "amount")

    def test_rejects_invented_amount_date_currency_article_and_balance(self):
        for raw, text, reason in (
            (entry("1500", "1500"), "Оплатили 15000 сом", "amount"),
            (entry("1500", "1500"), "Оплатили $1500", "amount"),
            (entry("1500", "1500"), "Оплатили уборку\nОстаток 1500", "amount"),
            (entry("12.11", "12.11"), "12.11 - оплатили 1500", "amount"),
            (entry("1500", "1500", day=date(2026, 11, 12)), "Оплатили 1500 сом", "date"),
            (entry("1500", "1500"), "12.11 - оплатили 1500 сом", "date"),
            (entry("1500", "1500", article="Доходы с производства"), "Оплатили 1500 сом", "article"),
            (entry("1500", "1500"), "+1500 сом возврат", "amount"),
        ):
            with self.subTest(text=text, reason=reason):
                self.assertEqual(check(decision("expense", raw), text).reason, reason)

    def test_responses_request_redacts_secrets_and_reports_usage(self):
        output = json.dumps(decision("expense", entry("1500", "1500 сом")))
        reply = {"status": "completed", "usage": {"input_tokens": 200, "output_tokens": 50},
                 "output": [{"type": "message", "content": [{"type": "output_text", "text": output}]}]}
        send = Mock(return_value=io.BytesIO(json.dumps(reply).encode()))
        result, usage = request_decision("test-key", DEFAULT_MODEL, CATALOG,
                                         "Оплатили 1500 сом, карта 1111 2222 3333 4444", DAY,
                                         SHARED_DDS_CHAT_ID, "n0visad", send=send)
        payload = json.loads(send.call_args.args[0].data)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertIn("[redacted]", payload["input"])
        self.assertNotIn("1111", payload["input"])
        self.assertEqual(result.action, "write")
        self.assertEqual(usage, {"input_tokens": 200, "output_tokens": 50})

    def test_balance_does_not_call_api(self):
        send = Mock()
        result, _ = request_decision("test-key", DEFAULT_MODEL, CATALOG, "Остаток: 1000 сом",
                                     DAY, SHARED_DDS_CHAT_ID, send=send)
        self.assertEqual(result.action, "skip")
        send.assert_not_called()


class WriterTests(unittest.TestCase):
    def test_batch_is_limited_to_input_cells_and_links(self):
        entries = check(transfer(), "Перевели 800 000 сом из наличных на СКП").entries
        link = "https://t.me/c/1234567/81"
        requests = build_entry_requests(0, 21, entries, link)
        self.assertEqual(len(requests), 4)
        self.assertEqual([r["updateCells"]["start"]["columnIndex"] for r in requests], [3, 7, 3, 7])
        self.assertEqual(len(requests[0]["updateCells"]["rows"][0]["values"]), 7)
        self.assertEqual(existing_rows([["", "", "", "", link + "0"], ["", "", "", "", link]], link), [10])

    def test_production_rejected_and_manual_row_blocks_retry(self):
        with self.assertRaises(ValueError):
            PilotWriter(SimpleNamespace(id=SHARED_DDS_SPREADSHEET_ID))
        chat = -1001234567
        sheet = SimpleNamespace(id=0, row_count=100, get=Mock(return_value=[
            ["", "", "", "", "Оплата\nhttps://t.me/c/1234567/81"],
        ]))
        book = SimpleNamespace(id=PILOT_SPREADSHEET_ID, worksheet=Mock(return_value=sheet), batch_update=Mock())
        writer = PilotWriter(book)
        entries = check(transfer(), "Перевели 800 000 сом из наличных на СКП").entries
        self.assertEqual(writer.seen(chat, 81), [4])
        self.assertEqual(writer.write(chat, 81, entries), ("review", [4]))
        book.batch_update.assert_not_called()

    def test_retry_after_successful_batch_with_lost_response_does_not_write_twice(self):
        entries = check(transfer(), "Перевели 800 000 сом из наличных на СКП").entries
        sheet = SimpleNamespace(id=0, row_count=100, get=Mock())
        book = SimpleNamespace(id=PILOT_SPREADSHEET_ID, worksheet=Mock(return_value=sheet), batch_update=Mock())
        writer = PilotWriter(book)
        recorded = []

        def get(range_name, *, value_render_option):
            if range_name == "Q9:Q10":
                return [[6], [7]]
            if range_name == "D4:J100":
                return [[], [], [], [], [], *recorded]
            return recorded

        def lose_response(batch):
            for request in batch["requests"][::2]:
                cells = request["updateCells"]["rows"][0]["values"]
                recorded.append([next(iter(c.get("userEnteredValue", {}).values()), "") for c in cells])
            raise TimeoutError("Response lost after commit")

        sheet.get.side_effect = get
        book.batch_update.side_effect = lose_response
        with self.assertRaises(TimeoutError):
            writer.write(-1001234567, 81, entries)
        self.assertEqual(writer.write(-1001234567, 81, entries), ("already_present", [9, 10]))
        book.batch_update.assert_called_once()


class QueueTests(unittest.TestCase):
    def test_restart_keeps_pending_operation_reply_and_staged_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "queue.sqlite")
            queue = Queue(path)
            queue.enqueue(81, 1, "n0visad", DAY, "Оплатили 1500")
            queue.finish(81, "waiting", question_id=82)
            self.assertFalse(queue.answer(82, 2, "Александр С"))
            queue.db.close()
            queue = Queue(path)
            self.assertTrue(queue.answer(82, 1, "Александр С"))
            self.assertEqual(json.loads(queue.next()["replies"]), ["Александр С"])
            entries = check(transfer(), "Перевели 800 000 сом из наличных на СКП").entries
            queue.stage_write(81, entries)
            queue.db.close()
            queue = Queue(path)
            self.assertEqual(queue.next()["state"], "writing")
            self.assertEqual(len(json.loads(queue.next()["entries"])), 2)
            self.assertTrue(queue.edit(81, "Исправление"))
            self.assertIsNone(queue.next())
            queue.db.close()

    def test_call_budget_is_reserved_before_request(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Queue(str(Path(temp) / "queue.sqlite"))
            self.assertTrue(all(queue.claim_call() for _ in range(CALLS_PER_MONTH)))
            self.assertFalse(queue.claim_call())
            queue.db.close()
