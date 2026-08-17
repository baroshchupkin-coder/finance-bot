import unittest

from receipt_ocr import choose_payment_candidate


class ReceiptOcrDecisionTests(unittest.TestCase):
    def test_accepts_single_currency_amount(self):
        decision = choose_payment_candidate("Оплата студии", "Платеж выполнен\n8 709,00 KGS")
        self.assertEqual(str(decision.candidate.amount), "-8709.00")
        self.assertEqual(decision.candidate.currency, "KGS")
        self.assertEqual(decision.confidence, "high")

    def test_prefers_total_over_commission(self):
        decision = choose_payment_candidate(
            "Госпошлина",
            "Сумма операции 30 000 сом\nКомиссия 300 сом\nОстаток 12 500 сом",
        )
        self.assertEqual(str(decision.candidate.amount), "-30000")

    def test_rejects_multiple_unlabelled_amounts(self):
        decision = choose_payment_candidate("Расходы", "23,80 $\n62,18 $\n35,53 $")
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.reason, "ambiguous_amounts")

    def test_uses_chat_currency_only_for_labelled_total(self):
        decision = choose_payment_candidate("Оплата", "Итого 4565,72", default_currency="KGS")
        self.assertEqual(str(decision.candidate.amount), "-4565.72")
        self.assertEqual(decision.candidate.currency, "KGS")

    def test_does_not_guess_bare_unlabelled_number(self):
        decision = choose_payment_candidate("Оплата", "Карта 1234\n05.08.2026", default_currency="KGS")
        self.assertIsNone(decision.candidate)


if __name__ == "__main__":
    unittest.main()
