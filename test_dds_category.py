import unittest
from decimal import Decimal

from dds_category import CategoryExample, DdsCategoryClassifier


class DdsCategoryClassifierTests(unittest.TestCase):
    def test_exact_description_sign_and_wallet_is_automatic(self):
        classifier = DdsCategoryClassifier([
            CategoryExample(
                description="-300 сом - доставка брендированных футболок",
                amount=Decimal("-300"),
                wallet="Офис подотчет",
                article="Доп. работы отдела построения",
            ),
        ])

        prediction = classifier.predict(
            "-450 сом - доставка брендированных футболок",
            Decimal("-450"),
            "Офис подотчет",
        )

        self.assertEqual(prediction.status, "auto")
        self.assertEqual(prediction.article, "Доп. работы отдела построения")

    def test_amount_sign_prevents_income_from_matching_expense(self):
        classifier = DdsCategoryClassifier([
            CategoryExample(
                description="Возврат подотчета",
                amount=Decimal("-1000"),
                wallet="Офис подотчет",
                article="Подотчетные средства",
            ),
        ])

        prediction = classifier.predict(
            "Возврат подотчета",
            Decimal("1000"),
            "Офис подотчет",
        )

        self.assertEqual(prediction.status, "review")

    def test_similar_description_is_suggested_for_review(self):
        classifier = DdsCategoryClassifier([
            CategoryExample(
                description="Оплата коммунальных услуг офиса за июль",
                amount=Decimal("-10000"),
                wallet="Расчетный счет",
                article="Аренда помещений",
            ),
        ])

        prediction = classifier.predict(
            "Коммунальные услуги офиса за август",
            Decimal("-11000"),
            "Расчетный счет",
        )

        self.assertEqual(prediction.status, "review")
        self.assertEqual(prediction.article, "Аренда помещений")

    def test_unknown_description_stays_blank_for_review(self):
        classifier = DdsCategoryClassifier([
            CategoryExample(
                description="Аренда офиса",
                amount=Decimal("-10000"),
                wallet="Расчетный счет",
                article="Аренда помещений",
            ),
        ])

        prediction = classifier.predict(
            "Совершенно новый непохожий расход",
            Decimal("-42"),
            "Другой кошелек",
        )

        self.assertEqual(prediction.status, "review")
        self.assertEqual(prediction.article, "")

    def test_learned_correction_makes_next_exact_match_automatic(self):
        classifier = DdsCategoryClassifier()
        classifier.add_verified(
            description="Бронь студии для съемок",
            amount=Decimal("-5000"),
            wallet="Офис подотчет",
            article="Доп. работы отдела развития",
            source="learned",
        )

        prediction = classifier.predict(
            "Бронь студии для съемок",
            Decimal("-7500"),
            "Другой кошелек",
        )

        self.assertEqual(prediction.status, "auto")
        self.assertEqual(prediction.article, "Доп. работы отдела развития")

    def test_conflicting_exact_history_leaves_article_blank(self):
        classifier = DdsCategoryClassifier([
            CategoryExample("Перевод в подотчет", -100, "Кошелек", "Статья 1"),
            CategoryExample("Перевод в подотчет", -200, "Кошелек", "Статья 2"),
        ])

        prediction = classifier.predict("Перевод в подотчет", -300, "Кошелек")

        self.assertEqual(prediction.status, "review")
        self.assertEqual(prediction.article, "")
        self.assertIn("conflicting", prediction.reason)


if __name__ == "__main__":
    unittest.main()
