"""Read SKP reference data and apply an atomic group of entries to the pilot copy."""

import logging
import re
from datetime import date
from difflib import SequenceMatcher
from threading import Lock

from dds_category import amount_sign, normalize_description
from dds_integration import telegram_message_link
from shared_dds import SHARED_DDS_SPREADSHEET_ID
from skp_ai import Catalog, NO_PROJECT, WALLET_CURRENCIES, sanitize_text


PILOT_SPREADSHEET_ID = "17com6EKE2UKzPoiBqj9Fl3otjpWoRsv3iV7XwpLDR-8"
SHEET_NAME = "ДДС: месяц"
WALLET_TAB = "ДДС: настройки (для ввода сальдо)"
ARTICLE_TAB = "ДДС: статьи"
SUMMARY_TAB = "ДДС: Сводный"
SUMMARY_BLOCKS = ((22, 31), (32, 41), (42, 51), (52, 54), (55, 57))
SHEETS_EPOCH = date(1899, 12, 30)
_LINK = re.compile(r"https://t\.me/(?:c/\d+|[A-Za-z0-9_]+)/\d+")
_REVIEW_COLOR = {"red": 1.0, "green": 0.949, "blue": 0.8}
_GENERIC_TERMS = {"компе", "расхо", "запла", "выпла", "сегод", "вчера"}
LOG = logging.getLogger(__name__)


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
        if entry.review_category:
            requests.append({"repeatCell": {
                "range": {
                    "sheetId": sheet_id, "startRowIndex": row - 1, "endRowIndex": row,
                    "startColumnIndex": 8, "endColumnIndex": 10,
                },
                "cell": {"userEnteredFormat": {"backgroundColorStyle": {"rgbColor": _REVIEW_COLOR}}},
                "fields": "userEnteredFormat.backgroundColorStyle",
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


def _matches_rows(values, expected):
    # The values API trims empty project/article cells at the end of a row.
    return [list(row) + [""] * max(0, 7 - len(row)) for row in values] == expected


def _terms(text):
    return {word[:5] for word in text.split() if len(word) >= 4} - _GENERIC_TERMS


def rank_review_candidates(rows, text, catalog, chat_id, approved_source_ids):
    source_prefix = telegram_message_link(chat_id, 0).rsplit("/", 1)[0] + "/"
    queries = [normalize_description(line)[:220] for line in text.splitlines()]
    queries = [(line, _terms(line)) for line in queries if line]
    if not queries:
        return []
    proposed = []
    for number, row in enumerate(rows, 4):
        if len(row) < 7:
            continue
        raw, project, article = row[4:7]
        sign = amount_sign(row[1])
        group = {"expense": "Выбытие", "income": "Поступление"}.get(sign)
        if (not group or project == NO_PROJECT or article not in catalog.projects.get(project, ())
                or catalog.articles.get(article, {}).get("group") != group):
            continue
        links = _LINK.findall(str(raw))
        if any(link.startswith("https://t.me/c/1000000000/") for link in links):
            continue
        if any(link.startswith(source_prefix) and int(link.rsplit("/", 1)[1]) not in approved_source_ids
               for link in links):
            continue
        purpose = normalize_description(sanitize_text(raw).replace("[redacted]", " "))[:220]
        terms = _terms(purpose)
        if not terms:
            continue
        scores = [
            0.75 * len(terms & query_terms) / min(len(terms), len(query_terms))
            + 0.25 * SequenceMatcher(None, purpose, line).ratio()
            for line, query_terms in queries if terms & query_terms
        ]
        proposed.append((max(scores, default=0), number, purpose, project, article, sign))
    closest = sorted((entry for entry in proposed if entry[0] >= 0.52), reverse=True)[:12]
    recent, pair_counts = [], {}
    for entry in reversed(proposed):
        pair = entry[3:5]
        if entry in closest or pair_counts.get(pair, 0) >= 2:
            continue
        recent.append(entry)
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
        if len(recent) == 6:
            break
    return closest + recent


def _is_review_yellow(cell):
    fmt = cell.get("userEnteredFormat", {})
    color = fmt.get("backgroundColorStyle", {}).get("rgbColor") or fmt.get("backgroundColor") or {}
    return color.get("red", 0) >= 0.95 and 0.9 <= color.get("green", 0) <= 0.99 and color.get("blue", 0) < 0.88


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

    def inspect_message(self, chat_id, message_id, text, catalog, approved_source_ids):
        link = telegram_message_link(chat_id, message_id)
        if not link:
            raise ValueError("Missing Telegram link")
        with self.lock:
            rows = self._rows()
            existing = existing_rows(rows, link, 4)
            if existing:
                return existing, ()
            candidates = rank_review_candidates(rows, text, catalog, chat_id, approved_source_ids)
            if not candidates:
                return (), ()
            try:
                metadata = self.book.fetch_sheet_metadata(params={
                    "ranges": [f"'{SHEET_NAME}'!I{n}:J{n}" for _, n, *_ in candidates],
                    "includeGridData": "true",
                    "fields": "sheets(data(startRow,startColumn,rowData(values(formattedValue,"
                              "userEnteredFormat(backgroundColor,backgroundColorStyle)))))",
                })
            except Exception as error:
                LOG.warning("SKP review examples unavailable: %s", type(error).__name__)
                return (), ()
            labels = {}
            for sheet in metadata.get("sheets", []):
                for grid in sheet.get("data", []):
                    if grid.get("startColumn") != 8:
                        continue
                    for number, row in enumerate(grid.get("rowData", []), grid.get("startRow", 0) + 1):
                        cells = row.get("values", [])
                        if len(cells) == 2 and not any(_is_review_yellow(cell) for cell in cells):
                            labels[number] = tuple(c.get("formattedValue") for c in cells)
            examples = []
            for _, number, purpose, project, article, sign in candidates:
                if labels.get(number) == (project, article):
                    example = {"purpose": purpose, "project": project, "article": article, "kind": sign}
                    if example not in examples:
                        examples.append(example)
                    if len(examples) == 6:
                        break
            return (), tuple(examples)

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
                return ("already_present" if _matches_rows(current, _expected_values(entries, link)) else "review"), existing
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
            if not _matches_rows(written, _expected_values(entries, link)):
                raise RuntimeError("DDS write could not be verified; inspect source link before retrying")
            return "written", list(range(target, end + 1))
