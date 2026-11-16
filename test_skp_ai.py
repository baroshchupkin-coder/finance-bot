import io
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from shared_dds import SHARED_DDS_CHAT_ID, SHARED_DDS_SPREADSHEET_ID
from skp_ai import (
    ARTICLE_HINTS, DEFAULT_MODEL, EXCHANGE_ARTICLES, FINANCING_ARTICLES, NO_PROJECT,
    TRANSFER_ARTICLES, WALLET_CURRENCIES, Catalog, Decision, request_decision, validate_response,
)
from skp_ai_worker import CALLS_PER_MONTH, SolWorker, Queue, confirmation_text
from skp_ai_sheet import (
    ARTICLE_TAB, PILOT_SPREADSHEET_ID, SHEET_NAME, SUMMARY_TAB, WALLET_TAB,
    SolDdsWriter, build_entry_requests, existing_rows, read_catalog,
)


DAY = date(2026, 11, 13)
PROJECT = "ВЛ — команда Алексея"
SKP_PROJECT = "СКП — команда Алибека"
PRODUCTION_PROJECT = "Производство"
COMMISSION_ARTICLE = "Переменная мотивация"
PRODUCTION_ARTICLE = "Расходы на производство"
ARTICLES = {
    name: {"group": "Поступление" if name.startswith(("Доход", "Объем")) else "Выбытие"}
    for name in (
        *TRANSFER_ARTICLES, *EXCHANGE_ARTICLES, *FINANCING_ARTICLES,
        "1. Построения", "Объем продаж", COMMISSION_ARTICLE, PRODUCTION_ARTICLE,
    )
}
CATALOG = Catalog(WALLET_CURRENCIES, {
    NO_PROJECT: (*TRANSFER_ARTICLES, *EXCHANGE_ARTICLES),
    PROJECT: ("1. Построения", "Объем продаж", *FINANCING_ARTICLES),
    SKP_PROJECT: (COMMISSION_ARTICLE,),
    PRODUCTION_PROJECT: (PRODUCTION_ARTICLE,),
}, ARTICLES)


def entry(amount, wallet="Александр С", *, side="debit", project=PROJECT,
          article="1. Построения", currency="KGS", day=DAY):
    return {"date": day.isoformat(), "side": side, "amount": amount,
            "currency": currency, "wallet": wallet, "counterparty": "", "purpose": "Пилотная операция",
            "project": project, "article": article}


def operation(kind, *entries, status="completed"):
    return {"kind": kind, "status": status, "entries": list(entries)}


def batch(*operations, action="write", missing="none"):
    return {"action": action, "missing": missing, "operations": list(operations)}


def decision(kind, *entries):
    return batch(operation(kind, *entries))


def api_message(raw, *, target="new", question=""):
    return {"type": "message", "content": [{
        "type": "output_text",
        "text": json.dumps({"decision": raw, "applies_to": target, "question": question}),
    }]}


def api_write(raw, *, target="new", note=""):
    return {
        "type": "function_call", "call_id": "call_write", "name": "write_dds",
        "arguments": json.dumps({
            "applies_to": target, "note": note,
            "operations": [{k: op[k] for k in ("kind", "entries")} for op in raw["operations"]],
        }),
    }


def check(raw, text, *, replies=()):
    return validate_response(raw, text, DAY, CATALOG, replies)


def transfer(amount="800000", target="р/c СКП"):
    return decision("transfer",
                    entry(amount, project=NO_PROJECT, article=TRANSFER_ARTICLES[0]),
                    entry(amount, target, side="credit", project=NO_PROJECT, article=TRANSFER_ARTICLES[1]))


MIXED_REQUEST = "Можете 800к наличных перевести на СКП и из наличных 94 300 сом на  1070 юсдт поменять"
REQUEST_AND_RECEIPT = MIXED_REQUEST + "\n+ 100 500 наличными от ВЛ"


def receipts():
    return operation("income", entry("100500", "Константин С", side="credit"))


def mixed_request():
    return batch(
        {**transfer()["operations"][0], "status": "unconfirmed"},
        operation(
            "exchange",
            entry("94300", project=NO_PROJECT, article=EXCHANGE_ARTICLES[0]),
            entry("1070", "Александр U", side="credit", currency="USDT",
                  project=NO_PROJECT, article=EXCHANGE_ARTICLES[1]),
            status="unconfirmed",
        ),
        action="confirm",
    )


class ExtractionTests(unittest.TestCase):
    def test_model_selected_draft_for_combined_movements_preserves_all_four_rows(self):
        prepared = check(mixed_request(), MIXED_REQUEST)
        self.assertEqual(prepared.action, "confirm")
        self.assertEqual([(e.amount, e.currency, e.wallet) for e in prepared.entries],
                         [(-800000, "KGS", "Александр С"), (800000, "KGS", "р/c СКП"),
                          (-94300, "KGS", "Александр С"), (1070, "USDT", "Александр U")])
        self.assertIn("+1 070 USDT", confirmation_text(prepared.entries))
        raw = mixed_request()
        raw["operations"][0]["entries"][0]["amount"] = "700000"
        malformed_preview = check(raw, MIXED_REQUEST)
        self.assertEqual(malformed_preview.action, "confirm")
        self.assertEqual([entry.amount for entry in malformed_preview.entries], [-94300, 1070])
        for op in raw["operations"]:
            op["status"] = "completed"
        raw["action"] = "write"
        self.assertEqual(check(raw, MIXED_REQUEST).reason, "pair")

    def test_code_does_not_change_intent_based_on_phrasing(self):
        text = "800k наличных перевести на SKP и из наличных поменять на USDT"
        self.assertEqual(check(batch(action="skip"), text).action, "skip")
        clarification = check(batch(action="clarify", missing="exchange_amounts"), text)
        self.assertEqual((clarification.action, clarification.reason), ("clarify", "exchange_amounts"))
        actual = decision("expense", entry("1500"))
        self.assertEqual(check(actual, "Уже оплатили 1500 сом за доставку, на завтра ничего не нужно?").action, "write")

    def test_completed_and_unconfirmed_are_not_written_together_before_answer(self):
        raw = batch(*mixed_request()["operations"], receipts(), action="confirm", missing="settled")
        model_result = check(raw, REQUEST_AND_RECEIPT)
        self.assertEqual((model_result.action, model_result.completed_count), ("confirm", 1))
        self.assertEqual([row.amount for row in model_result.entries], [100500, -800000, 800000, -94300, 1070])
        preview = confirmation_text(model_result.entries, model_result.completed_count)
        self.assertIn("Уже проведено по сообщению:\n+100 500", preview)
        self.assertIn("Нужно подтвердить:\n−800 000", preview)

    def test_missing_details_keeps_valid_preview_without_writing(self):
        raw = batch(receipts(), action="clarify", missing="exchange_amounts")
        result = check(raw, "Обменяли наличные на USDT\n+ 100 500 наличными от ВЛ")
        self.assertEqual((result.action, result.reason, result.completed_count), ("clarify", "exchange_amounts", 1))
        self.assertEqual([e.amount for e in result.entries], [100500])

    def test_model_question_does_not_require_a_preview_or_category(self):
        reply = {"status": "completed", "output": [
            api_message(batch(action="confirm", missing="settled"), target="pending", question="Уже провели?"),
        ]}
        pending = {"message": "Сделать перевод", "message_date": DAY.isoformat(), "reason": "draft", "replies": []}
        result, _ = request_decision("test-key", DEFAULT_MODEL, CATALOG, "Кошельки Константина", DAY,
                                     SHARED_DDS_CHAT_ID, pending=pending,
                                     send=Mock(return_value=io.BytesIO(json.dumps(reply).encode())))
        self.assertEqual((result.action, result.entries, result.question), ("confirm", (), "Уже провели?"))
        self.assertTrue(result.applies_to_pending)

    def test_follow_up_date_does_not_shift_already_completed_entries(self):
        next_day = date(2026, 11, 14)
        raw = batch(
            receipts(),
            operation("income", entry("600", side="credit", day=next_day)),
        )
        result = validate_response(raw, "+ 100 500 наличными от ВЛ\nВнести 600 сом", next_day, CATALOG,
                                   ("Всё уже сделали",))
        self.assertEqual([entry.date for entry in result.entries], [DAY, next_day])

    def test_reported_bank_transfer_needs_no_status_or_date_question(self):
        text = "100000 с рс ор на рс ст"
        proposed = decision(
            "transfer",
            entry("100000", "р/c ОР", project=NO_PROJECT, article=TRANSFER_ARTICLES[0]),
            entry("100000", "р/c СТ", side="credit", project=NO_PROJECT, article=TRANSFER_ARTICLES[1]),
        )
        result = check(proposed, text)
        self.assertEqual(result.action, "write")
        self.assertEqual([(e.amount, e.wallet, e.date) for e in result.entries],
                         [(-100000, "р/c ОР", DAY), (100000, "р/c СТ", DAY)])
        proposed["operations"][0]["entries"][1]["wallet"] = "Несуществующий счёт"
        self.assertEqual(check(proposed, text).reason, "wallet")

    def test_short_expenses_can_be_classified_into_different_projects(self):
        text = "- 47 000 комиссия Алибеку \n- 526 250 производство"
        rows = (
            entry("47000", "Константин С", project=SKP_PROJECT, article=COMMISSION_ARTICLE),
            entry("526250", "Константин С", project=PRODUCTION_PROJECT, article=PRODUCTION_ARTICLE),
        )
        reply = {"status": "completed", "output": [api_write(decision("expense", *rows))]}
        send = Mock(return_value=io.BytesIO(json.dumps(reply).encode()))
        result, _ = request_decision("test-key", DEFAULT_MODEL, CATALOG, text, DAY, SHARED_DDS_CHAT_ID,
                                     "ba_roshchupkin", send=send)
        payload = json.loads(send.call_args.args[0].data)
        for label in (SKP_PROJECT, COMMISSION_ARTICLE, PRODUCTION_ARTICLE):
            self.assertIn(label, payload["instructions"])
        self.assertEqual(result.action, "write")
        self.assertEqual([(e.amount, e.wallet, e.project, e.article) for e in result.entries],
                         [(-47000, "Константин С", SKP_PROJECT, COMMISSION_ARTICLE),
                          (-526250, "Константин С", PRODUCTION_PROJECT, PRODUCTION_ARTICLE)])

    def test_two_reported_incomes_have_independent_wallets(self):
        text = "+ 877 500 наличными от скп\n+ 122 500 на счет ор"
        first = operation("income", entry("877500", "Константин С", side="credit"))
        second = operation("income", entry("122500", "р/c ОР", side="credit"))
        result = check(batch(first, second), text)
        self.assertEqual(result.action, "write")
        self.assertEqual([(row.amount, row.wallet) for row in result.entries],
                         [(877500, "Константин С"), (122500, "р/c ОР")])
        second["entries"][0]["wallet"] = "Александр U"
        self.assertEqual(check(batch(first, second), text).reason, "wallet")

    def test_tax_and_exchange_are_three_rows_after_reply_without_a_repeat_question(self):
        original = "Счет ор - 57 140 - оплата налогов\nАлександр 2 140 = 2 000 $"
        tax = operation("expense", entry("57140", "р/c ОР"))
        exchange = operation(
            "exchange",
            entry("2140", "Александр U", currency="USDT", project=NO_PROJECT, article=EXCHANGE_ARTICLES[0]),
            entry("2000", "Александр D", currency="USD", side="credit",
                  project=NO_PROJECT, article=EXCHANGE_ARTICLES[1]),
        )
        reply = {"status": "completed", "output": [api_write(batch(tax, exchange), target="pending")]}
        pending = {"message": original, "message_date": DAY.isoformat(), "reason": "amount", "replies": []}
        result, _ = request_decision("test-key", DEFAULT_MODEL, CATALOG, "Обмен USDT на $",
                                     DAY, SHARED_DDS_CHAT_ID, pending=pending,
                                     send=Mock(return_value=io.BytesIO(json.dumps(reply).encode())))
        self.assertTrue(result.applies_to_pending)
        self.assertEqual(result.action, "write")
        self.assertEqual([(row.amount, row.currency, row.wallet) for row in result.entries],
                         [(-57140, "KGS", "р/c ОР"), (-2140, "USDT", "Александр U"), (2000, "USD", "Александр D")])

    def test_distinct_operations_can_have_the_same_amount(self):
        first = operation("expense", entry("700"))
        second = operation("expense", entry("700"))
        self.assertEqual(check(batch(first, second), "700 сом такси\n700 сом доставка").action, "write")

    def test_transfer_and_office_topup(self):
        good = check(transfer(), "Перевели 800 000 сом из наличных на СКП")
        self.assertEqual([(e.amount, e.wallet) for e in good.entries],
                         [(-800000, "Александр С"), (800000, "р/c СКП")])
        office = check(transfer("1500", "Офис подотчет"),
                       "13.11 - + 1.500 сом - пополнение в под отчёт\nОстаток: 2.216,74 сом")
        self.assertEqual([e.amount for e in office.entries], [-1500, 1500])

    def test_conversion_and_financing(self):
        exchange = decision(
            "exchange",
            entry("94300", project=NO_PROJECT, article=EXCHANGE_ARTICLES[0]),
            entry("1070", "Александр U", currency="USDT", side="credit",
                  project=NO_PROJECT, article=EXCHANGE_ARTICLES[1]),
        )
        self.assertEqual([e.amount for e in check(exchange, "Обменяли 94 300 сом на 1 070 USDT").entries],
                         [-94300, 1070])
        financing = decision(
            "financing", entry("30000", article=FINANCING_ARTICLES[0]),
            entry("4000", article=FINANCING_ARTICLES[1]),
        )
        self.assertEqual(check(financing, "Выплатили 30 000 сом тело и 4 000 сом проценты").action, "write")
        financing["operations"][0]["entries"].pop()
        self.assertEqual(check(financing, "Выплатили всего 34 000 сом").reason, "split")

    def test_normalized_model_amounts_do_not_depend_on_source_notation(self):
        for amount, source in (
            ("100", "+100наличными от ВЛ"),
            ("1150", "+1\u202f150 на счёт СТ"),
            ("120000", "+120к сом"),
            ("1200.50", "+1.200,50 сом"),
            ("100", "сто сом наличными"),
        ):
            with self.subTest(source=source):
                result = check(decision("income", entry(amount, side="credit")), source)
                self.assertEqual(result.action, "write")
                self.assertEqual(str(result.entries[0].amount), amount)

    def test_a_long_message_keeps_every_operation_from_the_model(self):
        text = "\n".join(f"+{i}с разными назначениями" for i in range(1, 21))
        raw = batch(*(operation("income", entry(str(i), side="credit")) for i in range(1, 21)))
        result = check(raw, text)
        self.assertEqual(result.action, "write")
        self.assertEqual([row.amount for row in result.entries], list(range(1, 21)))

    def test_compact_money_report_retains_every_operation_before_confirmation(self):
        text = ("100000 с рс скп на наличные\n\n"
                "120к наличных перевести на СКП и из наличных 4 300 сом на  70 юсдт поменять\n\n"
                "+ 100наличными от ВЛ\n+ 1150 на счет ст")
        raw = batch(
            operation("transfer", entry("100000", "р/c СКП"),
                      entry("100000", "Константин С", side="credit")),
            {**transfer("120000")["operations"][0], "status": "unconfirmed"},
            operation("exchange", entry("4300"),
                      entry("70", "Александр U", side="credit", currency="USDT"),
                      status="unconfirmed"),
            operation("income", entry("100", "Константин С", side="credit")),
            operation("income", entry("1150", "р/c СТ", side="credit")),
            action="confirm",
        )
        result = check(raw, text)
        self.assertEqual((result.action, result.completed_count), ("confirm", 4))
        self.assertEqual([row.amount for row in result.entries],
                         [-100000, 100000, 100, 1150, -120000, 120000, -4300, 70])

    def test_rejects_invalid_normalized_amounts_and_wayward_dates(self):
        for amount in ("0", "-1", "NaN", "1 500", "900000000000.123456"):
            with self.subTest(amount=amount):
                self.assertEqual(check(decision("expense", entry(amount)), "Оплатили").reason, "amount")
        self.assertEqual(check(decision("expense", entry("1500", day=date(2026, 11, 14))),
                               "Оплатили 1500 сом").reason, "date")

    def test_normalized_dates_come_from_model_or_the_message_date(self):
        historical = date(2026, 11, 12)
        raw = decision("expense", entry("1500", day=historical))
        self.assertEqual(check(raw, "Вчера оплатили 1500 сом").entries[0].date, historical)
        raw["operations"][0]["entries"][0]["date"] = ""
        self.assertEqual(check(raw, "Оплатили 1500 сом").entries[0].date, DAY)
        raw["operations"][0]["entries"][0]["date"] = "2026-11-31"
        self.assertEqual(check(raw, "Оплатили 1500 сом").reason, "date")

    def test_keeps_the_expense_when_classification_is_missing_or_invalid(self):
        text = "- 2000 - компенсация за встречу с финансовым партнёром"
        for raw in (
            decision("expense", entry("2000", project="", article="")),
            decision("expense", entry("2000", article="Доходы с производства")),
        ):
            with self.subTest(raw=raw):
                result = check(raw, text)
                self.assertEqual(result.action, "write")
                saved = result.entries[0]
                self.assertEqual((saved.amount, saved.wallet, saved.project, saved.article, saved.review_category),
                                 (-2000, "Александр С", "", "", False))

    def test_unknown_wallet_does_not_prevent_ordinary_amount_from_being_recorded(self):
        result = check(decision("expense", entry("1500", wallet="", project="", article="")),
                       "-1500 оборудование")
        self.assertEqual(result.action, "write")
        self.assertEqual(result.entries[0].wallet, "")

    def test_model_recognized_report_does_not_need_a_sign_or_status_keyword(self):
        result = check(decision("expense", entry("2000", project="", article="")),
                       "Эрланову 2000 компенсация за встречу с ФП")
        self.assertEqual((result.action, result.entries[0].amount), ("write", -2000))

    def test_responses_request_redacts_secrets_and_reports_usage(self):
        reply = {"status": "completed", "usage": {"input_tokens": 200, "output_tokens": 50},
                 "output": [api_write(decision("expense", entry("1500")))]}
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

    def test_model_can_write_after_the_optional_history_call(self):
        call = {"type": "function_call", "call_id": "call_1", "name": "search_dds_history",
                "arguments": json.dumps({"query": "выездная встреча с ФП"})}
        raw = decision("expense", entry("1500"))
        replies = [
            {"status": "completed", "output": [call], "usage": {"input_tokens": 200, "output_tokens": 15}},
            {"status": "completed", "usage": {"input_tokens": 240, "output_tokens": 30},
             "output": [api_write(raw)]},
        ]
        send = Mock(side_effect=[io.BytesIO(json.dumps(reply).encode()) for reply in replies])
        search = Mock(return_value=[{"purpose": "встреча с фп", "project": PROJECT, "article": "1. Построения"}])
        reserve = Mock(return_value=True)
        result, usage = request_decision("test-key", DEFAULT_MODEL, CATALOG, "Оплатили 1500 сом за встречу",
                                         DAY, SHARED_DDS_CHAT_ID, "n0visad", search_history=search,
                                         reserve_search_call=reserve, send=send)
        self.assertEqual((result.action, usage), ("write", {"input_tokens": 440, "output_tokens": 45}))
        reserve.assert_called_once()
        search.assert_called_once_with("выездная встреча с ФП")
        second = json.loads(send.call_args.args[0].data)
        self.assertFalse(second["store"])
        self.assertEqual([tool["name"] for tool in second["tools"]], ["write_dds"])
        self.assertEqual(second["input"][-2], call)
        self.assertEqual(json.loads(second["input"][-1]["output"])[0]["project"], PROJECT)

    def test_display_response_cannot_write_without_a_model_tool_call(self):
        reply = {"status": "completed", "output": [api_message(decision("expense", entry("1500")))]}
        with self.assertRaisesRegex(ValueError, "Only the write tool"):
            request_decision("test-key", DEFAULT_MODEL, CATALOG, "Оплатили 1500", DAY, SHARED_DDS_CHAT_ID,
                             send=Mock(return_value=io.BytesIO(json.dumps(reply).encode())))

    def test_write_tool_provides_a_visible_note_for_operations_left_out(self):
        note = "Не внёс обмен: его отменили."
        reply = {"status": "completed", "output": [api_write(decision("expense", entry("1500")), note=note)]}
        result, _ = request_decision("test-key", DEFAULT_MODEL, CATALOG, "Оплатили 1500", DAY,
                                     SHARED_DDS_CHAT_ID,
                                     send=Mock(return_value=io.BytesIO(json.dumps(reply).encode())))
        self.assertEqual((result.action, result.write_note), ("write", note))

    def test_history_search_budget_prevents_additional_paid_request(self):
        call = {"type": "function_call", "call_id": "call_1", "name": "search_dds_history",
                "arguments": '{"query":"доставка"}'}
        send = Mock(return_value=io.BytesIO(json.dumps({"status": "completed", "output": [call]}).encode()))
        search = Mock()
        result, _ = request_decision("test-key", DEFAULT_MODEL, CATALOG, "1500 сом оплата доставки",
                                     DAY, SHARED_DDS_CHAT_ID, search_history=search,
                                     reserve_search_call=lambda: False, send=send)
        self.assertEqual(result.reason, "context_budget")
        send.assert_called_once()
        search.assert_not_called()

    def test_model_not_keyword_filter_skips_approval_conversation_and_balances(self):
        for text in ("Остаток: 1000 сом", "Согласуйте оплату 1500 за доставку", "Да, согласовано"):
            with self.subTest(text=text):
                model_skip = {"status": "completed", "output": [api_message(batch(action="skip"))]}
                send = Mock(return_value=io.BytesIO(json.dumps(model_skip).encode()))
                result, _ = request_decision("test-key", DEFAULT_MODEL, CATALOG, text, DAY,
                                             SHARED_DDS_CHAT_ID, send=send)
                self.assertEqual((result.action, result.entries), ("skip", ()))
                send.assert_called_once()

    def test_actual_expense_can_follow_approval_without_copying_its_amount(self):
        raw = decision("expense", entry("1500"))
        fake_response = {"status": "completed", "output": [api_write(raw)]}
        result, _ = request_decision(
            "test-key", DEFAULT_MODEL, CATALOG, "- 1 500 за доставку", DAY, SHARED_DDS_CHAT_ID,
            "n0visad", parent_text="Да, согласовано 2000",
            send=Mock(return_value=io.BytesIO(json.dumps(fake_response).encode())),
        )
        self.assertEqual([e.amount for e in result.entries], [-1500])
        self.assertEqual(check(decision("expense", entry("1500")),
                               "Согласовали и оплатили 1500 за доставку").action, "write")

    def test_article_context_uses_the_current_catalog_and_keeps_official_description(self):
        catalog = Catalog({}, {}, {"3. Финансов": {"group": "Выбытие", "description": "Описание из ДДС"}})
        details = catalog.prompt_data()["article_details"]
        self.assertEqual(set(details), {"3. Финансов"})
        self.assertEqual(details["3. Финансов"]["description"], "Описание из ДДС")
        self.assertIn("Банковские комиссии", details["3. Финансов"]["usage_hint"])
        self.assertEqual(len(ARTICLE_HINTS), 17)


class WriterTests(unittest.TestCase):
    def test_sheet_wallet_not_in_code_is_accepted_without_article_metadata(self):
        tabs = {
            SHEET_NAME: SimpleNamespace(row_count=100),
            WALLET_TAB: SimpleNamespace(col_values=Mock(return_value=["Месяц", "Кошелек", "Фонд команды"])),
            ARTICLE_TAB: SimpleNamespace(get=Mock(return_value=[])),
            SUMMARY_TAB: SimpleNamespace(col_values=Mock(return_value=[])),
        }
        book = SimpleNamespace(worksheet=tabs.__getitem__)
        catalog = read_catalog(book)
        self.assertEqual(catalog.wallets, {"Фонд команды": None})
        raw = decision("expense", entry("1500", "Фонд команды"))
        result = validate_response(raw, "Из фонда команды оплатили 1500 сом", DAY, catalog)
        self.assertEqual((result.action, result.entries[0].wallet, result.entries[0].article),
                         ("write", "Фонд команды", ""))

    def test_history_uses_reviewed_neighbours_and_ignores_unreviewed_or_old_autofill(self):
        source = -1001234567
        rows = [
            [0, -100, "Александр С", "", "Выездная встреча с ФП\nhttps://t.me/c/1234567/1",
             PROJECT, "1. Построения"],
            [0, -200, "Александр С", "", "Представительские расходы\nhttps://t.me/c/1234567/2",
             PROJECT, "1. Построения"],
            [0, -300, "Александр С", "", "Компенсация расходов на встречу\nhttps://t.me/c/1234567/3",
             PROJECT, "1. Построения"],
        ]
        sheet = SimpleNamespace(id=0, row_count=100, get=Mock(return_value=rows))
        book = SimpleNamespace(id=PILOT_SPREADSHEET_ID, worksheet=Mock(return_value=sheet),
                               fetch_sheet_metadata=Mock())
        writer = SolDdsWriter(book)
        clean = [{"formattedValue": PROJECT}, {"formattedValue": "1. Построения"}]
        yellow = [{"formattedValue": PROJECT, "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": {"red": 1.0, "green": 0.949, "blue": 0.8}},
        }}, clean[1]]
        book.fetch_sheet_metadata.return_value = {"sheets": [{"data": [
            {"startRow": 4, "startColumn": 8, "rowData": [{"values": clean}, {"values": yellow}]}
        ]}]}
        _, examples = writer.inspect_message(source, 4, "-2000 компенсация встречи с ФП", CATALOG, {2, 3})
        self.assertEqual([e["purpose"] for e in examples], ["представительские расходы"])
        self.assertNotIn("I4:J4", str(book.fetch_sheet_metadata.call_args))

    def test_inferred_pair_is_yellow_only_in_project_and_article(self):
        result = check(decision("expense", entry("1500")), "-1500 обслуживание офиса")
        requests = build_entry_requests(0, 21, result.entries, "https://t.me/c/1234567/9")
        paint = requests[-1]["repeatCell"]
        self.assertEqual((paint["range"]["startColumnIndex"], paint["range"]["endColumnIndex"]), (8, 10))
        explicit = check(decision("expense", entry("1500")),
                         f"-1500 обслуживание офиса\nПроект: {PROJECT}. Статья: 1. Построения.")
        self.assertFalse(explicit.entries[0].review_category)

    def test_write_accepts_sheets_trimming_empty_classification_cells(self):
        entries = check(decision("expense", entry("1500", project="", article="")),
                        "-1500 компенсация").entries
        sheet = SimpleNamespace(id=0, row_count=100, get=Mock())
        book = SimpleNamespace(id=PILOT_SPREADSHEET_ID, worksheet=Mock(return_value=sheet),
                               batch_update=Mock())
        writer = SolDdsWriter(book)
        request = build_entry_requests(0, 9, entries, "https://t.me/c/1234567/81")[0]["updateCells"]
        trimmed = [[next(iter(c.get("userEnteredValue", {}).values()), "")
                    for c in request["rows"][0]["values"][:5]]]
        sheet.get.side_effect = [[], [[6]], trimmed]
        self.assertEqual(writer.write(-1001234567, 81, entries), ("written", [9]))

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
            SolDdsWriter(SimpleNamespace(id=SHARED_DDS_SPREADSHEET_ID))
        chat = -1001234567
        sheet = SimpleNamespace(id=0, row_count=100, get=Mock(return_value=[
            ["", "", "", "", "Оплата\nhttps://t.me/c/1234567/81"],
        ]))
        book = SimpleNamespace(id=PILOT_SPREADSHEET_ID, worksheet=Mock(return_value=sheet), batch_update=Mock())
        writer = SolDdsWriter(book)
        entries = check(transfer(), "Перевели 800 000 сом из наличных на СКП").entries
        self.assertEqual(writer.seen(chat, 81), [4])
        self.assertEqual(writer.write(chat, 81, entries), ("review", [4]))
        book.batch_update.assert_not_called()

    def test_retry_after_successful_batch_with_lost_response_does_not_write_twice(self):
        entries = check(transfer(), "Перевели 800 000 сом из наличных на СКП").entries
        sheet = SimpleNamespace(id=0, row_count=100, get=Mock())
        book = SimpleNamespace(id=PILOT_SPREADSHEET_ID, worksheet=Mock(return_value=sheet), batch_update=Mock())
        writer = SolDdsWriter(book)
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


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_wallet_correction_keeps_the_dialogue_open_until_model_writes(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as cleanup:
            queue = Queue(str(Path(temp) / "continued.sqlite"))
            cleanup.callback(queue.db.close)
            writer = Mock()
            writer.inspect_message.return_value = ([], ())
            writer.write.return_value = ("written", [9, 10, 11, 12, 13])
            bot = SimpleNamespace(
                _post=AsyncMock(return_value=True),
                send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=82), SimpleNamespace(message_id=84)]),
            )
            pilot = SolWorker(queue, writer, "test-key", DEFAULT_MODEL, -1001234567, SHARED_DDS_CHAT_ID)
            pilot.catalog, pilot.catalog_until = CATALOG, time.monotonic() + 60
            draft = check(batch(*mixed_request()["operations"], receipts(), action="confirm"), REQUEST_AND_RECEIPT)
            wallets = {"Александр С": "Константин С", "Александр U": "Константин U"}
            corrected = replace(draft, applies_to_pending=True, question="Перевод и обмен уже провели?",
                                entries=tuple(replace(e, wallet=wallets.get(e.wallet, e.wallet))
                                              for e in draft.entries))
            confirmed = replace(corrected, action="write", question="")
            outcomes = [(draft, {}), (corrected, {}), (confirmed, {})]
            with patch("skp_ai_worker.request_decision", side_effect=outcomes) as model:
                queue.enqueue(81, 1, "ba_roshchupkin", DAY, REQUEST_AND_RECEIPT)
                await pilot.process(SimpleNamespace(bot=bot))
                queue.enqueue(83, 1, "ba_roshchupkin", DAY, "Во всех переводах кошельки Константина", reply_to_id=82)
                await pilot.process(SimpleNamespace(bot=bot))
                self.assertEqual(queue.get(81)["state"], "confirming")
                self.assertEqual(queue.get(83)["state"], "answered")
                self.assertEqual(bot.send_message.await_count, 2)
                self.assertIn(corrected.question, bot.send_message.await_args.args[1])
                self.assertIn("Константин U", bot.send_message.await_args.args[1])
                writer.write.assert_not_called()
                bot._post.assert_not_awaited()
                queue.enqueue(85, 1, "ba_roshchupkin", DAY, "Да, перевод и обмен сделали", reply_to_id=82)
                await pilot.process(SimpleNamespace(bot=bot))
            pending = model.call_args.kwargs["pending"]
            self.assertEqual(pending["dialogue"][0]["answer"], "Во всех переводах кошельки Константина")
            self.assertIn(corrected.question, pending["question"])
            self.assertEqual(queue.get(81)["state"], "written")
            self.assertEqual(queue.get(85)["state"], "answered")
            writer.write.assert_called_once_with(-1001234567, 81, confirmed.entries)
            self.assertEqual(bot.send_message.await_count, 2)
            self.assertEqual(bot._post.await_args.args[1]["message_id"], 85)

    async def test_combined_report_waits_for_one_question_then_writes_once(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as cleanup:
            queue = Queue(str(Path(temp) / "queue.sqlite"))
            cleanup.callback(queue.db.close)
            writer = Mock()
            writer.inspect_message.return_value = ([], ())
            writer.write.return_value = ("written", [9])
            bot = SimpleNamespace(_post=AsyncMock(return_value=True),
                                  send_message=AsyncMock(return_value=SimpleNamespace(message_id=82)))
            pilot = SolWorker(queue, writer, "test-key", DEFAULT_MODEL, -1001234567, SHARED_DDS_CHAT_ID)
            pilot.catalog, pilot.catalog_until = CATALOG, time.monotonic() + 60
            draft = check(batch(*mixed_request()["operations"], receipts(), action="confirm"), REQUEST_AND_RECEIPT)
            confirmed = check(batch(receipts()), REQUEST_AND_RECEIPT)
            reply = Decision("write", confirmed.reason, confirmed.entries, applies_to_pending=True)
            with patch("skp_ai_worker.request_decision", side_effect=[(draft, {}), (reply, {})]) as model:
                queue.enqueue(81, 1, "ba_roshchupkin", DAY, REQUEST_AND_RECEIPT)
                await pilot.process(SimpleNamespace(bot=bot))
                self.assertEqual(queue.get(81)["state"], "confirming")
                writer.write.assert_not_called()
                queue.enqueue(83, 1, "ba_roshchupkin", DAY, "Нет, их пока не проводили")
                await pilot.process(SimpleNamespace(bot=bot))
            pending = model.call_args.kwargs["pending"]
            self.assertEqual(len(pending["completed_entries"]), 1)
            self.assertEqual(len(pending["proposed_entries"]), 4)
            writer.write.assert_called_once_with(-1001234567, 81, confirmed.entries)
            bot.send_message.assert_awaited_once()
            self.assertEqual(queue.get(81)["state"], "written")

    async def test_model_selected_transfer_writes_both_sides_without_question(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Queue(str(Path(temp) / "queue.sqlite"))
            try:
                queue.enqueue(81, 1, "n0visad", DAY, "100000 с рс ор на рс ст")
                writer = Mock()
                writer.inspect_message.return_value = ([], ())
                writer.write.return_value = ("written", [9, 10])
                bot = SimpleNamespace(_post=AsyncMock(return_value=True), send_message=AsyncMock())
                pilot = SolWorker(queue, writer, "test-key", DEFAULT_MODEL, -1001234567, SHARED_DDS_CHAT_ID)
                pilot.catalog, pilot.catalog_until = CATALOG, time.monotonic() + 60
                result = check(decision(
                    "transfer",
                    entry("100000", "р/c ОР", project=NO_PROJECT, article=TRANSFER_ARTICLES[0]),
                    entry("100000", "р/c СТ", side="credit", project=NO_PROJECT,
                          article=TRANSFER_ARTICLES[1]),
                ), "100000 с рс ор на рс ст")
                with patch("skp_ai_worker.request_decision", return_value=(result, {})) as api:
                    await pilot.process(SimpleNamespace(bot=bot))
                    api.assert_called_once()
                saved = writer.write.call_args.args[2]
                self.assertEqual([(e.amount, e.wallet, e.date) for e in saved],
                                 [(-100000, "р/c ОР", DAY), (100000, "р/c СТ", DAY)])
                self.assertEqual(queue.get(81)["state"], "written")
                bot.send_message.assert_not_awaited()
            finally:
                queue.db.close()

    async def test_one_question_and_plain_answer_without_consuming_an_unrelated_report(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as cleanup:
            queue = Queue(str(Path(temp) / "queue.sqlite"))
            cleanup.callback(queue.db.close)
            writer = Mock()
            writer.inspect_message.return_value = ([], ())
            writer.write.return_value = ("written", [9, 10])
            bot = SimpleNamespace(_post=AsyncMock(return_value=True),
                                  send_message=AsyncMock(return_value=SimpleNamespace(message_id=82)))
            pilot = SolWorker(queue, writer, "test-key", DEFAULT_MODEL, -1001234567, SHARED_DDS_CHAT_ID)
            pilot.catalog, pilot.catalog_until = CATALOG, time.monotonic() + 60
            question = Decision("clarify", "wallet", question="На какой расчётный счёт перевели?")
            answers = [question, Decision("skip", "model_skip"),
                       Decision("write", "transfer",
                                check(transfer(), "Перевели 800 000 сом из наличных на СКП").entries,
                                applies_to_pending=True)]
            with patch("skp_ai_worker.request_decision", side_effect=[(answer, {}) for answer in answers]) as api:
                queue.enqueue(81, 1, "n0visad", DAY, "800 000 из наличных на расчётный счёт")
                await pilot.process(SimpleNamespace(bot=bot))
                self.assertEqual(queue.get(81)["state"], "waiting")
                queue.enqueue(83, 1, "n0visad", DAY, "Спасибо, увидел")
                await pilot.process(SimpleNamespace(bot=bot))
                self.assertEqual(queue.get(81)["state"], "waiting")
                queue.enqueue(84, 1, "n0visad", DAY, "На тот, который открыт для СКП")
                await pilot.process(SimpleNamespace(bot=bot))
            self.assertEqual(api.call_args.kwargs["pending"]["question"], question.question)
            self.assertEqual(queue.get(81)["state"], "written")
            self.assertEqual(queue.get(84)["state"], "answered")
            self.assertEqual(writer.write.call_args.args[1], 81)
            self.assertEqual(bot._post.await_args.args[1]["message_id"], 84)
            bot.send_message.assert_awaited_once_with(-1001234567, question.question, reply_to_message_id=81)
            matching = "620 000 из наличных на расчётный счёт"
            remembered = queue.recall_clarifications(1, matching, exclude_id=85)
            self.assertEqual(remembered[0]["accounts"][1]["wallet"], "р/c СКП")
            self.assertNotIn("800", json.dumps(remembered))
            self.assertEqual(queue.recall_clarifications(2, matching, exclude_id=85), ())
            self.assertEqual(queue.recall_clarifications(1, matching, exclude_id=81), ())
            queue.edit(81, "Указал не тот кошелёк")
            self.assertEqual(queue.recall_clarifications(1, matching, exclude_id=85), ())

    async def test_question_survives_a_transport_error_without_another_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Queue(str(Path(temp) / "queue.sqlite"))
            try:
                queue.enqueue(81, 1, "n0visad", DAY, "За доставку вчера пришла сумма, какая неясно")
                writer = Mock()
                writer.inspect_message.return_value = ([], ())
                bot = SimpleNamespace(_post=AsyncMock(), send_message=AsyncMock(
                    side_effect=[TimeoutError(), SimpleNamespace(message_id=82)],
                ))
                pilot = SolWorker(queue, writer, "test-key", DEFAULT_MODEL, -1001234567, SHARED_DDS_CHAT_ID)
                pilot.catalog, pilot.catalog_until = CATALOG, time.monotonic() + 60
                question = Decision("clarify", "amount", question="Сколько пришло за доставку?")
                with patch("skp_ai_worker.request_decision", return_value=(question, {})) as model:
                    await pilot.process(SimpleNamespace(bot=bot))
                    self.assertEqual(queue.get(81)["state"], "asking")
                    with queue.db:
                        queue.db.execute("UPDATE events SET next_try=0 WHERE id=81")
                    await pilot.process(SimpleNamespace(bot=bot))
                model.assert_called_once()
                self.assertEqual(queue.get(81)["state"], "waiting")
                self.assertEqual(queue.get(81)["question_reason"], "amount")
                writer.write.assert_not_called()
                self.assertEqual(bot.send_message.await_count, 2)
                self.assertEqual(bot.send_message.await_args.args[1], question.question)
            finally:
                queue.db.close()

    async def test_verified_write_reacts_to_confirmation_reply_even_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "queue.sqlite")
            queue = Queue(path)
            queue.enqueue(81, 1, "n0visad", DAY, MIXED_REQUEST)
            draft = check(mixed_request(), MIXED_REQUEST)
            queue.stage_question(queue.get(81), draft, "Уже провели?")
            queue.question_delivered(queue.get(81), 82)
            queue.enqueue(83, 1, "n0visad", DAY, "Деньги ушли")
            confirmation = Decision("write", "mixed", check(mixed_request(), MIXED_REQUEST).entries)
            self.assertTrue(queue.resolve_followup(queue.get(81), queue.get(83), confirmation))
            queue.db.close()
            queue = Queue(path)
            try:
                writer = Mock()
                writer.write.return_value = ("written", [9, 10, 11, 12])
                pilot = SolWorker(queue, writer, "test-key", DEFAULT_MODEL, -1001234567, SHARED_DDS_CHAT_ID)
                bot = SimpleNamespace(_post=AsyncMock(return_value=True), send_message=AsyncMock())
                await pilot.process(SimpleNamespace(bot=bot))
                bot._post.assert_awaited_once_with("setMessageReaction", {
                    "chat_id": -1001234567, "message_id": 83,
                    "reaction": [{"type": "emoji", "emoji": "\N{THUMBS UP SIGN}"}],
                })
                bot.send_message.assert_not_awaited()
                self.assertEqual(queue.get(81)["state"], "written")
                self.assertEqual(writer.write.call_args.args[1], 81)
            finally:
                queue.db.close()

    async def test_reaction_fallback_does_not_retry_writes_or_acknowledge_review(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Queue(str(Path(temp) / "queue.sqlite"))
            try:
                entries = check(transfer(), "Перевели 800 000 сом из наличных на СКП").entries
                writer = Mock()
                writer.write.side_effect = [("already_present", [9, 10]), ("review", [11])]
                bot = SimpleNamespace(_post=AsyncMock(side_effect=RuntimeError), send_message=AsyncMock())
                pilot = SolWorker(queue, writer, "test-key", DEFAULT_MODEL, -1001234567, SHARED_DDS_CHAT_ID)
                for message_id in (81, 82):
                    queue.enqueue(message_id, 1, "n0visad", DAY, "Перевели 800 000 сом из наличных на СКП")
                    queue.stage_write(message_id, entries)
                    await pilot.process(SimpleNamespace(bot=bot))
                self.assertEqual(queue.get(81)["state"], "already_present")
                self.assertEqual(queue.get(82)["state"], "review")
                bot._post.assert_awaited_once()
                self.assertEqual(bot.send_message.await_count, 2)
                self.assertEqual(bot.send_message.await_args_list[0].args[1], "Записано в ДДС.")
                self.assertIn("Не записано", bot.send_message.await_args_list[1].args[1])
                self.assertEqual(writer.write.call_count, 2)
                self.assertIsNone(queue.next())
            finally:
                queue.db.close()

    async def test_model_skip_does_not_write_or_send_a_message(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Queue(str(Path(temp) / "queue.sqlite"))
            try:
                queue.enqueue(81, 1, "n0visad", DAY, "Согласуйте оплату 1500 за доставку")
                writer = Mock()
                writer.inspect_message.return_value = ([], ())
                pilot = SolWorker(queue, writer, "test-key", DEFAULT_MODEL, -1001234567, SHARED_DDS_CHAT_ID)
                pilot.catalog, pilot.catalog_until = CATALOG, time.monotonic() + 60
                context = Mock()
                with patch("skp_ai_worker.request_decision", return_value=(Decision("skip", "model_skip"), {})) as model:
                    await pilot.process(context)
                self.assertEqual(queue.get(81)["state"], "skipped")
                model.assert_called_once()
                writer.write.assert_not_called()
                self.assertEqual(context.mock_calls, [])
            finally:
                queue.db.close()


class QueueTests(unittest.TestCase):
    def test_plain_answer_requires_one_recent_question_by_author_or_a_reply(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Queue(str(Path(temp) / "queue.sqlite"))
            try:
                queue.enqueue(81, 1, "n0visad", DAY, "+1000 сом за кэш-флоу")
                queue.finish(81, "waiting", question_id=82, question_reason="wallet")
                queue.enqueue(83, 2, "other", DAY, "рс ОР", reply_to_id=82)
                self.assertIsNone(queue.pending_for(queue.get(83)))
                queue.enqueue(84, 1, "n0visad", DAY, "Тот счёт, о котором писали раньше")
                self.assertEqual(queue.pending_for(queue.get(84))["id"], 81)
                queue.enqueue(85, 1, "n0visad", DAY, "+2000 сом за кэш-флоу")
                queue.finish(85, "waiting", question_id=86, question_reason="wallet")
                queue.enqueue(87, 1, "n0visad", DAY, "рс ОР")
                self.assertIsNone(queue.pending_for(queue.get(87)))
                queue.enqueue(88, 1, "n0visad", DAY, "рс ОР", reply_to_id=82)
                self.assertEqual(queue.pending_for(queue.get(88))["id"], 81)
            finally:
                queue.db.close()

    def test_restart_keeps_pending_operation_reply_and_staged_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "queue.sqlite")
            queue = Queue(path)
            queue.enqueue(81, 1, "n0visad", DAY, "Оплатили 1500")
            queue.finish(81, "waiting", question_id=82)
            queue.enqueue(83, 1, "n0visad", DAY, "Александр С", reply_to_id=82)
            queue.db.close()
            queue = Queue(path)
            entries = check(transfer(), "Перевели 800 000 сом из наличных на СКП").entries
            answer = queue.next()
            self.assertTrue(queue.resolve_followup(queue.pending_for(answer), answer,
                                                  Decision("write", "transfer", entries)))
            self.assertEqual(json.loads(queue.next()["replies"]), ["Александр С"])
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
