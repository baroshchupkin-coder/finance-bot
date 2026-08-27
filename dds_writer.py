import logging
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
from dds_category import CategoryExample, DdsCategoryClassifier


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
    "category_suggestion",
    "category_confidence",
    "category_status",
    "category_match",
    "category_reviewed_value",
]
LEGACY_LOG_HEADER_COUNT = 15

RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


def is_retryable_dds_error(error):
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in RETRYABLE_HTTP_STATUSES:
        return True

    error_text = str(error)
    if any(f"[{status}]" in error_text for status in RETRYABLE_HTTP_STATUSES):
        return True

    module_name = error.__class__.__module__
    return isinstance(error, (ConnectionError, TimeoutError)) or module_name.startswith(
        ("requests", "urllib3")
    )


class DdsWriter:
    def __init__(
        self,
        client,
        start_row,
        wallets_by_user=None,
        wallets_by_username=None,
        activation_time=None,
        release_key="",
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
        try:
            category_examples = self._load_category_examples()
        except Exception:
            logging.exception("Failed to load DDS category history")
            category_examples = []
        self.category_classifier = DdsCategoryClassifier(category_examples)
        if activation_time is not None:
            self._mark_ready(activation_time, release_key=release_key)

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
                range_name="A1:T1",
                raw=True,
            )
        elif header[:LEGACY_LOG_HEADER_COUNT] != LOG_HEADERS[:LEGACY_LOG_HEADER_COUNT]:
            raise RuntimeError(
                f"{LOG_SHEET_NAME} has unexpected headers: {header}"
            )
        else:
            current_extension = header[
                LEGACY_LOG_HEADER_COUNT:len(LOG_HEADERS)
            ]
            expected_extension = LOG_HEADERS[LEGACY_LOG_HEADER_COUNT:]
            if current_extension and current_extension != expected_extension[:len(current_extension)]:
                raise RuntimeError(
                    f"{LOG_SHEET_NAME} has unexpected category headers: {header}"
                )
            if len(header) < len(LOG_HEADERS):
                worksheet.resize(cols=len(LOG_HEADERS))
                worksheet.update(
                    values=[expected_extension],
                    range_name="P1:T1",
                    raw=True,
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
                "amount": row[9] if len(row) > 9 else "",
                "wallet": row[10] if len(row) > 10 else "",
                "description": row[14] if len(row) > 14 else "",
                "category_suggestion": row[15] if len(row) > 15 else "",
                "category_confidence": row[16] if len(row) > 16 else "",
                "category_status": row[17] if len(row) > 17 else "",
                "category_match": row[18] if len(row) > 18 else "",
                "category_reviewed_value": row[19] if len(row) > 19 else "",
            }
        return entries

    def _load_category_examples(self):
        learned_rows = {
            entry["dds_row"]
            for entry in self.log_entries.values()
            if entry.get("dds_row")
            and entry.get("category_status") == "learned"
        }
        excluded_rows = {
            entry["dds_row"]
            for entry in self.log_entries.values()
            if entry.get("dds_row")
            and entry.get("category_status") in {"auto", "review"}
        }
        raw_rows = self.dds_sheet.get(
            f"E4:I{self.dds_sheet.row_count}",
            value_render_option="UNFORMATTED_VALUE",
        )
        examples = []
        for offset, row in enumerate(raw_rows):
            row_number = offset + 4
            if row_number in excluded_rows:
                continue
            amount = row[0] if len(row) > 0 else ""
            wallet = row[1] if len(row) > 1 else ""
            purpose = row[3] if len(row) > 3 else ""
            article = row[4] if len(row) > 4 else ""
            if str(purpose or "").strip() and str(article or "").strip():
                examples.append(CategoryExample(
                    description=purpose,
                    amount=amount,
                    wallet=wallet,
                    article=article,
                    source="learned" if row_number in learned_rows else "history",
                ))
        return examples

    def _mark_ready(self, activation_time, release_key=""):
        event_key = f"system:activation:{activation_time.isoformat()}"
        if release_key:
            event_key = f"{event_key}:{release_key}"
        if event_key in self.log_entries:
            return

        log_row = self._append_log([
            event_key,
            activation_time.isoformat(),
            "ready",
            "system",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "DDS integration initialized",
            "",
        ])
        self.log_entries[event_key] = {
            "log_row": log_row,
            "status": "ready",
            "dds_row": None,
        }

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

    def record_diagnostic(
        self,
        event_key,
        event_time,
        status,
        source_kind,
        chat_id,
        message_id,
        user_id,
        username,
        currency="",
        amount="",
        reason="",
        description="",
    ):
        with self.lock:
            existing = self.log_entries.get(event_key)
            if existing:
                return {
                    "status": existing["status"],
                    "dds_row": existing["dds_row"],
                    "log_row": existing["log_row"],
                }

            log_row = self._append_log([
                event_key,
                event_time.isoformat(),
                status,
                source_kind,
                str(chat_id),
                str(message_id),
                str(user_id),
                username or "",
                currency or "",
                decimal_for_sheets(amount) if amount not in (None, "") else "",
                "",
                "",
                "",
                reason or "",
                description or "",
            ])
            self.log_entries[event_key] = {
                "log_row": log_row,
                "status": status,
                "dds_row": None,
            }
            return {"status": status, "dds_row": None, "log_row": log_row}

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

    def _write_dds_row(self, row_number, dds_row, category_prediction):
        updates = dds_row.updates_for_row(row_number)
        if category_prediction.article:
            updates[f"I{row_number}"] = [[category_prediction.article]]
        self.dds_sheet.batch_update(
            [
                {"range": range_name, "values": values}
                for range_name, values in updates.items()
            ],
            value_input_option="USER_ENTERED",
        )
        self._make_telegram_link_clickable(row_number, dds_row.purpose)
        if category_prediction.status == "review":
            self._mark_category_for_review(row_number)

    def _category_cell_request(self, row_number, background_color):
        return {
            "repeatCell": {
                "range": {
                    "sheetId": self.dds_sheet.id,
                    "startRowIndex": row_number - 1,
                    "endRowIndex": row_number,
                    "startColumnIndex": 8,
                    "endColumnIndex": 9,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": background_color,
                    },
                },
                "fields": "userEnteredFormat.backgroundColor",
            },
        }

    def _mark_category_for_review(self, row_number):
        self.dds_book.batch_update({
            "requests": [self._category_cell_request(
                row_number,
                {"red": 1.0, "green": 0.949, "blue": 0.8},
            )],
        })

    def _clear_category_review(self, row_number):
        self.dds_book.batch_update({
            "requests": [self._category_cell_request(
                row_number,
                {"red": 1.0, "green": 1.0, "blue": 1.0},
            )],
        })

    def _sync_category_feedback(self):
        pending = [
            (event_key, entry)
            for event_key, entry in self.log_entries.items()
            if entry.get("dds_row")
            and entry.get("category_status") == "review"
        ]
        if not pending:
            return 0

        first_row = min(entry["dds_row"] for _, entry in pending)
        last_row = max(entry["dds_row"] for _, entry in pending)
        review_cells = self._read_category_review_cells(first_row, last_row)
        learned_count = 0
        for event_key, entry in pending:
            current_article, is_yellow = review_cells.get(
                entry["dds_row"],
                ("", True),
            )
            if is_yellow or not current_article.strip():
                continue

            self.category_classifier.add_verified(
                description=entry.get("description", ""),
                amount=entry.get("amount", ""),
                wallet=entry.get("wallet", ""),
                article=current_article,
                source="learned",
            )
            self.log_sheet.update_cell(entry["log_row"], 18, "learned")
            self.log_sheet.update_cell(entry["log_row"], 20, current_article)
            entry["category_status"] = "learned"
            entry["category_reviewed_value"] = current_article
            self._clear_category_review(entry["dds_row"])
            learned_count += 1
        return learned_count

    @staticmethod
    def _is_review_yellow(color):
        if not color:
            return False
        return (
            float(color.get("red", 0)) >= 0.95
            and 0.90 <= float(color.get("green", 0)) <= 0.99
            and 0.72 <= float(color.get("blue", 0)) <= 0.86
        )

    def _read_category_review_cells(self, first_row, last_row):
        metadata = self.dds_book.fetch_sheet_metadata(params={
            "includeGridData": "true",
            "ranges": f"'{DDS_SHEET_NAME}'!I{first_row}:I{last_row}",
        })
        cells = {
            row_number: ("", False)
            for row_number in range(first_row, last_row + 1)
        }
        for sheet in metadata.get("sheets", []):
            if sheet.get("properties", {}).get("sheetId") != self.dds_sheet.id:
                continue
            for grid_data in sheet.get("data", []):
                start_row = int(grid_data.get("startRow", first_row - 1)) + 1
                for offset, row_data in enumerate(grid_data.get("rowData", [])):
                    row_number = start_row + offset
                    values = row_data.get("values", [])
                    cell = values[0] if values else {}
                    article = str(cell.get("formattedValue", ""))
                    user_format = cell.get("userEnteredFormat", {})
                    color = user_format.get("backgroundColor", {})
                    if not color:
                        color = (
                            user_format.get("backgroundColorStyle", {})
                            .get("rgbColor", {})
                        )
                    cells[row_number] = (
                        article,
                        self._is_review_yellow(color),
                    )
        return cells

    def _make_telegram_link_clickable(self, row_number, purpose):
        match = re.search(r"https://t\.me/\S+", str(purpose or ""))
        if not match:
            return
        start_index = len(
            str(purpose)[:match.start()].encode("utf-16-le")
        ) // 2

        self.dds_book.batch_update({
            "requests": [{
                "updateCells": {
                    "range": {
                        "sheetId": self.dds_sheet.id,
                        "startRowIndex": row_number - 1,
                        "endRowIndex": row_number,
                        "startColumnIndex": 7,
                        "endColumnIndex": 8,
                    },
                    "rows": [{
                        "values": [{
                            "textFormatRuns": [{
                                "startIndex": start_index,
                                "format": {
                                    "link": {"uri": match.group(0)},
                                    "foregroundColor": {
                                        "red": 0.0667,
                                        "green": 0.3333,
                                        "blue": 0.8,
                                    },
                                    "underline": True,
                                },
                            }],
                        }],
                    }],
                    "fields": "textFormatRuns",
                },
            }],
        })

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
            if existing and existing["status"] not in {"processing", "needs_wallet"}:
                return {
                    "status": existing["status"],
                    "dds_row": existing["dds_row"],
                    "duplicate": True,
                }

            try:
                self._sync_category_feedback()
            except Exception:
                logging.exception("Failed to sync DDS category feedback")

            wallet_reason = ""
            try:
                wallet = resolve_wallet_for_payer(
                    user_id,
                    username,
                    candidate.currency,
                    self.wallets_by_user,
                    self.wallets_by_username,
                )
            except MissingWalletMapping as exc:
                wallet = ""
                wallet_reason = str(exc)

            dds_row = DdsRow(
                payment_date=event_time.date(),
                amount=candidate.amount,
                wallet=wallet,
                purpose=candidate.description,
            )
            category_prediction = self.category_classifier.predict(
                candidate.description,
                candidate.amount,
                wallet,
            )

            if existing and existing["dds_row"]:
                target_row = existing["dds_row"]
                log_row = existing["log_row"]
            else:
                target_row = self._find_next_row()
                if existing:
                    log_row = existing["log_row"]
                else:
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
                        wallet_reason,
                        candidate.description,
                        category_prediction.article,
                        str(category_prediction.confidence),
                        category_prediction.status,
                        category_prediction.match,
                        "",
                    ])

            if existing:
                refreshed_values = {
                    3: "processing",
                    4: candidate.source_kind,
                    5: str(chat_id),
                    6: str(message_id),
                    7: str(user_id),
                    8: str(username or ""),
                    9: candidate.currency,
                    10: decimal_for_sheets(candidate.amount),
                    11: wallet,
                    12: str(target_row),
                    13: str(request_id or ""),
                    14: wallet_reason,
                    15: candidate.description,
                    16: category_prediction.article,
                    17: str(category_prediction.confidence),
                    18: category_prediction.status,
                    19: category_prediction.match,
                    20: "",
                }
                for column, value in refreshed_values.items():
                    self.log_sheet.update_cell(log_row, column, value)

            self.log_entries[event_key] = {
                "log_row": log_row,
                "status": "processing",
                "dds_row": target_row,
                "amount": decimal_for_sheets(candidate.amount),
                "wallet": wallet,
                "description": candidate.description,
                "category_suggestion": category_prediction.article,
                "category_confidence": str(category_prediction.confidence),
                "category_status": category_prediction.status,
                "category_match": category_prediction.match,
                "category_reviewed_value": "",
            }

            self._write_dds_row(target_row, dds_row, category_prediction)
            self.log_sheet.update_cell(log_row, 3, "written")
            self.log_entries[event_key]["status"] = "written"
            return {
                "status": "written",
                "dds_row": target_row,
                "duplicate": False,
                "category": category_prediction.article,
                "category_status": category_prediction.status,
            }
