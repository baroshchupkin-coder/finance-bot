import unittest
from datetime import date
from unittest.mock import patch

from shared_dds import SHARED_DDS_CHAT_ID, SHARED_DDS_OFFICE_CHAT_ID
from skp_ai import Decision
from skp_ai_runtime import (
    DurableQueue, STATE_HEADER, STATE_ROWS, SheetJournal, start_skp_sol,
)


DAY = date(2026, 11, 16)


class SheetDouble:
    def __init__(self):
        self.rows = {1: [STATE_HEADER]}
        self.fail_after_write = False

    def row_values(self, index):
        return self.rows.get(index, [])[:]

    def update(self, *, range_name, values, raw):
        assert raw
        index = int(range_name[1:])
        old = self.rows.get(index, [])
        self.rows[index] = [str(v) for v in values[0]] + old[len(values[0]):]
        if self.fail_after_write:
            self.fail_after_write = False
            raise TimeoutError("commit may have succeeded")


class BookDouble:
    def __init__(self):
        self.sheet = SheetDouble()

    def worksheet(self, name):
        return self.sheet


class DurableQueueTests(unittest.TestCase):
    def queue(self, journal, chat_id):
        queue = DurableQueue(journal, chat_id)
        self.addCleanup(queue.queue.db.close)
        return queue

    def test_dialogue_and_equal_message_ids_survive_restart_in_separate_chats(self):
        book = BookDouble()
        original = SheetJournal(book)
        general = self.queue(original, SHARED_DDS_CHAT_ID)
        office = self.queue(original, SHARED_DDS_OFFICE_CHAT_ID)
        general.call("enqueue", 11, 7, "n0visad", DAY, "Перевести на СКП")
        office.call("enqueue", 11, 7, "n0visad", DAY, "Купили бумагу")
        pending = Decision("clarify", "amount", question="Сколько перевели?")
        office_amount = Decision("clarify", "amount", question="Сколько стоила бумага?")
        general.call("stage_question", general.call("get", 11), pending, pending.question)
        general.call("question_delivered", general.call("get", 11), 12)
        office.call("stage_question", office.call("get", 11), office_amount, office_amount.question)
        office.call("question_delivered", office.call("get", 11), 12)

        restarted = SheetJournal(book)
        general = self.queue(restarted, SHARED_DDS_CHAT_ID)
        office = self.queue(restarted, SHARED_DDS_OFFICE_CHAT_ID)
        general.call("enqueue", 13, 7, "n0visad", DAY, "Сумма 1500", reply_to_id=12)
        office.call("enqueue", 13, 7, "n0visad", DAY, "100 сом", reply_to_id=12)
        self.assertEqual(general.call("pending_for", general.call("get", 13))["question_text"], pending.question)
        self.assertEqual(office.call("pending_for", office.call("get", 13))["question_text"], office_amount.question)

    def test_ambiguous_storage_commit_is_reloaded_before_retry(self):
        book = BookDouble()
        journal = SheetJournal(book)
        queue = self.queue(journal, SHARED_DDS_CHAT_ID)
        book.sheet.fail_after_write = True
        with self.assertRaises(TimeoutError):
            queue.call("enqueue", 11, 7, "n0visad", DAY, "1500 доставка")
        queue.call("enqueue", 11, 7, "n0visad", DAY, "1500 доставка")
        persisted = journal.load(STATE_ROWS[SHARED_DDS_CHAT_ID], str(SHARED_DDS_CHAT_ID), {})
        self.assertEqual(len(persisted["events"]), 1)
        self.assertEqual(queue.call("next")["body"], "1500 доставка")

    def test_call_budget_is_shared_and_survives_restart(self):
        book = BookDouble()
        journal = SheetJournal(book)
        general = self.queue(journal, SHARED_DDS_CHAT_ID)
        office = self.queue(journal, SHARED_DDS_OFFICE_CHAT_ID)
        with patch("skp_ai_runtime.CALLS_PER_MONTH", 2):
            self.assertTrue(general.call("claim_call"))
            self.assertTrue(office.call("claim_call"))
            restarted = self.queue(SheetJournal(book), SHARED_DDS_OFFICE_CHAT_ID)
            self.assertFalse(restarted.call("claim_call"))

    def test_existing_parser_is_the_default(self):
        with patch("skp_ai_runtime.SkpSolRuntime") as runtime:
            self.assertIsNone(start_skp_sol(object(), {}))
            self.assertIsNone(start_skp_sol(object(), {"SKP_DDS_MODE": "legacy"}))
        runtime.assert_not_called()
