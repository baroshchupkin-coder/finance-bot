"""Read SKP reference data and apply an atomic group of entries to the pilot copy."""

import re
from datetime import date
from threading import Lock

from dds_integration import telegram_message_link
from shared_dds import SHARED_DDS_SPREADSHEET_ID
from skp_ai import Catalog, NO_PROJECT, WALLET_CURRENCIES


PILOT_SPREADSHEET_ID = "17com6EKE2UKzPoiBqj9Fl3otjpWoRsv3iV7XwpLDR-8"
SHEET_NAME = "ДДС: месяц"
WALLET_TAB = "ДДС: настройки (для ввода сальдо)"
ARTICLE_TAB = "ДДС: статьи"
SUMMARY_TAB = "ДДС: Сводный"
SUMMARY_BLOCKS = ((22, 31), (32, 41), (42, 51), (52, 54), (55, 57))
SHEETS_EPOCH = date(1899, 12, 30)
_LINK = re.compile(r"https://t\.me/(?:c/\d+|[A-Za-z0-9_]+)/\d+")


def read_catalog(book):
    month = book.worksheet(SHEET_NAME)
    wallet_names = book.worksheet(WALLET_TAB).col_values(1)[2:]
    wallets = {name: WALLET_CURRENCIES[name] for name in wallet_names if name in WALLET_CURRENCIES}
    article_rows = book.worksheet(ARTICLE_TAB).get("A2:E106")
    articles = {}
    for row in article_rows:
        if row and row[0]:
            cells = (list(row) + [""] * 5)[:5]
            articles[cells[0]] = {"group": cells[2], "description": cells[4]}

    summary = book.worksheet(SUMMARY_TAB).col_values(1)
    projects = {NO_PROJECT: tuple(summary[17:21])}
    for start, end in SUMMARY_BLOCKS:
        project = summary[start - 1]
        projects[project] = tuple(name for name in summary[start:end] if name)
    if (month.row_count < 9 or not wallets or len(projects) != len(SUMMARY_BLOCKS) + 1
            or any(not name or not values or any(a not in articles for a in values)
                   for name, values in projects.items())):
        raise ValueError("SKP catalog is incomplete or its layout changed")
    return Catalog(wallets, projects, articles)


def _cell(value):
    if isinstance(value, (float, int)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": value}} if value else {}


def build_entry_requests(sheet_id, first_row, entries, link):
    if not entries or not _LINK.fullmatch(link):
        raise ValueError("Missing entries or canonical Telegram link")
    requests = []
    for row, entry in enumerate(entries, first_row):
        purpose = f"{entry.purpose.rstrip()}\n{link}"
        values = [
            _cell((entry.date - SHEETS_EPOCH).days), _cell(float(entry.amount)),
            _cell(entry.wallet), _cell(entry.counterparty), _cell(purpose),
            _cell(entry.project), _cell(entry.article),
        ]
        requests.append({"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": row - 1, "columnIndex": 3},
            "rows": [{"values": values}],
            "fields": "userEnteredValue",
        }})
        requests.append({"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": row - 1, "columnIndex": 7},
            "rows": [{"values": [{"textFormatRuns": [{
                "startIndex": len((entry.purpose.rstrip() + "\n").encode("utf-16-le")) // 2,
                "format": {"link": {"uri": link}},
            }]}]}],
            "fields": "textFormatRuns",
        }})
    return requests


def existing_rows(rows, link, first_row=9):
    return [
        n for n, row in enumerate(rows, first_row)
        if len(row) > 4 and link in _LINK.findall(str(row[4]))
    ]


def _expected_values(entries, link):
    return [
        [(e.date - SHEETS_EPOCH).days, float(e.amount), e.wallet, e.counterparty,
         f"{e.purpose.rstrip()}\n{link}", e.project, e.article]
        for e in entries
    ]


class PilotWriter:
    """Single-process writer. A sheet write is atomic; ambiguous retries re-read."""

    def __init__(self, book, *, first_row=9):
        if book.id != PILOT_SPREADSHEET_ID or book.id == SHARED_DDS_SPREADSHEET_ID:
            raise ValueError("AI pilot may only use its isolated spreadsheet copy")
        self.book = book
        self.sheet = book.worksheet(SHEET_NAME)
        self.first_row = first_row
        self.lock = Lock()

    def _rows(self, options="FORMULA"):
        return self.sheet.get(f"D4:J{self.sheet.row_count}",
                              value_render_option=options)

    def seen(self, chat_id, message_id):
        link = telegram_message_link(chat_id, message_id)
        if not link:
            raise ValueError("Missing Telegram link")
        with self.lock:
            return existing_rows(self._rows(), link, 4)

    def write(self, chat_id, message_id, entries):
        entries = tuple(entries)
        if not entries or len(entries) > 8:
            raise ValueError("Expected one bounded operation")
        link = telegram_message_link(chat_id, message_id)
        if not link:
            raise ValueError("Missing Telegram link")
        with self.lock:
            rows = self._rows()
            existing = existing_rows(rows, link, 4)
            if existing:
                if len(existing) != len(entries) or existing != list(range(existing[0], existing[-1] + 1)):
                    return "review", existing
                current = self.sheet.get(
                    f"D{existing[0]}:J{existing[-1]}", value_render_option="UNFORMATTED_VALUE",
                )
                return ("already_present" if current == _expected_values(entries, link) else "review"), existing
            # Do not reuse internal gaps or overwrite formulas in the input columns.
            last = max((n for n, row in enumerate(rows, 4)
                        if any(value not in ("", None) for value in row)), default=self.first_row - 1)
            target = max(self.first_row, last + 1)
            end = target + len(entries) - 1
            if end > self.sheet.row_count:
                raise ValueError("No prepared rows in the DDS copy")
            keys = self.sheet.get(f"Q{target}:Q{end}", value_render_option="UNFORMATTED_VALUE")
            if len(keys) != len(entries) or any(not row or type(row[0]) is not int or row[0] <= 0 for row in keys):
                raise ValueError("Target rows have no DDS validation keys")
            self.book.batch_update({"requests": build_entry_requests(self.sheet.id, target, entries, link)})
            written = self.sheet.get(f"D{target}:J{end}", value_render_option="UNFORMATTED_VALUE")
            if written != _expected_values(entries, link):
                raise RuntimeError("DDS write could not be verified; inspect source link before retrying")
            return "written", list(range(target, end + 1))
