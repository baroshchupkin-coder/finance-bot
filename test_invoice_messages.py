import ast
import re
import unittest
from pathlib import Path


class InvoiceMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(Path(__file__).with_name("bot.py").read_text(encoding="utf-8"))
        names = {
            "get_cell", "format_user_tag", "format_invoice_creator", "get_invoice_payer_tag",
            "build_pending_approval_invoice_text", "build_approved_approval_text",
            "build_payment_invoice_text", "build_paid_invoice_text",
        }
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        cls.messages = {
            "re": re, "REQUEST_ID_COL": 0, "APPROVER_NAME_COL": 12, "PAYER_TAG_COL": 13,
            "PAYMENT_PROJECT_LABEL_KEYS": {"VL"}, "build_invoice_details": lambda row: row[6],
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot.py", "exec"), cls.messages)

    def setUp(self):
        self.row = [""] * 14
        self.row[0], self.row[2], self.row[3] = "892", "creator", "VL"
        self.row[6] = "Бюджет рекламного кабинета"
        self.row[11:14] = ["Имя автора", "approver", "@assigned_payer"]

    def test_creator_is_shown_before_and_after_approval(self):
        for stage in ("build_pending_approval_invoice_text", "build_approved_approval_text"):
            with self.subTest(stage=stage):
                self.assertTrue(self.messages[stage](self.row).endswith("Отправлено: @creator"))

    def test_payment_message_shows_creator_and_actual_approver(self):
        text = self.messages["build_payment_invoice_text"](self.row)
        self.assertTrue(text.startswith("@assigned_payer\nПроект: VL\n"))
        self.assertTrue(text.endswith("Отправлено: @creator\nСогласовано: @approver"))

    def test_paid_message_keeps_creator_and_shows_actual_payer(self):
        self.row[2] = ""
        text = self.messages["build_paid_invoice_text"](self.row, "@actual_payer")
        self.assertTrue(text.endswith(
            "Отправлено: Имя автора\nСогласовано: @approver\nОплачено: @actual_payer"
        ))


if __name__ == "__main__":
    unittest.main()
