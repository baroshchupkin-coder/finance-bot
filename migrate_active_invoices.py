import argparse
import json
import logging
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials


REQUEST_ID_COL = 0
STATUS_COL = 7
APPROVER_CHAT_ID_COL = 8
FILE_ID_COL = 9
APPROVER_NAME_COL = 12
PAYER_TAG_COL = 13
EXPENSE_CATEGORY_COL = 22
LAST_INVOICE_MESSAGE_CHAT_ID_COL = 20
LAST_INVOICE_MESSAGE_ID_COL = 21

STATUS_APPROVED = "Согласован"
STATUS_PAID = "Оплачено"
STATUS_REJECTED = "Отклонен"
STATUS_CANCELLED = "Отменен"

SPREADSHEET_NAME = os.getenv("MIGRATION_SPREADSHEET_NAME", "Finance bot")
REQUESTS_SHEET_NAME = os.getenv("MIGRATION_REQUESTS_SHEET_NAME", "requests")
LOGS_SHEET_NAME = os.getenv("MIGRATION_LOGS_SHEET_NAME", "logs")
OR_ADS_PAYER_TAG = "@bulat_sufyanov"
OR_PROJECT_KEYS = {"or", "or kg", "orkg"}
OR_ADS_EXPENSE_CATEGORY = "Рекламный бюджет"
OR_PROJECT_TRANSLATION = str.maketrans({
    "\u043e": "o",
    "\u0440": "r",
    "\u043a": "k",
    "\u0433": "g",
})

LOG_HEADERS = [
    "run_id",
    "timestamp",
    "mode",
    "request_id",
    "sheet_row",
    "chat_id",
    "project",
    "target",
    "amount",
    "old_message_id",
    "new_message_id",
    "action",
    "result",
    "error",
]


def get_cell(row, index, default=""):
    return row[index].strip() if len(row) > index and row[index] else default


def get_expense_category(row):
    return get_cell(row, EXPENSE_CATEGORY_COL, "Без статьи")

def normalize_project_key(project_name):
    return " ".join(
        str(project_name)
        .strip()
        .lower()
        .translate(OR_PROJECT_TRANSLATION)
        .replace("_", " ")
        .replace("-", " ")
        .replace("/", " ")
        .split()
    )

def resolve_payer_tag(project_name, expense_category, default_payer_tag):
    if (
        normalize_project_key(project_name) in OR_PROJECT_KEYS
        and expense_category == OR_ADS_EXPENSE_CATEGORY
    ):
        return OR_ADS_PAYER_TAG
    return default_payer_tag

def get_invoice_payer_tag(row):
    return resolve_payer_tag(
        get_cell(row, 3),
        get_expense_category(row),
        get_cell(row, PAYER_TAG_COL)
    )

def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_photo_path(file_path):
    return file_path.startswith("photos/")


def build_invoice_text(row):
    payer_tag = get_invoice_payer_tag(row)
    approver_name = get_cell(row, APPROVER_NAME_COL, "неизвестно")

    return (
        f"{payer_tag}\n"
        f"Счет #{get_cell(row, REQUEST_ID_COL)} одобрен\n\n"
        f"{get_cell(row, 4)}\n\n"
        f"{get_cell(row, 6)}\n\n"
        f"Согласовано: @{approver_name}"
    )


def build_paid_keyboard(request_id):
    return {
        "inline_keyboard": [
            [
                {
                    "text": "💰 Оплатил – прикрепить чек",
                    "callback_data": f"paid_{request_id}",
                }
            ],
            [
                {
                    "text": "❌ Отменить счет",
                    "callback_data": f"cancel_{request_id}",
                }
            ],
        ]
    }


def telegram_api(token, method, data=None, files=None):
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data or {},
        files=files,
        timeout=60,
    )

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram returned non-JSON response: {response.text}") from exc

    if not response.ok or not payload.get("ok"):
        raise RuntimeError(payload.get("description", response.text))

    return payload["result"]


def download_old_file(old_token, file_id):
    file_info = telegram_api(old_token, "getFile", {"file_id": file_id})
    file_path = file_info["file_path"]
    filename = Path(file_path).name or "invoice_file"

    response = requests.get(
        f"https://api.telegram.org/file/bot{old_token}/{file_path}",
        timeout=120,
    )
    response.raise_for_status()

    temp_file = tempfile.NamedTemporaryFile(delete=False)
    try:
        temp_file.write(response.content)
        temp_file.close()
        return temp_file.name, filename, is_photo_path(file_path)
    except Exception:
        temp_file.close()
        Path(temp_file.name).unlink(missing_ok=True)
        raise


def send_new_invoice(new_token, chat_id, row, downloaded_file):
    request_id = get_cell(row, REQUEST_ID_COL)
    text = build_invoice_text(row)
    data = {
        "chat_id": str(chat_id),
        "reply_markup": json.dumps(build_paid_keyboard(request_id), ensure_ascii=False),
    }

    if not downloaded_file:
        data["text"] = text
        result = telegram_api(new_token, "sendMessage", data)
        return result["message_id"], ""

    file_path, filename, is_photo = downloaded_file
    method = "sendPhoto" if is_photo else "sendDocument"
    file_field = "photo" if is_photo else "document"
    data["caption"] = text

    with open(file_path, "rb") as file_handle:
        result = telegram_api(
            new_token,
            method,
            data,
            files={file_field: (filename, file_handle)},
        )

    if is_photo:
        new_file_id = result["photo"][-1]["file_id"]
    else:
        new_file_id = result["document"]["file_id"]

    return result["message_id"], new_file_id


def delete_old_message(old_token, chat_id, message_id):
    if not chat_id or not message_id:
        return "skipped", "missing old chat_id/message_id"

    telegram_api(
        old_token,
        "deleteMessage",
        {"chat_id": str(chat_id), "message_id": str(message_id)},
    )
    return "deleted", ""


def connect_sheets():
    credentials_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not credentials_json:
        raise RuntimeError("GOOGLE_CREDENTIALS is not set")

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(credentials_json),
        scope,
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open(SPREADSHEET_NAME)
    requests_sheet = spreadsheet.worksheet(REQUESTS_SHEET_NAME)

    try:
        logs_sheet = spreadsheet.worksheet(LOGS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        logs_sheet = spreadsheet.add_worksheet(title=LOGS_SHEET_NAME, rows=1000, cols=len(LOG_HEADERS))

    ensure_log_headers(logs_sheet)
    return requests_sheet, logs_sheet


def ensure_log_headers(logs_sheet):
    first_row = logs_sheet.row_values(1)
    if first_row[: len(LOG_HEADERS)] == LOG_HEADERS:
        return

    logs_sheet.update([LOG_HEADERS], range_name="A1:N1")


def append_log(logs_sheet, entry):
    logs_sheet.append_row(
        [entry.get(header, "") for header in LOG_HEADERS],
        value_input_option="USER_ENTERED",
    )


def update_request_row(requests_sheet, sheet_row, new_chat_id, new_message_id, new_file_id):
    updates = [
        {
            "range": gspread.utils.rowcol_to_a1(sheet_row, LAST_INVOICE_MESSAGE_CHAT_ID_COL + 1),
            "values": [[str(new_chat_id)]],
        },
        {
            "range": gspread.utils.rowcol_to_a1(sheet_row, LAST_INVOICE_MESSAGE_ID_COL + 1),
            "values": [[str(new_message_id)]],
        },
    ]

    if new_file_id:
        updates.append({
            "range": gspread.utils.rowcol_to_a1(sheet_row, FILE_ID_COL + 1),
            "values": [[new_file_id]],
        })

    requests_sheet.batch_update(updates)


def collect_candidates(rows, request_ids, limit):
    candidates = []

    for sheet_row, row in enumerate(rows[1:], start=2):
        request_id = get_cell(row, REQUEST_ID_COL)
        if request_ids and request_id not in request_ids:
            continue

        if get_cell(row, STATUS_COL) != STATUS_APPROVED:
            continue

        chat_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_CHAT_ID_COL))
        if not chat_id:
            chat_id = parse_int(get_cell(row, APPROVER_CHAT_ID_COL))

        if not chat_id:
            logging.warning("Skipping request %s: missing target chat id", request_id)
            continue

        candidates.append((sheet_row, row, chat_id))

        if limit and len(candidates) >= limit:
            break

    return candidates


def print_summary(candidates, mode):
    by_chat = defaultdict(list)
    with_files = 0

    for sheet_row, row, chat_id in candidates:
        by_chat[chat_id].append((sheet_row, row))
        if get_cell(row, FILE_ID_COL):
            with_files += 1

    print(f"{mode.upper()}: found {len(candidates)} approved unpaid invoices")
    print(f"Chats: {len(by_chat)}")
    print(f"With files: {with_files}")
    print(f"Without files: {len(candidates) - with_files}")

    for chat_id, items in by_chat.items():
        print("")
        print(f"Chat: {chat_id}")
        for sheet_row, row in items:
            print(
                "  "
                f"row {sheet_row} | "
                f"#{get_cell(row, REQUEST_ID_COL)} | "
                f"project: {get_cell(row, 3)} | "
                f"amount: {get_cell(row, 5)} | "
                f"target: {get_cell(row, 4)}"
            )


def log_candidate(logs_sheet, run_id, mode, sheet_row, row, chat_id, action, result, error="", new_message_id=""):
    append_log(logs_sheet, {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "request_id": get_cell(row, REQUEST_ID_COL),
        "sheet_row": str(sheet_row),
        "chat_id": str(chat_id),
        "project": get_cell(row, 3),
        "target": get_cell(row, 4),
        "amount": get_cell(row, 5),
        "old_message_id": get_cell(row, LAST_INVOICE_MESSAGE_ID_COL),
        "new_message_id": str(new_message_id or ""),
        "action": action,
        "result": result,
        "error": error,
    })


def migrate_candidate(args, requests_sheet, logs_sheet, run_id, old_token, new_token, sheet_row, row, chat_id):
    request_id = get_cell(row, REQUEST_ID_COL)
    old_message_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_ID_COL))
    file_id = get_cell(row, FILE_ID_COL)
    downloaded_file = None

    try:
        if file_id:
            downloaded_file = download_old_file(old_token, file_id)

        new_message_id, new_file_id = send_new_invoice(new_token, chat_id, row, downloaded_file)
        update_request_row(requests_sheet, sheet_row, chat_id, new_message_id, new_file_id)

        print(f"MIGRATE #{request_id}: sent to chat {chat_id}, message_id={new_message_id}")
        log_candidate(logs_sheet, run_id, "run", sheet_row, row, chat_id, "send_new", "sent", new_message_id=new_message_id)

        if args.keep_old:
            log_candidate(logs_sheet, run_id, "run", sheet_row, row, chat_id, "delete_old", "skipped", "keep_old enabled")
            return

        try:
            delete_result, delete_error = delete_old_message(old_token, chat_id, old_message_id)
            print(f"MIGRATE #{request_id}: old message {delete_result}")
            log_candidate(logs_sheet, run_id, "run", sheet_row, row, chat_id, "delete_old", delete_result, delete_error, new_message_id)
        except Exception as exc:
            print(f"MIGRATE #{request_id}: old message not deleted: {exc}")
            log_candidate(logs_sheet, run_id, "run", sheet_row, row, chat_id, "delete_old", "failed", str(exc), new_message_id)
    except Exception as exc:
        print(f"MIGRATE #{request_id}: failed: {exc}")
        log_candidate(logs_sheet, run_id, "run", sheet_row, row, chat_id, "send_new", "failed", str(exc))
    finally:
        if downloaded_file:
            Path(downloaded_file[0]).unlink(missing_ok=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Migrate active approved invoices from old Telegram bot to new Telegram bot.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Only list invoices and write dry-run logs. Default mode.")
    mode.add_argument("--run", action="store_true", help="Send invoices with NEW_BOT_TOKEN and update Google Sheets.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of invoices to process.")
    parser.add_argument("--request-id", action="append", default=[], help="Process only this request id. Can be passed more than once.")
    parser.add_argument("--keep-old", action="store_true", help="Do not try to delete old bot messages in --run mode.")
    return parser.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    mode = "run" if args.run else "dry-run"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    old_token = os.environ.get("BOT_TOKEN")
    new_token = os.environ.get("NEW_BOT_TOKEN")

    if not old_token:
        raise RuntimeError("BOT_TOKEN is not set")
    if args.run and not new_token:
        raise RuntimeError("NEW_BOT_TOKEN is required for --run")
    if args.run and old_token == new_token:
        raise RuntimeError("BOT_TOKEN and NEW_BOT_TOKEN must be different")

    requests_sheet, logs_sheet = connect_sheets()
    rows = requests_sheet.get_all_values()
    candidates = collect_candidates(rows, set(args.request_id), args.limit)

    print_summary(candidates, mode)

    for sheet_row, row, chat_id in candidates:
        if mode == "dry-run":
            log_candidate(logs_sheet, run_id, "dry-run", sheet_row, row, chat_id, "candidate", "found")
            continue

        migrate_candidate(args, requests_sheet, logs_sheet, run_id, old_token, new_token, sheet_row, row, chat_id)

    if mode == "dry-run":
        print("")
        print("Dry-run complete. No Telegram messages were sent and requests sheet was not changed.")
    else:
        print("")
        print("Migration run complete. Check logs sheet for details.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
