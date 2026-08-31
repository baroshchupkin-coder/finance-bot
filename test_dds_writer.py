import unittest
from datetime import datetime
from decimal import Decimal
from threading import Lock
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from dds_integration import (
    CURRENCY_KGS, PaymentCandidate, add_message_link,
    parse_standalone_payments, standalone_payment_event_key,
)
from dds_category import CategoryExample, DdsCategoryClassifier
from dds_writer import DdsWriter, is_retryable_dds_error


class FakeDdsSheet:
    row_count = 610
    id = 0

    def __init__(self):
        self.writes = []

    def get(self, range_name, value_render_option=None):
        return []

    def batch_update(self, updates, value_input_option=None):
        self.writes.append((updates, value_input_option))


class FakeLogSheet:
    def __init__(self):
        self.rows = []
        self.cell_updates = []

    def append_row(self, values, value_input_option=None):
        self.rows.append(values)
        row_number = len(self.rows) + 1
        return {"updates": {"updatedRange": f"dds_logs!A{row_number}:O{row_number}"}}

    def col_values(self, column):
        return ["event_key"] + [row[0] for row in self.rows]

    def update_cell(self, row, column, value):
        self.cell_updates.append((row, column, value))


class FakeDdsBook:
    def __init__(self):
        self.requests = []
        self.metadata = {"sheets": []}

    def batch_update(self, body):
        self.requests.append(body)

    def fetch_sheet_metadata(self, params=None):
        return self.metadata


def build_writer(wallets_by_username):
    writer = DdsWriter.__new__(DdsWriter)
    writer.start_row = 606
    writer.wallets_by_user = {}
    writer.wallets_by_username = wallets_by_username
    writer.lock = Lock()
    writer.dds_sheet = FakeDdsSheet()
    writer.dds_book = FakeDdsBook()
    writer.log_sheet = FakeLogSheet()
    writer.log_entries = {}
    writer.category_classifier = DdsCategoryClassifier()
    return writer


class DdsWriterTests(unittest.TestCase):
    def setUp(self):
        self.event_time = datetime(
            2026,
            8,
            4,
            12,
            0,
            tzinfo=ZoneInfo("Asia/Bishkek"),
        )
        self.candidate = PaymentCandidate(
            amount=Decimal("-300"),
            currency=CURRENCY_KGS,
            description="-300 сом - доставка",
            source_kind="standalone_chat_payment",
        )

    def test_writes_first_available_row_and_marks_log_written(self):
        writer = build_writer({
            "kirillvorontcov": {CURRENCY_KGS: "Офис подотчет"},
        })

        result = writer.record_candidate(
            "message:-1003764038215:1",
            self.event_time,
            self.candidate,
            -1003764038215,
            1,
            1525565778,
            "KirillVorontcov",
        )

        self.assertEqual(result["status"], "written")
        self.assertEqual(result["dds_row"], 606)
        self.assertEqual(len(writer.dds_sheet.writes), 1)
        updates, value_input_option = writer.dds_sheet.writes[0]
        self.assertEqual(value_input_option, "USER_ENTERED")
        self.assertEqual(
            updates,
            [
                {
                    "range": "D606:F606",
                    "values": [["04.08.2026", -300.0, "Офис подотчет"]],
                },
                {
                    "range": "H606",
                    "values": [["-300 сом - доставка"]],
                },
            ],
        )
        self.assertIn((2, 3, "written"), writer.log_sheet.cell_updates)

    def test_duplicate_written_event_is_not_written_twice(self):
        writer = build_writer({
            "kirillvorontcov": {CURRENCY_KGS: "Офис подотчет"},
        })
        writer.log_entries["invoice:516"] = {
            "log_row": 2,
            "status": "written",
            "dds_row": 606,
        }

        result = writer.record_candidate(
            "invoice:516",
            self.event_time,
            self.candidate,
            -1003806940668,
            100,
            1,
            "n0visad",
            request_id="516",
        )

        self.assertTrue(result["duplicate"])
        self.assertEqual(writer.dds_sheet.writes, [])

    def test_split_payments_retry_without_duplicate_rows(self):
        writer = build_writer({"payer": {CURRENCY_KGS: "Офис подотчет"}})
        writer._find_next_row = Mock(side_effect=[606, 607])
        candidates = parse_standalone_payments(
            "Ютуб 120к продюсер 180к съемки", default_currency=CURRENCY_KGS,
        ).candidates
        original_write = writer._write_dds_row
        failures = [607]

        def write_with_one_failure(row_number, *args):
            if row_number in failures:
                failures.remove(row_number)
                raise TimeoutError("Simulated Sheets interruption")
            return original_write(row_number, *args)

        writer._write_dds_row = write_with_one_failure
        link = "https://t.me/c/3806940668/999"

        def record(index, candidate):
            return writer.record_candidate(
                standalone_payment_event_key(-1003806940668, 999, index),
                self.event_time, add_message_link(candidate, link),
                -1003806940668, 999, 7, "payer",
            )

        record(0, candidates[0])
        with self.assertRaises(TimeoutError):
            record(1, candidates[1])
        self.assertTrue(record(0, candidates[0])["duplicate"])
        record(1, candidates[1])
        self.assertEqual(len(writer.dds_sheet.writes), 2)
        self.assertEqual(len(writer.log_sheet.rows), 2)
        self.assertEqual(writer._find_next_row.call_count, 2)
        for index, (updates, _) in enumerate(writer.dds_sheet.writes):
            self.assertEqual(updates[0]["values"][0][1], [-120000.0, -180000.0][index])
            self.assertEqual(updates[0]["values"][0][2], "Офис подотчет")
            self.assertIn(link, updates[1]["values"][0][0])

    def test_unknown_wallet_is_written_with_blank_wallet(self):
        writer = build_writer({})

        result = writer.record_candidate(
            "message:-1003806940668:2",
            self.event_time,
            self.candidate,
            -1003806940668,
            2,
            999,
            "unknown",
        )

        self.assertEqual(result["status"], "written")
        self.assertEqual(result["dds_row"], 606)
        updates, _ = writer.dds_sheet.writes[0]
        self.assertEqual(
            updates[0]["values"],
            [["04.08.2026", -300.0, ""]],
        )
        self.assertEqual(writer.log_sheet.rows[0][13].startswith("No DDS wallet"), True)

    def test_existing_needs_wallet_event_is_recovered_without_duplicate_log(self):
        writer = build_writer({})
        writer.log_entries["invoice:551"] = {
            "log_row": 2,
            "status": "needs_wallet",
            "dds_row": None,
        }

        result = writer.record_candidate(
            "invoice:551",
            self.event_time,
            self.candidate,
            -1003806940668,
            1663,
            5293695558,
            "ba_roshchupkin",
            request_id="551",
        )

        self.assertEqual(result["status"], "written")
        self.assertEqual(result["dds_row"], 606)
        self.assertEqual(writer.log_sheet.rows, [])
        self.assertIn((2, 12, "606"), writer.log_sheet.cell_updates)

    def test_media_reference_writes_blank_amount_and_unique_wallet(self):
        writer = build_writer({
            "kirillvorontcov": {CURRENCY_KGS: "Офис подотчет"},
        })
        candidate = PaymentCandidate(
            amount=None,
            currency="",
            description=(
                "Оплата студии\n"
                "https://t.me/c/3764038215/600"
            ),
            source_kind="standalone_chat_media_reference",
        )

        result = writer.record_candidate(
            "message:-1003764038215:600",
            self.event_time,
            candidate,
            -1003764038215,
            600,
            1525565778,
            "KirillVorontcov",
        )

        self.assertEqual(result["status"], "written")
        updates, _ = writer.dds_sheet.writes[0]
        self.assertEqual(
            updates[0]["values"],
            [["04.08.2026", "", "Офис подотчет"]],
        )
        link_request = writer.dds_book.requests[0]["requests"][0]["updateCells"]
        self.assertEqual(link_request["range"]["startRowIndex"], 605)
        run = link_request["rows"][0]["values"][0]["textFormatRuns"][0]
        self.assertEqual(run["startIndex"], len("Оплата студии\n"))
        self.assertEqual(
            run["format"]["link"]["uri"],
            "https://t.me/c/3764038215/600",
        )

    def test_medium_confidence_writes_article_and_marks_cell_yellow(self):
        writer = build_writer({
            "kirillvorontcov": {CURRENCY_KGS: "Офис подотчет"},
        })
        writer.category_classifier = DdsCategoryClassifier([
            CategoryExample(
                description="Доставка футболок в офис",
                amount=Decimal("-250"),
                wallet="Офис подотчет",
                article="Доп. работы отдела построения",
            ),
        ])

        result = writer.record_candidate(
            "message:-1003764038215:2",
            self.event_time,
            self.candidate,
            -1003764038215,
            2,
            1525565778,
            "KirillVorontcov",
        )

        self.assertEqual(result["category_status"], "review")
        updates, _ = writer.dds_sheet.writes[0]
        self.assertEqual(
            updates[-1],
            {
                "range": "I606",
                "values": [["Доп. работы отдела построения"]],
            },
        )
        repeat_cell = writer.dds_book.requests[-1]["requests"][0]["repeatCell"]
        self.assertEqual(repeat_cell["range"]["startColumnIndex"], 8)
        self.assertEqual(
            repeat_cell["cell"]["userEnteredFormat"]["backgroundColor"],
            {"red": 1.0, "green": 0.949, "blue": 0.8},
        )

    def test_removed_yellow_fill_confirms_suggestion_and_teaches_classifier(self):
        writer = build_writer({})
        writer.log_entries["message:-1003764038215:9"] = {
            "log_row": 2,
            "status": "written",
            "dds_row": 605,
            "amount": "-300",
            "wallet": "Офис подотчет",
            "description": "Доставка футболок в офис",
            "category_suggestion": "Доп. работы отдела построения",
            "category_status": "review",
        }
        writer.dds_book.metadata = {
            "sheets": [{
                "properties": {"sheetId": 0},
                "data": [{
                    "startRow": 604,
                    "rowData": [{
                        "values": [{
                            "formattedValue": "Доп. работы отдела построения",
                        }],
                    }],
                }],
            }],
        }

        learned_count = writer._sync_category_feedback()
        prediction = writer.category_classifier.predict(
            "Доставка футболок в офис",
            Decimal("-450"),
            "Офис подотчет",
        )

        self.assertEqual(learned_count, 1)
        self.assertEqual(prediction.status, "auto")
        self.assertEqual(prediction.article, "Доп. работы отдела построения")
        self.assertIn((2, 18, "learned"), writer.log_sheet.cell_updates)
        self.assertIn((2, 20, "Доп. работы отдела построения"), writer.log_sheet.cell_updates)

    def test_yellow_fill_keeps_suggestion_pending(self):
        writer = build_writer({})
        writer.log_entries["message:-1003764038215:9"] = {
            "log_row": 2,
            "status": "written",
            "dds_row": 605,
            "amount": "-300",
            "wallet": "Офис подотчет",
            "description": "Доставка футболок в офис",
            "category_suggestion": "Доп. работы отдела построения",
            "category_status": "review",
        }
        writer.dds_book.metadata = {
            "sheets": [{
                "properties": {"sheetId": 0},
                "data": [{
                    "startRow": 604,
                    "rowData": [{
                        "values": [{
                            "formattedValue": "Доп. работы отдела построения",
                            "userEnteredFormat": {
                                "backgroundColor": {
                                    "red": 1.0,
                                    "green": 0.949,
                                    "blue": 0.8,
                                },
                            },
                        }],
                    }],
                }],
            }],
        }

        learned_count = writer._sync_category_feedback()

        self.assertEqual(learned_count, 0)
        self.assertEqual(writer.log_entries[
            "message:-1003764038215:9"
        ]["category_status"], "review")

    def test_changed_article_with_removed_fill_teaches_correction(self):
        writer = build_writer({})
        writer.log_entries["message:-1003764038215:10"] = {
            "log_row": 3,
            "status": "written",
            "dds_row": 606,
            "amount": "-5000",
            "wallet": "Офис подотчет",
            "description": "Бронь студии для съемок",
            "category_suggestion": "Аренда помещений",
            "category_status": "review",
        }
        writer.dds_book.metadata = {
            "sheets": [{
                "properties": {"sheetId": 0},
                "data": [{
                    "startRow": 605,
                    "rowData": [{
                        "values": [{
                            "formattedValue": "Доп. работы отдела развития",
                        }],
                    }],
                }],
            }],
        }

        learned_count = writer._sync_category_feedback()
        prediction = writer.category_classifier.predict(
            "Бронь студии для съемок",
            Decimal("-6000"),
            "Офис подотчет",
        )

        self.assertEqual(learned_count, 1)
        self.assertEqual(prediction.article, "Доп. работы отдела развития")
        self.assertIn((3, 20, "Доп. работы отдела развития"), writer.log_sheet.cell_updates)

    def test_ready_marker_is_idempotent(self):
        writer = build_writer({})

        writer._mark_ready(self.event_time)
        writer._mark_ready(self.event_time)

        self.assertEqual(len(writer.log_sheet.rows), 1)
        self.assertEqual(writer.log_sheet.rows[0][2], "ready")
        self.assertEqual(writer.log_sheet.rows[0][3], "system")

    def test_ocr_diagnostic_is_idempotent_and_does_not_write_dds(self):
        writer = build_writer({})

        result = writer.record_diagnostic(
            "ocr:-1003764038215:600",
            self.event_time,
            "ocr_shadow_candidate",
            "standalone_receipt_ocr",
            -1003764038215,
            600,
            1525565778,
            "KirillVorontcov",
            CURRENCY_KGS,
            Decimal("-8709"),
            "single_amount; confidence=high",
            "Платеж выполнен\n8 709,00 KGS",
        )
        duplicate = writer.record_diagnostic(
            "ocr:-1003764038215:600",
            self.event_time,
            "ocr_shadow_no_candidate",
            "standalone_receipt_ocr",
            -1003764038215,
            600,
            1525565778,
            "KirillVorontcov",
        )

        self.assertEqual(result["status"], "ocr_shadow_candidate")
        self.assertEqual(duplicate["status"], "ocr_shadow_candidate")
        self.assertEqual(len(writer.log_sheet.rows), 1)
        self.assertEqual(writer.log_sheet.rows[0][8:10], [CURRENCY_KGS, "-8709"])
        self.assertEqual(writer.dds_sheet.writes, [])

    def test_identifies_retryable_google_api_errors(self):
        class FakeResponse:
            status_code = 503

        class FakeApiError(Exception):
            response = FakeResponse()

        self.assertTrue(is_retryable_dds_error(FakeApiError("unavailable")))
        self.assertTrue(is_retryable_dds_error(Exception("APIError: [429]: quota")))
        self.assertFalse(is_retryable_dds_error(ValueError("invalid worksheet")))


if __name__ == "__main__":
    unittest.main()
