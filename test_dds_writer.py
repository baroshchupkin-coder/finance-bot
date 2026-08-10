import unittest
from datetime import datetime
from decimal import Decimal
from threading import Lock
from zoneinfo import ZoneInfo

from dds_integration import CURRENCY_KGS, PaymentCandidate
from dds_writer import DdsWriter, is_retryable_dds_error


class FakeDdsSheet:
    row_count = 610

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


def build_writer(wallets_by_username):
    writer = DdsWriter.__new__(DdsWriter)
    writer.start_row = 606
    writer.wallets_by_user = {}
    writer.wallets_by_username = wallets_by_username
    writer.lock = Lock()
    writer.dds_sheet = FakeDdsSheet()
    writer.log_sheet = FakeLogSheet()
    writer.log_entries = {}
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

    def test_unknown_wallet_is_logged_without_dds_write(self):
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

        self.assertEqual(result["status"], "needs_wallet")
        self.assertEqual(writer.dds_sheet.writes, [])
        self.assertEqual(writer.log_sheet.rows[0][2], "needs_wallet")

    def test_ready_marker_is_idempotent(self):
        writer = build_writer({})

        writer._mark_ready(self.event_time)
        writer._mark_ready(self.event_time)

        self.assertEqual(len(writer.log_sheet.rows), 1)
        self.assertEqual(writer.log_sheet.rows[0][2], "ready")
        self.assertEqual(writer.log_sheet.rows[0][3], "system")

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
