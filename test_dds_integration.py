import unittest
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from dds_integration import (
    CURRENCY_KGS,
    CURRENCY_RUB,
    CURRENCY_USD,
    MissingWalletMapping,
    add_message_link,
    build_bot_invoice_candidate,
    build_media_reference_candidate,
    build_dds_row,
    event_is_in_scope,
    event_key,
    find_next_available_row,
    parse_amount_with_currency,
    parse_number,
    parse_standalone_payment,
    resolve_wallet_for_payer,
    telegram_message_link,
)


class StandalonePaymentParsingTests(unittest.TestCase):
    def test_parses_payment_and_ignores_balance_amount(self):
        text = (
            "- 300 сом - доставка брендированных футболок и скатерти "
            "в офис к форуму 26.07\nОстаток 51,5 тысяча сом"
        )
        decision = parse_standalone_payment(text)

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.amount, Decimal("-300"))
        self.assertEqual(decision.candidate.currency, CURRENCY_KGS)
        self.assertEqual(decision.candidate.description, text)

    def test_unsigned_leading_payment_defaults_to_expense(self):
        decision = parse_standalone_payment(
            "3 000 руб. - оплата студии"
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.amount, Decimal("-3000"))
        self.assertEqual(decision.candidate.currency, CURRENCY_RUB)

    def test_explicit_plus_is_preserved_as_income(self):
        decision = parse_standalone_payment(
            "+ $1,250.50 - возврат средств"
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.amount, Decimal("1250.50"))
        self.assertEqual(decision.candidate.currency, CURRENCY_USD)

    def test_amount_only_requires_attached_media(self):
        self.assertFalse(parse_standalone_payment("300 сом").accepted)

        decision = parse_standalone_payment("300 сом", has_media=True)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.amount, Decimal("-300"))

    def test_parses_explicit_amount_inside_media_caption(self):
        decision = parse_standalone_payment(
            "Отель Ташкент (17\u00a0281,2 сом)",
            has_media=True,
            default_currency=CURRENCY_KGS,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.amount, Decimal("-17281.2"))
        self.assertEqual(decision.candidate.currency, CURRENCY_KGS)
        self.assertEqual(
            decision.candidate.source_kind,
            "standalone_chat_payment_from_media_caption",
        )

    def test_ignores_balance_only_message(self):
        decision = parse_standalone_payment("Остаток 51 500 сом")
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "balance_only")

    def test_ignores_discussion_with_numbers_not_at_start(self):
        decision = parse_standalone_payment(
            "Давайте оплатим завтра 3000 сом после согласования"
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "amount_not_at_start")

    def test_ignores_file_without_caption(self):
        decision = parse_standalone_payment("", has_media=True)
        self.assertFalse(decision.accepted)

    def test_infers_kgs_for_short_payment_starting_with_amount(self):
        decision = parse_standalone_payment(
            "30 000 на работу по Хвану",
            default_currency=CURRENCY_KGS,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.amount, Decimal("-30000"))
        self.assertEqual(decision.candidate.currency, CURRENCY_KGS)
        self.assertEqual(
            decision.candidate.source_kind,
            "standalone_chat_payment_inferred_kgs",
        )

    def test_infers_kgs_for_caption_ending_with_amount(self):
        decision = parse_standalone_payment(
            "Хеннесси, конверт и пакет - 8 709",
            has_media=True,
            default_currency=CURRENCY_KGS,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.candidate.amount, Decimal("-8709"))
        self.assertEqual(decision.candidate.currency, CURRENCY_KGS)

    def test_currency_free_amount_requires_chat_default(self):
        decision = parse_standalone_payment("30 000 на работу по Хвану")
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "amount_without_currency")

    def test_currency_free_discussion_is_not_treated_as_payment(self):
        decision = parse_standalone_payment(
            "Обсудим бюджет 30 000 завтра",
            default_currency=CURRENCY_KGS,
        )
        self.assertFalse(decision.accepted)

    def test_currency_free_message_with_multiple_numbers_is_ambiguous(self):
        decision = parse_standalone_payment(
            "30 000 на работу, остаток 50 000",
            default_currency=CURRENCY_KGS,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "ambiguous_currency_free_amount")


class BotInvoiceParsingTests(unittest.TestCase):
    def test_builds_deterministic_candidate_from_request_data(self):
        candidate = build_bot_invoice_candidate(
            "448",
            "15.000 сом",
            "Уборка офиса",
            "Оплата уборщице за июль",
        )

        self.assertEqual(candidate.amount, Decimal("-15000"))
        self.assertEqual(candidate.currency, CURRENCY_KGS)
        self.assertEqual(
            candidate.description,
            "Счет #448 — Уборка офиса\nОплата уборщице за июль",
        )

    def test_compacts_payment_details_and_keeps_amount_breakdown(self):
        candidate = build_bot_invoice_candidate(
            "551",
            "45 000 сом",
            "Зарплата менеджера",
            (
                "35 000 сом - фиксированная часть\n"
                "5 000 сом - KPI\n"
                "5 000 сом - процент\n"
                "45 000 сом - итоговая сумма к оплате\n"
                "Оплатить 10.08 переводом на карту 1234 5678 9012 3456"
            ),
        )

        self.assertEqual(
            candidate.description,
            (
                "Счет #551 — Зарплата менеджера\n"
                "35 000 сом - фиксированная часть\n"
                "5 000 сом - KPI\n"
                "5 000 сом - процент"
            ),
        )

    def test_removes_wallet_and_payment_instructions(self):
        candidate = build_bot_invoice_candidate(
            "551",
            "400 $",
            "Пополнение подотчета на оплату сервисов Виктория",
            (
                "400 $ - итоговая сумма к оплате\n"
                "Оплатить 04.08\n"
                "Оплата в TRC20 на кошелек:\n"
                "TU9KV9BKjrhipJt7ztMSqaGvJSU9kADqAe\n"
                "*Перед оплатой отправить адрес кошелька"
            ),
        )

        self.assertEqual(
            candidate.description,
            "Счет #551 — Пополнение подотчета на оплату сервисов Виктория",
        )

    def test_removes_technical_suffix_but_keeps_purchase_purpose(self):
        candidate = build_bot_invoice_candidate(
            "582",
            "3500 сом",
            "Яндекс Еда",
            (
                "3500 сом - покупка кофе в зёрнах для кофемашины. "
                "Оплата переводом курьеру"
            ),
        )

        self.assertEqual(
            candidate.description,
            "Счет #582 — Яндекс Еда\n3500 сом - покупка кофе в зёрнах для кофемашины",
        )

    def test_uses_request_amount_and_detects_currency_in_comment(self):
        candidate = build_bot_invoice_candidate(
            "516",
            "80000",
            "Эргешова Анастасия",
            "80000 сом - СММ",
        )
        self.assertEqual(candidate.amount, Decimal("-80000"))
        self.assertEqual(candidate.currency, CURRENCY_KGS)

    def test_supports_usdt_as_usd_wallet_currency(self):
        candidate = build_bot_invoice_candidate(
            "530",
            "1.092,8",
            "Руководитель квалификации",
            "1.092,8 USDT - итоговая сумма",
        )
        self.assertEqual(candidate.amount, Decimal("-1092.8"))
        self.assertEqual(candidate.currency, CURRENCY_USD)

    def test_parses_common_currency_positions(self):
        cases = {
            "500 $": (Decimal("-500"), CURRENCY_USD),
            "$1,000": (Decimal("-1000"), CURRENCY_USD),
            "2 500 RUB": (Decimal("-2500"), CURRENCY_RUB),
            "1.250,50 сом": (Decimal("-1250.50"), CURRENCY_KGS),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                parsed = parse_amount_with_currency(value)
                self.assertEqual((parsed.amount, parsed.currency), expected)

    def test_parses_currency_free_request_amount(self):
        self.assertEqual(parse_number("67.007"), Decimal("-67007"))
        self.assertEqual(parse_number("1.092,8"), Decimal("-1092.8"))


class WalletAndRowTests(unittest.TestCase):
    def test_resolves_wallet_by_user_and_currency(self):
        candidate = parse_standalone_payment(
            "- 300 сом - доставка"
        ).candidate
        row = build_dds_row(
            candidate,
            date(2026, 7, 29),
            123,
            {"123": {CURRENCY_KGS: "Офис подотчет"}},
        )

        self.assertEqual(row.wallet, "Офис подотчет")
        self.assertEqual(
            row.updates_for_row(582),
            {
                "D582:F582": [["29.07.2026", -300.0, "Офис подотчет"]],
                "H582": [["- 300 сом - доставка"]],
            },
        )

    def test_missing_wallet_is_not_guessed(self):
        candidate = parse_standalone_payment(
            "- 300 сом - доставка"
        ).candidate
        with self.assertRaises(MissingWalletMapping):
            build_dds_row(candidate, date(2026, 7, 29), 123, {})

    def test_resolves_confirmed_username_mapping_without_guessing(self):
        wallet = resolve_wallet_for_payer(
            999,
            "@n0visad",
            CURRENCY_USD,
            {},
            {"n0visad": {CURRENCY_USD: "Александр $"}},
        )
        self.assertEqual(wallet, "Александр $")

        with self.assertRaises(MissingWalletMapping):
            resolve_wallet_for_payer(
                999,
                "@unknown",
                CURRENCY_USD,
                {},
                {"n0visad": {CURRENCY_USD: "Александр $"}},
            )

    def test_resolves_only_wallet_when_media_has_no_currency(self):
        wallet = resolve_wallet_for_payer(
            999,
            "@kirillvorontcov",
            "",
            {},
            {"kirillvorontcov": {CURRENCY_KGS: "Офис подотчет"}},
        )
        self.assertEqual(wallet, "Офис подотчет")

        with self.assertRaises(MissingWalletMapping):
            resolve_wallet_for_payer(
                999,
                "@n0visad",
                "",
                {},
                {
                    "n0visad": {
                        CURRENCY_KGS: "Александр KGS",
                        CURRENCY_USD: "Александр $",
                    }
                },
            )

    def test_media_reference_keeps_caption_and_clickable_message_url(self):
        link = telegram_message_link(-1003806940668, 1663)
        candidate = build_media_reference_candidate("Оплата студии", link)

        self.assertIsNone(candidate.amount)
        self.assertEqual(candidate.currency, "")
        self.assertEqual(
            candidate.description,
            "Оплата студии\nhttps://t.me/c/3806940668/1663",
        )
        self.assertEqual(
            add_message_link(candidate, link).description,
            candidate.description,
        )

    def test_media_reference_can_use_chat_default_currency(self):
        candidate = build_media_reference_candidate(
            "Оплата госпошлины",
            "https://t.me/c/3806940668/1759",
            default_currency=CURRENCY_KGS,
        )

        self.assertIsNone(candidate.amount)
        self.assertEqual(candidate.currency, CURRENCY_KGS)
        self.assertEqual(
            candidate.source_kind,
            "standalone_chat_media_reference_inferred_kgs",
        )

    def test_bot_invoice_uses_chat_default_when_currency_is_missing(self):
        candidate = build_bot_invoice_candidate(
            544,
            "10000",
            "Дмитрий Жирков",
            "10000 за поддержку август, оплата по QR",
            default_currency=CURRENCY_KGS,
        )

        self.assertEqual(candidate.amount, Decimal("-10000"))
        self.assertEqual(candidate.currency, CURRENCY_KGS)

    def test_public_chat_message_link_uses_username(self):
        self.assertEqual(
            telegram_message_link(-1001234567890, 42, "finance_chat"),
            "https://t.me/finance_chat/42",
        )

    def test_finds_first_empty_row_from_explicit_launch_cursor(self):
        rows = [
            ["29.07.2026", "-100", "Офис подотчет", "Оплата"],
            ["", "", "", ""],
            ["20.07.2026", "-15794", "Офис подотчет", "Старая строка"],
        ]
        self.assertEqual(find_next_available_row(rows, 582), 583)


class LaunchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Asia/Novosibirsk")
        self.start_at = datetime(2026, 8, 3, 14, 0, tzinfo=self.tz)

    def test_integration_is_hard_disabled(self):
        self.assertFalse(
            event_is_in_scope(
                -1003806940668,
                self.start_at,
                enabled=False,
                start_at=self.start_at,
            )
        )

    def test_rejects_old_and_unrelated_events(self):
        self.assertFalse(
            event_is_in_scope(
                -1003806940668,
                datetime(2026, 8, 3, 13, 59, tzinfo=self.tz),
                enabled=True,
                start_at=self.start_at,
            )
        )
        self.assertFalse(
            event_is_in_scope(
                -1000000000000,
                self.start_at,
                enabled=True,
                start_at=self.start_at,
            )
        )

    def test_accepts_only_allowed_event_at_or_after_start(self):
        self.assertTrue(
            event_is_in_scope(
                -1003764038215,
                self.start_at,
                enabled=True,
                start_at=self.start_at,
            )
        )
        self.assertEqual(event_key(-1003764038215, 777), "-1003764038215:777")


if __name__ == "__main__":
    unittest.main()
