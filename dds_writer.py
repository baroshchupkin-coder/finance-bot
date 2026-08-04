import re
from threading import Lock

try:
    from gspread.exceptions import WorksheetNotFound
except ImportError:
    class WorksheetNotFound(Exception):
        pass

from dds_integration import (
    DDS_SHEET_NAME,
    DDS_SPREADSHEET_ID,
    DdsRow,
    MissingWalletMapping,
    decimal_for_sheets,
    find_next_available_row,
    resolve_wallet_for_payer,
)


LOG_SHEET_NAME = "dds_logs"
LOG_HEADERS = [
    "event_key",
    "event_time",
    "status",
    "source_kind",
    "chat_id",
    "message_id",
    "user_id",
    "username",
    "currency",
    "amount",
    "wallet",
    "dds_row",
    "request_id",
    "reason",
    "description",
]


class DdsWriter:
    def __init__(
        self,
        client,
        start_row,
        wallets_by_user=None,
        wallets_by_username=None,
    ):
        self.start_row = int(start_row)
        self.wallets_by_user = wallets_by_user or {}
        self.wallets_by_username = {
            str(username).strip().lstrip("@").lower(): values
            for username, values in (wallets_by_username or {}).items()
        }
        self.lock = Lock()

        self.dds_book = client.open_by_key(DDS_SPREADSHEET_ID)
        self.dds_sheet = self.dds_book.worksheet(DDS_SHEET_NAME)
        self.finance_book = client.open("Finance bot")
        self.log_sheet = self._get_or_create_log_sheet()
        self.log_entries = self._load_log_entries()

    def _get_or_create_log_sheet(self):
        try:
            worksheet = self.finance_book.worksheet(LOG_SHEET_NAME)
        except WorksheetNotFound:
            worksheet = self.finance_book.add_worksheet(
                title=LOG_SHEET_NAME,
                rows=1000,
                cols=len(LOG_HEADERS),
            )

        header = worksheet.row_values(1)
        if not header:
            worksheet.update(
                values=[LOG_HEADERS],
                range_name=f"A1:O1",
                raw=True,
            )
        elif header[:len(LOG_HEADERS)] != LOG_HEADERS:
            raise RuntimeError(
                f"{LOG_SHEET_NAME} has unexpected headers: {header}"
            )
        return worksheet

    def _load_log_entries(self):
        entries = {}
        for row_number, row in enumerate(
            self.log_sheet.get_all_values()[1:],
            start=2,
        ):
            if not row or not row[0]:
                continue
            entries[row[0]] = {
                "log_row": row_number,
                "status": row[2] if len(row) > 2 else "",
                "dds_row": self._parse_row_number(row[11] if len(row) > 11 else ""),
            }
        return entries

    @staticmethod
    def _parse_row_number(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _append_log(self, values):
        response = self.log_sheet.append_row(
            values,
            value_input_option="RAW",
        )
        updated_range = (
            response.get("updates", {}).get("updatedRange", "")
            if isinstance(response, dict)
            else ""
        )
        match = re.search(r"!A(\d+)", updated_range)
        if match:
            return int(match.group(1))
        return len(self.log_sheet.col_values(1))

    def _find_next_row(self):
        end_row = self.dds_sheet.row_count
        raw_rows = self.dds_sheet.get(
            f"D{self.start_row}:H{end_row}",
            value_render_option="FORMATTED_VALUE",
        )
        candidate_rows = []
        for offset in range(end_row - self.start_row + 1):
            row = raw_rows[offset] if offset < len(raw_rows) else []
            candidate_rows.append([
                row[0] if len(row) > 0 else "",
                row[1] if len(row) > 1 else "",
                row[2] if len(row) > 2 else "",
                row[4] if len(row) > 4 else "",
            ])
        return find_next_available_row(candidate_rows, self.start_row)

    def _write_dds_row(self, row_number, dds_row):
        updates = dds_row.updates_for_row(row_number)
        self.dds_sheet.batch_update(
            [
                {"range": range_name, "values": values}
                for range_name, values in updates.items()
            ],
            value_input_option="USER_ENTERED",
        )

    def record_candidate(
        self,
        event_key,
        event_time,
        candidate,
        chat_id,
        message_id,
        user_id,
        username,
        request_id="",
    ):
        with self.lock:
            existing = self.log_entries.get(event_key)
            if existing and existing["status"] != "processing":
                return {
                    "status": existing["status"],
                    "dds_row": existing["dds_row"],
                    "duplicate": True,
                }

            try:
                wallet = resolve_wallet_for_payer(
                    user_id,
                    username,
                    candidate.currency,
                    self.wallets_by_user,
                    self.wallets_by_username,
                )
            except MissingWalletMapping as exc:
                if existing:
                    self.log_sheet.update_cell(
                        existing["log_row"],
                        3,
                        "needs_wallet",
                    )
                    self.log_sheet.update_cell(
                        existing["log_row"],
                        14,
                        str(exc),
                    )
                    existing["status"] = "needs_wallet"
                else:
                    log_row = self._append_log([
                        event_key,
                        event_time.isoformat(),
                        "needs_wallet",
                        candidate.source_kind,
                        str(chat_id),
                        str(message_id),
                        str(user_id),
                        str(username or ""),
                        candidate.currency,
                        decimal_for_sheets(candidate.amount),
                        "",
                        "",
                        str(request_id or ""),
                        str(exc),
                        candidate.description,
                    ])
                    self.log_entries[event_key] = {
                        "log_row": log_row,
                        "status": "needs_wallet",
                        "dds_row": None,
                    }
                return {
                    "status": "needs_wallet",
                    "dds_row": None,
                    "duplicate": False,
                }

            dds_row = DdsRow(
                payment_date=event_time.date(),
                amount=candidate.amount,
                wallet=wallet,
                purpose=candidate.description,
            )

            if existing and existing["dds_row"]:
                target_row = existing["dds_row"]
                log_row = existing["log_row"]
            else:
                target_row = self._find_next_row()
                log_row = self._append_log([
                    event_key,
                    event_time.isoformat(),
                    "processing",
                    candidate.source_kind,
                    str(chat_id),
                    str(message_id),
                    str(user_id),
                    str(username or ""),
                    candidate.currency,
                    decimal_for_sheets(candidate.amount),
                    wallet,
                    str(target_row),
                    str(request_id or ""),
                    "",
                    candidate.description,
                ])
                self.log_entries[event_key] = {
                    "log_row": log_row,
                    "status": "processing",
                    "dds_row": target_row,
                }

            self._write_dds_row(target_row, dds_row)
            self.log_sheet.update_cell(log_row, 3, "written")
            self.log_entries[event_key]["status"] = "written"
            return {
                "status": "written",
                "dds_row": target_row,
                "duplicate": False,
            }
