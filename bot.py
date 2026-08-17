import asyncio
import logging
import gspread
import cgi
import hashlib
import hmac
import requests
import re
import subprocess
import sys
import time
from oauth2client.service_account import ServiceAccountCredentials
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    WebAppInfo
)
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from threading import Lock, Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlparse

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from payment_schedule import (
    format_payment_date,
    parse_payment_date,
    should_dispatch_payment,
)
from taxi_reimbursements import (
    format_taxi_amount,
    format_taxi_period,
    group_taxi_entries,
    is_taxi_summary_time,
    parse_taxi_amount,
    taxi_period_for_run_date,
    taxi_summary_key,
)
from dds_integration import (
    CURRENCY_KGS,
    CURRENCY_RUB,
    CURRENCY_USD,
    DDS_CHAT_IDS,
    add_message_link,
    build_bot_invoice_candidate,
    build_media_reference_candidate,
    event_is_in_scope,
    event_key,
    parse_standalone_payment,
    telegram_message_link,
)
from dds_writer import DdsWriter, is_retryable_dds_error
from miniapp_dashboard import (
    build_dashboard,
    user_can_manage,
)
from receipt_ocr import (
    OcrFailed,
    OcrUnavailable,
    choose_payment_candidate,
    extract_text as extract_receipt_text,
    tesseract_available,
)

TOKEN = os.getenv("BOT_TOKEN")
DDS_ENABLED = os.getenv("DDS_ENABLED", "true").lower() == "true"
DDS_START_AT_TEXT = os.getenv(
    "DDS_START_AT",
    "2026-08-04T06:17:42+00:00",
)
DDS_START_AT = datetime.fromisoformat(DDS_START_AT_TEXT)
if DDS_START_AT.tzinfo is None:
    DDS_START_AT = DDS_START_AT.replace(tzinfo=timezone.utc)
DDS_WRITE_START_ROW = int(os.getenv("DDS_WRITE_START_ROW", "606"))
DDS_RELEASE_KEY = "miniapp-dashboard-ocr-shadow-v1"
DDS_RETRY_DELAYS = (2, 5, 15, 30, 60)
MINIAPP_MAX_UPLOAD_BYTES = int(os.getenv("MINIAPP_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MINIAPP_INIT_DATA_MAX_AGE_SECONDS = int(
    os.getenv("MINIAPP_INIT_DATA_MAX_AGE_SECONDS", "86400")
)
DDS_OCR_ENABLED = os.getenv("DDS_OCR_ENABLED", "false").lower() == "true"
DDS_OCR_MODE = os.getenv("DDS_OCR_MODE", "shadow").strip().lower()
if DDS_OCR_MODE not in {"shadow", "write"}:
    DDS_OCR_MODE = "shadow"
DDS_OCR_TIMEOUT_SECONDS = int(os.getenv("DDS_OCR_TIMEOUT_SECONDS", "8"))
DDS_OCR_MAX_FILE_BYTES = int(os.getenv("DDS_OCR_MAX_FILE_BYTES", str(5 * 1024 * 1024)))
DDS_OCR_MAX_PIXELS = int(os.getenv("DDS_OCR_MAX_PIXELS", "4000000"))
DDS_OCR_LANGUAGES = os.getenv("DDS_OCR_LANGUAGES", "rus+eng")
DDS_OCR_COMMAND = os.getenv("DDS_OCR_COMMAND", "tesseract")
DDS_OCR_TEXT_LIMIT = int(os.getenv("DDS_OCR_TEXT_LIMIT", "8000"))
DDS_WALLETS_BY_USERNAME = {
    "n0visad": {
        CURRENCY_KGS: "Александр KGS",
        CURRENCY_RUB: "Александр",
        CURRENCY_USD: "Александр $",
    },
    "bulat_sufyanov": {
        CURRENCY_KGS: "Булат KGS",
        CURRENCY_RUB: "Булат",
        CURRENCY_USD: "Булат $",
    },
    "kirillvorontcov": {
        CURRENCY_KGS: "Офис подотчет",
    },
}
DDS_WALLETS_BY_USER = {
    "375842023": DDS_WALLETS_BY_USERNAME["n0visad"],
    "38038661": DDS_WALLETS_BY_USERNAME["bulat_sufyanov"],
    "1525565778": DDS_WALLETS_BY_USERNAME["kirillvorontcov"],
}
DDS_WALLETS_BY_USERNAME["булат суфьянов"] = DDS_WALLETS_BY_USERNAME["bulat_sufyanov"]
DDS_WALLETS_BY_USERNAME["булат суфьянов инвестиции инфобиз"] = (
    DDS_WALLETS_BY_USERNAME["bulat_sufyanov"]
)
DDS_DEFAULT_CURRENCY_BY_CHAT = {
    chat_id: CURRENCY_KGS
    for chat_id in DDS_CHAT_IDS
}

REQUEST_ID_COL = 0
STATUS_COL = 7
APPROVER_CHAT_ID_COL = 8
FILE_ID_COL = 9
CREATOR_CHAT_ID_COL = 10
APPROVER_NAME_COL = 12
PAYER_TAG_COL = 13
APPROVED_AT_COL = 14
PAYMENT_CHAT_ID_COL = 15
PAYMENT_PAYER_TAG_COL = 16
PAYMENT_RECEIPT_FILE_ID_COL = 17
PAYMENT_RECEIPT_FILE_TYPE_COL = 18
WORKFLOW_KEY_COL = 19
LAST_INVOICE_MESSAGE_CHAT_ID_COL = 20
LAST_INVOICE_MESSAGE_ID_COL = 21
EXPENSE_CATEGORY_COL = 22
PAYMENT_DUE_DATE_COL = 23
PAYMENT_SENT_AT_COL = 24
PAYMENT_MESSAGE_ID_COL = 25
APPROVAL_LAST_SENT_AT_COL = 26
APPROVAL_REMINDER_TIMESTAMP_PREFIX = "approval-reminder:"

STATUS_APPROVED = "Согласован"
STATUS_PENDING_APPROVAL = "На согласовании"
STATUS_PAID = "Оплачено"
STATUS_REJECTED = "Отклонен"
STATUS_CANCELLED = "Отменен"
REMINDER_TIMEZONE_NAME = os.getenv("REMINDER_TIMEZONE", "Asia/Bishkek")
PAYMENT_DISPATCH_HOUR = int(os.getenv("PAYMENT_DISPATCH_HOUR", os.getenv("REMINDER_HOUR", "10")))
PAYMENT_DISPATCH_MINUTE = int(os.getenv("PAYMENT_DISPATCH_MINUTE", os.getenv("REMINDER_MINUTE", "0")))
PAYMENT_DISPATCH_INTERVAL_SECONDS = int(os.getenv("PAYMENT_DISPATCH_INTERVAL_SECONDS", "300"))
APPROVAL_REMINDER_TIMEZONE_NAME = os.getenv("APPROVAL_REMINDER_TIMEZONE", "Asia/Novosibirsk")
APPROVAL_REMINDER_HOUR = int(os.getenv("APPROVAL_REMINDER_HOUR", "11"))
APPROVAL_REMINDER_MINUTE = int(os.getenv("APPROVAL_REMINDER_MINUTE", "0"))
APPROVAL_REMINDER_INTERVAL_SECONDS = int(os.getenv("APPROVAL_REMINDER_INTERVAL_SECONDS", "300"))
EXPENSE_CATEGORIES = [
    ("team", "Команда"),
    ("ads", "Рекламный бюджет"),
    ("services", "Сервисы"),
    ("taxi", "Такси")
]
EXPENSE_CATEGORY_BY_KEY = dict(EXPENSE_CATEGORIES)
EXPENSE_CATEGORY_LABELS = [label for _, label in EXPENSE_CATEGORIES]
TAXI_EXPENSE_CATEGORY = EXPENSE_CATEGORY_BY_KEY["taxi"]
TAXI_SUMMARY_KEY_PREFIX = "taxi-summary|"
try:
    REMINDER_TZ = ZoneInfo(REMINDER_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    REMINDER_TZ = timezone.utc

try:
    APPROVAL_REMINDER_TZ = ZoneInfo(APPROVAL_REMINDER_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    APPROVAL_REMINDER_TZ = timezone.utc

# Google Sheets настройка
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]

import os
import json
from oauth2client.service_account import ServiceAccountCredentials

creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet = client.open("Finance bot").worksheet("requests")

projects_sheet = client.open("Finance bot").worksheet("projects")

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
reject_state = {}
user_state = {}
payment_state = {}
payment_dispatch_claims = set()
approval_resend_claims = set()
dds_linked_receipt_events = set()
miniapp_request_locks = {}
miniapp_request_locks_guard = Lock()
ocr_job_lock = Lock()
OCR_RUNTIME_AVAILABLE = DDS_OCR_ENABLED and tesseract_available(DDS_OCR_COMMAND)
if DDS_OCR_ENABLED:
    if OCR_RUNTIME_AVAILABLE:
        logging.info("Receipt OCR is available in %s mode", DDS_OCR_MODE)
    else:
        logging.warning(
            "Receipt OCR is enabled but Tesseract is unavailable; DDS will use existing parsing"
        )
dds_writer = None
if DDS_ENABLED:
    try:
        dds_writer = DdsWriter(
            client,
            start_row=DDS_WRITE_START_ROW,
            wallets_by_user=DDS_WALLETS_BY_USER,
            wallets_by_username=DDS_WALLETS_BY_USERNAME,
            activation_time=DDS_START_AT,
            release_key=DDS_RELEASE_KEY,
        )
        logging.info(
            "DDS integration enabled from %s for chats %s",
            DDS_START_AT.isoformat(),
            sorted(DDS_CHAT_IDS),
        )
    except Exception:
        logging.exception("DDS integration could not be initialized; bot will continue without it")
BASE_DIR = Path(__file__).resolve().parent
MINIAPP_REQUIRE_INIT_DATA = os.getenv("MINIAPP_REQUIRE_INIT_DATA", "true").lower() != "false"
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
if not WEBAPP_URL and os.getenv("RENDER_EXTERNAL_URL"):
    WEBAPP_URL = os.getenv("RENDER_EXTERNAL_URL").rstrip("/") + "/miniapp"
MIGRATION_SECRET = os.getenv("MIGRATION_SECRET", "").strip()
MIGRATION_TIMEOUT_SECONDS = int(os.getenv("MIGRATION_TIMEOUT_SECONDS", "600"))

def get_cell(row, index, default=""):
    return row[index].strip() if len(row) > index and row[index] else default

def set_cell(row, index, value):
    while len(row) <= index:
        row.append("")
    row[index] = value

def parse_iso_date(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None

def parse_iso_datetime(value, target_timezone):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=target_timezone)

    return parsed.astimezone(target_timezone)

def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def is_photo_file(file_id):
    return file_id.startswith(("Ag", "AQ"))

def build_paid_keyboard(request_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Оплатил – прикрепить чек", callback_data=f"paid_{request_id}")
        ],
        [
            InlineKeyboardButton("❌ Отменить счет", callback_data=f"cancel_{request_id}")
        ]
    ])

def build_approval_keyboard(request_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{request_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{request_id}")
    ]])

def build_payment_received_keyboard(request_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data=f"received_yes_{request_id}"),
            InlineKeyboardButton("❌ Нет", callback_data=f"received_no_{request_id}")
        ]
    ])

def build_expense_category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"expense_{key}")]
        for key, label in EXPENSE_CATEGORIES
    ])

def build_comment_prompt(expense_category):
    if expense_category == TAXI_EXPENSE_CATEGORY:
        return (
            "Введите комментарий:\n\n"
            "*Пример*\n\n"
            "??? сом - итоговая сумма за такси\n"
            "Цель поездки: ???"
        )

    return (
        "Введите комментарий:\n\n"
        "*Пример*\n"
        "??? сом - фиксированная часть за ...-...\n"
        "??? сом - KPI за *месяц*\n"
        "??? сом - % за *месяц*\n\n"
        "??? сом - итоговая сумма к оплате\n\n"
        "или\n\n"
        "??? сом - услуга\n\n"
        "перевод на карту 'номер телефона, банк' (если оплата не по счету)"
    )

def get_user_tag(user):
    if user.username:
        return f"@{user.username}"
    return user.first_name


def get_dds_event_time(message):
    event_time = message.date if message and message.date else datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    return event_time.astimezone(REMINDER_TZ)


async def write_dds_candidate(
    candidate,
    event_key_value,
    event_time,
    chat_id,
    message_id,
    user_id,
    username,
    request_id="",
):
    if not dds_writer:
        return None
    if not event_is_in_scope(
        chat_id,
        event_time,
        DDS_ENABLED,
        DDS_START_AT,
    ):
        return None

    attempt = 0
    while True:
        try:
            result = await asyncio.to_thread(
                dds_writer.record_candidate,
                event_key_value,
                event_time,
                candidate,
                chat_id,
                message_id,
                user_id,
                username or "",
                request_id,
            )
            logging.info(
                "DDS event %s finished with status %s, row %s",
                event_key_value,
                result["status"],
                result["dds_row"],
            )
            return result
        except Exception as exc:
            if attempt >= len(DDS_RETRY_DELAYS) or not is_retryable_dds_error(exc):
                logging.exception("DDS event %s failed", event_key_value)
                return None

            delay = DDS_RETRY_DELAYS[attempt]
            attempt += 1
            logging.warning(
                "DDS event %s hit a temporary error; retry %s/%s in %s seconds: %s",
                event_key_value,
                attempt,
                len(DDS_RETRY_DELAYS),
                delay,
                exc,
            )
            await asyncio.sleep(delay)


async def write_paid_invoice_to_dds(update, row, request_id, payment_chat_id):
    try:
        candidate = build_bot_invoice_candidate(
            request_id,
            get_cell(row, 5),
            get_cell(row, 4),
            get_cell(row, 6),
            default_currency=DDS_DEFAULT_CURRENCY_BY_CHAT.get(payment_chat_id),
        )
    except Exception:
        logging.exception(
            "Could not build DDS candidate for paid request %s",
            request_id,
        )
        return

    await write_dds_candidate(
        candidate,
        f"invoice:{request_id}",
        get_dds_event_time(update.message),
        payment_chat_id,
        update.message.message_id,
        update.effective_user.id,
        update.effective_user.username or update.effective_user.full_name,
        request_id=request_id,
    )


def ocr_image_source(message):
    if message.photo:
        photo = message.photo[-1]
        return photo, "image/jpeg", photo.file_size or 0
    if message.document and str(message.document.mime_type or "").startswith("image/"):
        return (
            message.document,
            message.document.mime_type,
            message.document.file_size or 0,
        )
    return None


async def record_ocr_diagnostic(
    event_time,
    chat_id,
    message_id,
    user_id,
    username,
    decision,
    ocr_result=None,
    error_reason="",
):
    if not dds_writer:
        return
    candidate = decision.candidate if decision else None
    reason_parts = [error_reason or (decision.reason if decision else "ocr_failed")]
    if decision:
        reason_parts.append(f"confidence={decision.confidence}")
    if ocr_result:
        reason_parts.extend([
            f"duration={ocr_result.duration_seconds:.3f}s",
            f"image={ocr_result.width}x{ocr_result.height}",
        ])
    status = "ocr_shadow_candidate" if candidate else "ocr_shadow_no_candidate"
    await asyncio.to_thread(
        dds_writer.record_diagnostic,
        f"ocr:{chat_id}:{message_id}",
        event_time,
        status,
        "standalone_receipt_ocr",
        chat_id,
        message_id,
        user_id,
        username,
        candidate.currency if candidate else "",
        candidate.amount if candidate else "",
        "; ".join(reason_parts),
        (ocr_result.text if ocr_result else "")[:DDS_OCR_TEXT_LIMIT],
    )


async def process_standalone_receipt_ocr(
    bot,
    update,
    base_candidate,
    message_link,
    event_time,
    payer_id,
    payer_username,
):
    message = update.effective_message
    source = ocr_image_source(message)
    if not source:
        return
    telegram_media, _mime_type, file_size = source
    if file_size and file_size > DDS_OCR_MAX_FILE_BYTES:
        if DDS_OCR_MODE == "shadow":
            await record_ocr_diagnostic(
                event_time,
                update.effective_chat.id,
                message.message_id,
                payer_id,
                payer_username,
                None,
                error_reason="file_too_large",
            )
        elif base_candidate:
            await write_dds_candidate(
                base_candidate,
                f"message:{event_key(update.effective_chat.id, message.message_id)}",
                event_time,
                update.effective_chat.id,
                message.message_id,
                payer_id,
                payer_username,
            )
        return

    if not ocr_job_lock.acquire(blocking=False):
        logging.info("OCR skipped because another receipt is running: %s", message.message_id)
        if DDS_OCR_MODE == "shadow":
            await record_ocr_diagnostic(
                event_time,
                update.effective_chat.id,
                message.message_id,
                payer_id,
                payer_username,
                None,
                error_reason="ocr_busy",
            )
        elif base_candidate:
            await write_dds_candidate(
                base_candidate,
                f"message:{event_key(update.effective_chat.id, message.message_id)}",
                event_time,
                update.effective_chat.id,
                message.message_id,
                payer_id,
                payer_username,
            )
        return

    try:
        telegram_file = await bot.get_file(telegram_media.file_id)
        image_bytes = bytes(await telegram_file.download_as_bytearray())
        result = await asyncio.to_thread(
            extract_receipt_text,
            image_bytes,
            DDS_OCR_TIMEOUT_SECONDS,
            DDS_OCR_MAX_PIXELS,
            DDS_OCR_LANGUAGES,
            DDS_OCR_COMMAND,
        )
        decision = choose_payment_candidate(
            message.text or message.caption or "",
            result.text,
            default_currency=DDS_DEFAULT_CURRENCY_BY_CHAT.get(update.effective_chat.id),
        )
        logging.info(
            "OCR message %s finished in %.3fs: %s (%s)",
            message.message_id,
            result.duration_seconds,
            decision.reason,
            decision.confidence,
        )

        if DDS_OCR_MODE == "shadow":
            await record_ocr_diagnostic(
                event_time,
                update.effective_chat.id,
                message.message_id,
                payer_id,
                payer_username,
                decision,
                ocr_result=result,
            )
            return

        candidate = decision.candidate
        if candidate:
            candidate = add_message_link(candidate, message_link)
        else:
            candidate = base_candidate
        if candidate:
            await write_dds_candidate(
                candidate,
                f"message:{event_key(update.effective_chat.id, message.message_id)}",
                event_time,
                update.effective_chat.id,
                message.message_id,
                payer_id,
                payer_username,
            )
    except (OcrUnavailable, OcrFailed, ValueError) as exc:
        logging.warning("OCR failed for message %s: %s", message.message_id, exc)
        if DDS_OCR_MODE == "shadow":
            await record_ocr_diagnostic(
                event_time,
                update.effective_chat.id,
                message.message_id,
                payer_id,
                payer_username,
                None,
                error_reason=f"ocr_failed:{type(exc).__name__}",
            )
        elif base_candidate:
            await write_dds_candidate(
                base_candidate,
                f"message:{event_key(update.effective_chat.id, message.message_id)}",
                event_time,
                update.effective_chat.id,
                message.message_id,
                payer_id,
                payer_username,
            )
    except Exception:
        logging.exception("Unexpected OCR failure for message %s", message.message_id)
        if DDS_OCR_MODE == "write" and base_candidate:
            await write_dds_candidate(
                base_candidate,
                f"message:{event_key(update.effective_chat.id, message.message_id)}",
                event_time,
                update.effective_chat.id,
                message.message_id,
                payer_id,
                payer_username,
            )
    finally:
        ocr_job_lock.release()


async def handle_dds_standalone_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    sender_chat = message.sender_chat if message else None
    if not message or (not user and not sender_chat):
        return
    if user and user.is_bot and not sender_chat:
        return

    if sender_chat:
        payer_id = sender_chat.id
        payer_username = sender_chat.username or sender_chat.title
    else:
        payer_id = user.id
        payer_username = user.username or user.full_name

    chat_id = update.effective_chat.id
    event_time = get_dds_event_time(message)
    if not event_is_in_scope(
        chat_id,
        event_time,
        DDS_ENABLED,
        DDS_START_AT,
    ):
        return

    message_event_key = event_key(chat_id, message.message_id)
    if message_event_key in dds_linked_receipt_events:
        dds_linked_receipt_events.discard(message_event_key)
        return

    text = message.text or message.caption or ""
    has_media = bool(message.document or message.photo)
    message_link = telegram_message_link(
        chat_id,
        message.message_id,
        update.effective_chat.username,
    )
    decision = parse_standalone_payment(
        text,
        has_media=has_media,
        default_currency=DDS_DEFAULT_CURRENCY_BY_CHAT.get(chat_id),
    )
    candidate = None
    if decision.accepted:
        candidate = add_message_link(decision.candidate, message_link)
    elif has_media and text.strip():
        candidate = build_media_reference_candidate(
            text,
            message_link,
            default_currency=DDS_DEFAULT_CURRENCY_BY_CHAT.get(chat_id),
        )
    ocr_eligible = bool(
        DDS_OCR_ENABLED
        and OCR_RUNTIME_AVAILABLE
        and ocr_image_source(message)
    )
    defer_base_write = bool(
        ocr_eligible
        and DDS_OCR_MODE == "write"
        and (candidate is None or candidate.amount is None)
    )
    if candidate is not None and not defer_base_write:
        context.application.create_task(
            write_dds_candidate(
                candidate,
                f"message:{message_event_key}",
                event_time,
                chat_id,
                message.message_id,
                payer_id,
                payer_username,
            ),
            update=update,
        )
    if ocr_eligible:
        context.application.create_task(
            process_standalone_receipt_ocr(
                context.bot,
                update,
                candidate,
                message_link,
                event_time,
                payer_id,
                payer_username,
            ),
            update=update,
        )

def format_user_tag(value):
    value = str(value or "").strip()
    if not value:
        return "неизвестно"
    if value.startswith("@") or " " in value:
        return value
    return f"@{value}"


def callback_matches_message(row, chat_id, message_id, stage):
    if stage == "approval":
        expected_chat_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_CHAT_ID_COL))
        expected_message_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_ID_COL))
    else:
        expected_chat_id = parse_int(get_cell(row, PAYMENT_CHAT_ID_COL))
        expected_message_id = parse_int(get_cell(row, PAYMENT_MESSAGE_ID_COL))
        if not expected_message_id:
            expected_chat_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_CHAT_ID_COL))
            expected_message_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_ID_COL))

    return expected_chat_id == int(chat_id) and expected_message_id == int(message_id)

def get_expense_category(row):
    return get_cell(row, EXPENSE_CATEGORY_COL, "Без статьи")

def is_taxi_invoice(row):
    return get_expense_category(row) == TAXI_EXPENSE_CATEGORY


def is_taxi_summary(row):
    return (
        is_taxi_invoice(row)
        and get_cell(row, WORKFLOW_KEY_COL).startswith(TAXI_SUMMARY_KEY_PREFIX)
    )


def get_created_at(row):
    value = get_cell(row, 1)
    if not value:
        return None

    try:
        created_at = datetime.fromisoformat(value)
    except ValueError:
        return None

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=REMINDER_TZ)

    return created_at.astimezone(REMINDER_TZ)


def format_approval_reminder_timestamp(value):
    return f"{APPROVAL_REMINDER_TIMESTAMP_PREFIX}{value.isoformat()}"

def get_approval_last_sent_at(row):
    value = get_cell(row, APPROVAL_LAST_SENT_AT_COL)
    if not value.startswith(APPROVAL_REMINDER_TIMESTAMP_PREFIX):
        return None

    return parse_iso_datetime(
        value[len(APPROVAL_REMINDER_TIMESTAMP_PREFIX):],
        APPROVAL_REMINDER_TZ
    )

def is_approval_reminder_due(row, now):
    if get_cell(row, STATUS_COL) != STATUS_PENDING_APPROVAL:
        return False

    last_sent_at = get_approval_last_sent_at(row)
    if last_sent_at is None or last_sent_at.date() >= now.date():
        return False

    return (now.hour, now.minute) >= (
        APPROVAL_REMINDER_HOUR,
        APPROVAL_REMINDER_MINUTE,
    )

def get_invoice_payer_tag(row):
    return get_cell(row, PAYER_TAG_COL)

def get_payment_due_date(row):
    return parse_iso_date(get_cell(row, PAYMENT_DUE_DATE_COL))


def get_payment_date_text(row):
    payment_due_date = get_payment_due_date(row)
    return format_payment_date(payment_due_date) if payment_due_date else "не указана"


def build_invoice_details(row):
    parts = []
    if not is_taxi_invoice(row):
        parts.append(f"Дата оплаты: {get_payment_date_text(row)}")
    parts.extend([
        get_cell(row, 4),
        get_cell(row, 6),
    ])
    return "\n\n".join(part for part in parts if part)


def build_pending_approval_invoice_text(row):
    return (
        f"Новый счет #{get_cell(row, REQUEST_ID_COL)}\n\n"
        f"{build_invoice_details(row)}"
    )


def build_approved_approval_text(row):
    approver_name = get_cell(row, APPROVER_NAME_COL, "неизвестно")
    return (
        f"Счет #{get_cell(row, REQUEST_ID_COL)} — Согласован✅\n\n"
        f"{build_invoice_details(row)}\n\n"
        f"Согласовано: {format_user_tag(approver_name)}"
    )


def build_payment_invoice_text(row):
    payer_tag = get_invoice_payer_tag(row)
    approver_name = get_cell(row, APPROVER_NAME_COL, "неизвестно")
    return (
        f"{payer_tag}\n"
        f"Счет #{get_cell(row, REQUEST_ID_COL)} — К оплате\n\n"
        f"{build_invoice_details(row)}\n\n"
        f"Согласовано: {format_user_tag(approver_name)}"
    )


def build_paid_invoice_text(row, payer_tag):
    approver_name = get_cell(row, APPROVER_NAME_COL, "неизвестно")
    return (
        f"Счет #{get_cell(row, REQUEST_ID_COL)} — Оплачен✅\n\n"
        f"{build_invoice_details(row)}\n\n"
        f"Согласовано: {format_user_tag(approver_name)}\n"
        f"Оплачено: {payer_tag}"
    )


def build_closed_invoice_text(row, status, reason):
    marker = "Отклонен❌" if status == STATUS_REJECTED else "Отменен❌"
    return (
        f"Счет #{get_cell(row, REQUEST_ID_COL)} — {marker}\n\n"
        f"{build_invoice_details(row)}\n\n"
        f"Причина: {reason}"
    )

async def _send_pending_approval_invoice_once(bot, chat_id, row):
    request_id = get_cell(row, REQUEST_ID_COL)
    file_id = get_cell(row, FILE_ID_COL)
    text = build_pending_approval_invoice_text(row)
    keyboard = build_approval_keyboard(request_id)

    if file_id:
        try:
            if is_photo_file(file_id):
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=file_id,
                    caption=text,
                    reply_markup=keyboard
                )

            return await bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=text,
                reply_markup=keyboard
            )
        except Exception as exc:
            if parse_int(getattr(exc, "new_chat_id", None)):
                raise
            logging.exception("Could not send pending approval file for request %s", request_id)

    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard
    )


async def send_pending_approval_invoice(bot, chat_id, row):
    try:
        return await _send_pending_approval_invoice_once(bot, chat_id, row)
    except Exception as exc:
        migrated_chat_id = parse_int(getattr(exc, "new_chat_id", None))
        if not migrated_chat_id:
            raise

        replace_migrated_project_chat_id(chat_id, migrated_chat_id)
        set_cell(row, APPROVER_CHAT_ID_COL, str(migrated_chat_id))
        return await _send_pending_approval_invoice_once(bot, migrated_chat_id, row)


async def _send_payment_invoice_once(bot, chat_id, row):
    request_id = get_cell(row, REQUEST_ID_COL)
    file_id = get_cell(row, FILE_ID_COL)
    text = build_payment_invoice_text(row)
    keyboard = build_paid_keyboard(request_id)

    if file_id:
        if is_photo_file(file_id):
            return await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=text,
                reply_markup=keyboard
            )

        return await bot.send_document(
            chat_id=chat_id,
            document=file_id,
            caption=text,
            reply_markup=keyboard
        )

    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard
    )


async def send_payment_invoice(bot, chat_id, row):
    try:
        return await _send_payment_invoice_once(bot, chat_id, row)
    except Exception as exc:
        migrated_chat_id = parse_int(getattr(exc, "new_chat_id", None))
        if not migrated_chat_id:
            raise

        replace_migrated_project_chat_id(chat_id, migrated_chat_id)
        return await _send_payment_invoice_once(bot, migrated_chat_id, row)

async def edit_invoice_message(bot, chat_id, message_id, row, text, reply_markup=None):
    try:
        return await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=text,
            reply_markup=reply_markup
        )
    except Exception:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup
        )


def save_paid_receipt(sheet_row_number, row, chat_id, payer_tag, file_id, file_type):
    payment_values = [
        get_cell(row, column)
        for column in range(STATUS_COL, PAYMENT_RECEIPT_FILE_TYPE_COL + 1)
    ]
    payment_values[STATUS_COL - STATUS_COL] = STATUS_PAID
    payment_values[PAYMENT_CHAT_ID_COL - STATUS_COL] = str(chat_id)
    payment_values[PAYMENT_PAYER_TAG_COL - STATUS_COL] = payer_tag
    payment_values[PAYMENT_RECEIPT_FILE_ID_COL - STATUS_COL] = file_id
    payment_values[PAYMENT_RECEIPT_FILE_TYPE_COL - STATUS_COL] = file_type

    sheet.update(
        values=[payment_values],
        range_name=f"H{sheet_row_number}:S{sheet_row_number}",
        raw=True
    )

    set_cell(row, STATUS_COL, STATUS_PAID)
    set_cell(row, PAYMENT_CHAT_ID_COL, str(chat_id))
    set_cell(row, PAYMENT_PAYER_TAG_COL, payer_tag)
    set_cell(row, PAYMENT_RECEIPT_FILE_ID_COL, file_id)
    set_cell(row, PAYMENT_RECEIPT_FILE_TYPE_COL, file_type)


async def restore_approved_payment_message(bot, sheet_row_number, row, chat_id, message_id):
    sheet.update_cell(sheet_row_number, STATUS_COL + 1, STATUS_APPROVED)
    set_cell(row, STATUS_COL, STATUS_APPROVED)
    await edit_invoice_message(
        bot,
        chat_id,
        message_id,
        row,
        build_payment_invoice_text(row),
        reply_markup=build_paid_keyboard(get_cell(row, REQUEST_ID_COL))
    )


async def notify_creator_invoice_approved(bot, row):
    creator_chat_id = parse_int(get_cell(row, CREATOR_CHAT_ID_COL))
    request_id = get_cell(row, REQUEST_ID_COL)
    target = get_cell(row, 4)

    if not creator_chat_id:
        logging.warning(
            "Could not notify creator about approved invoice %s: missing creator chat id",
            request_id
        )
        return

    if is_taxi_invoice(row):
        text = (
            "✅ Ваш счет согласован:\n\n"
            f"{target}\n\n"
            f"{get_cell(row, 6)}"
        )
    else:
        text = (
            "✅ Ваш счет согласован:\n\n"
            f"{target}\n\n"
            f"Дата оплаты: {get_payment_date_text(row)}"
        )

    try:
        await bot.send_message(chat_id=creator_chat_id, text=text)
    except Exception:
        logging.exception("Could not notify creator about approved invoice %s", request_id)


def save_last_invoice_message_ids(sheet_row_number, chat_id, message_id):
    sheet.update_cell(sheet_row_number, LAST_INVOICE_MESSAGE_CHAT_ID_COL + 1, str(chat_id))
    sheet.update_cell(sheet_row_number, LAST_INVOICE_MESSAGE_ID_COL + 1, str(message_id))


def save_last_invoice_message(sheet_row_number, message):
    save_last_invoice_message_ids(sheet_row_number, message.chat_id, message.message_id)


def save_payment_message(sheet_row_number, message):
    sheet.update_cell(sheet_row_number, PAYMENT_MESSAGE_ID_COL + 1, str(message.message_id))
    sheet.update_cell(sheet_row_number, PAYMENT_CHAT_ID_COL + 1, str(message.chat_id))
    sheet.update_cell(sheet_row_number, PAYMENT_SENT_AT_COL + 1, datetime.now(REMINDER_TZ).isoformat())


async def send_receipt_to_payment_chat(bot, chat_id, file_id, file_type, request_id, reply_to_message_id):
    caption = f"Чек по счету #{request_id}"

    try:
        if file_type == "photo":
            return await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=caption,
                reply_to_message_id=reply_to_message_id
            )

        return await bot.send_document(
            chat_id=chat_id,
            document=file_id,
            caption=caption,
            reply_to_message_id=reply_to_message_id
        )
    except Exception:
        logging.info("Could not send receipt as a reply for request %s", request_id)

    if file_type == "photo":
        return await bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=caption
        )

    return await bot.send_document(
        chat_id=chat_id,
        document=file_id,
        caption=caption
    )


def is_payment_due(row, now):
    payment_due_date = get_payment_due_date(row)
    return (
        not is_taxi_invoice(row)
        and get_cell(row, STATUS_COL) == STATUS_APPROVED
        and payment_due_date is not None
        and not get_cell(row, PAYMENT_MESSAGE_ID_COL)
        and should_dispatch_payment(
            payment_due_date,
            now,
            PAYMENT_DISPATCH_HOUR,
            PAYMENT_DISPATCH_MINUTE
        )
    )


async def send_due_payment_invoice(bot, sheet_row_number, row, now=None):
    now = now or datetime.now(REMINDER_TZ)
    if not is_payment_due(row, now):
        return None

    request_id = get_cell(row, REQUEST_ID_COL)
    if request_id in payment_dispatch_claims:
        return None

    payment_dispatch_claims.add(request_id)
    try:
        project_settings = get_project_settings(get_cell(row, 3))
        payment_chat_id = project_settings.get("payment_chat_id") if project_settings else None
        if not payment_chat_id:
            logging.error(
                "Could not dispatch request %s: payment_chat_id is missing for project %s",
                request_id,
                get_cell(row, 3)
            )
            return None

        sent_message = await send_payment_invoice(bot, payment_chat_id, row)
        save_payment_message(sheet_row_number, sent_message)
        set_cell(row, PAYMENT_CHAT_ID_COL, str(payment_chat_id))
        set_cell(row, PAYMENT_SENT_AT_COL, now.isoformat())
        set_cell(row, PAYMENT_MESSAGE_ID_COL, str(sent_message.message_id))
        logging.info(
            "Dispatched request %s to payment chat %s",
            request_id,
            payment_chat_id
        )
        return sent_message
    finally:
        payment_dispatch_claims.discard(request_id)

def taxi_source_belongs_to_period(row, period_start, period_end_exclusive):
    created_at = get_created_at(row)
    return (
        get_cell(row, STATUS_COL) == STATUS_APPROVED
        and is_taxi_invoice(row)
        and not is_taxi_summary(row)
        and created_at is not None
        and period_start <= created_at.date() < period_end_exclusive
    )


def collect_taxi_summary_groups(rows, period_start, period_end_exclusive):
    entries = []
    for sheet_row_number, row in enumerate(rows[1:], start=2):
        if not taxi_source_belongs_to_period(row, period_start, period_end_exclusive):
            continue

        creator_chat_id = get_cell(row, CREATOR_CHAT_ID_COL)
        project = get_cell(row, 3)
        if not creator_chat_id or not project:
            logging.error(
                "Taxi request %s is missing creator or project",
                get_cell(row, REQUEST_ID_COL)
            )
            continue

        entries.append({
            "project": project,
            "creator_chat_id": creator_chat_id,
            "creator_username": get_cell(row, 2),
            "creator_name": get_cell(row, 11),
            "request_id": get_cell(row, REQUEST_ID_COL),
            "sheet_row_number": sheet_row_number,
            "amount": get_cell(row, 5),
        })

    return group_taxi_entries(entries)


def build_taxi_summary_row(request_id, group, period_start, period_end_exclusive, now, settings):
    creator_label = (
        format_user_tag(group["creator_username"])
        if group["creator_username"]
        else f"ID {group['creator_chat_id']}"
    )
    total_text = format_taxi_amount(group["total"])
    period_text = format_taxi_period(period_start, period_end_exclusive)
    workflow_key = taxi_summary_key(
        period_start,
        period_end_exclusive,
        group["project"],
        group["creator_chat_id"],
    )

    row = [""] * (PAYMENT_MESSAGE_ID_COL + 1)
    set_cell(row, REQUEST_ID_COL, str(request_id))
    set_cell(row, 1, now.isoformat())
    set_cell(row, 2, group["creator_username"])
    set_cell(row, 3, group["project"])
    set_cell(row, 4, f"Компенсация за такси — {creator_label}")
    set_cell(row, 5, f"{total_text} сом")
    set_cell(
        row,
        6,
        f"{total_text} сом - итоговая сумма за такси\n"
        f"Период: {period_text}\n"
        f"Сотрудник: {creator_label}"
    )
    set_cell(row, STATUS_COL, STATUS_PENDING_APPROVAL)
    set_cell(row, APPROVER_CHAT_ID_COL, str(settings["approval_chat_id"]))
    set_cell(row, CREATOR_CHAT_ID_COL, group["creator_chat_id"])
    set_cell(row, 11, group["creator_name"] or group["creator_username"])
    set_cell(row, PAYER_TAG_COL, settings["payer_tag"])
    set_cell(row, WORKFLOW_KEY_COL, workflow_key)
    set_cell(row, EXPENSE_CATEGORY_COL, TAXI_EXPENSE_CATEGORY)
    set_cell(
        row,
        APPROVAL_LAST_SENT_AT_COL,
        format_approval_reminder_timestamp(now.astimezone(APPROVAL_REMINDER_TZ))
    )
    return row


async def ensure_taxi_summary_message(bot, sheet_row_number, row):
    if get_cell(row, LAST_INVOICE_MESSAGE_ID_COL):
        return

    chat_id = parse_int(get_cell(row, APPROVER_CHAT_ID_COL))
    if not chat_id:
        logging.error(
            "Could not send taxi summary %s: approval chat is missing",
            get_cell(row, REQUEST_ID_COL)
        )
        return

    sent_message = await send_pending_approval_invoice(bot, chat_id, row)
    sheet.update_cell(
        sheet_row_number,
        APPROVER_CHAT_ID_COL + 1,
        str(sent_message.chat_id)
    )
    save_last_invoice_message(sheet_row_number, sent_message)


async def send_scheduled_taxi_summaries(context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get("taxi_summary_running"):
        return

    now = datetime.now(REMINDER_TZ)
    if not is_taxi_summary_time(now, PAYMENT_DISPATCH_HOUR, PAYMENT_DISPATCH_MINUTE):
        return

    period = taxi_period_for_run_date(now.date())
    if not period:
        return

    context.application.bot_data["taxi_summary_running"] = True
    try:
        period_start, period_end_exclusive = period
        rows = sheet.get_all_values()
        existing_by_key = {
            get_cell(row, WORKFLOW_KEY_COL): (sheet_row_number, row)
            for sheet_row_number, row in enumerate(rows[1:], start=2)
            if is_taxi_summary(row)
        }
        groups = collect_taxi_summary_groups(rows, period_start, period_end_exclusive)

        for group in groups.values():
            if group["invalid_amounts"]:
                logging.error(
                    "Taxi summary skipped for creator %s, project %s: invalid amounts %s",
                    group["creator_chat_id"],
                    group["project"],
                    group["invalid_amounts"],
                )
                continue
            if group["total"] is None:
                continue

            workflow_key = taxi_summary_key(
                period_start,
                period_end_exclusive,
                group["project"],
                group["creator_chat_id"],
            )
            existing = existing_by_key.get(workflow_key)
            if existing:
                sheet_row_number, summary_row = existing
                if get_cell(summary_row, STATUS_COL) == STATUS_PENDING_APPROVAL:
                    await ensure_taxi_summary_message(
                        context.bot,
                        sheet_row_number,
                        summary_row,
                    )
                continue

            settings = get_project_settings(group["project"])
            if not settings or not settings["approval_chat_id"]:
                logging.error(
                    "Taxi summary skipped for project %s: approval chat is missing",
                    group["project"],
                )
                continue

            request_id = str(len(rows))
            summary_row = build_taxi_summary_row(
                request_id,
                group,
                period_start,
                period_end_exclusive,
                now,
                settings,
            )
            sheet.append_row(summary_row)
            rows.append(summary_row)
            sheet_row_number = len(rows)
            existing_by_key[workflow_key] = (sheet_row_number, summary_row)
            await ensure_taxi_summary_message(context.bot, sheet_row_number, summary_row)
            logging.info(
                "Created taxi summary %s for creator %s, project %s, sources %s",
                request_id,
                group["creator_chat_id"],
                group["project"],
                group["source_request_ids"],
            )
    finally:
        context.application.bot_data["taxi_summary_running"] = False


async def send_scheduled_payments(context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get("payment_dispatch_running"):
        return

    context.application.bot_data["payment_dispatch_running"] = True
    try:
        rows = sheet.get_all_values()
        now = datetime.now(REMINDER_TZ)

        for sheet_row_number, row in enumerate(rows[1:], start=2):
            if not is_payment_due(row, now):
                continue

            try:
                await send_due_payment_invoice(context.bot, sheet_row_number, row, now)
            except Exception:
                logging.exception(
                    "Failed to dispatch request %s",
                    get_cell(row, REQUEST_ID_COL)
                )
    finally:
        context.application.bot_data["payment_dispatch_running"] = False

async def resend_pending_approval_invoice(bot, sheet_row_number, row, now):
    if not is_approval_reminder_due(row, now):
        return None

    request_id = get_cell(row, REQUEST_ID_COL)
    if request_id in approval_resend_claims:
        return None

    approval_chat_id = parse_int(get_cell(row, APPROVER_CHAT_ID_COL))
    if not approval_chat_id:
        logging.error("Could not resend request %s: approval chat is missing", request_id)
        return None

    previous_chat_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_CHAT_ID_COL))
    previous_message_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_ID_COL))
    approval_resend_claims.add(request_id)
    try:
        sent_message = await send_pending_approval_invoice(bot, approval_chat_id, row)
        save_last_invoice_message(sheet_row_number, sent_message)
        sheet.update_cell(
            sheet_row_number,
            APPROVAL_LAST_SENT_AT_COL + 1,
            format_approval_reminder_timestamp(now)
        )
        set_cell(row, APPROVER_CHAT_ID_COL, str(sent_message.chat_id))
        set_cell(row, LAST_INVOICE_MESSAGE_CHAT_ID_COL, str(sent_message.chat_id))
        set_cell(row, LAST_INVOICE_MESSAGE_ID_COL, str(sent_message.message_id))
        set_cell(
            row,
            APPROVAL_LAST_SENT_AT_COL,
            format_approval_reminder_timestamp(now)
        )

        if previous_chat_id and previous_message_id:
            try:
                await bot.delete_message(
                    chat_id=previous_chat_id,
                    message_id=previous_message_id
                )
            except Exception:
                logging.info(
                    "Could not delete previous approval message for request %s",
                    request_id
                )

        logging.info(
            "Resent pending approval request %s to chat %s",
            request_id,
            sent_message.chat_id
        )
        return sent_message
    finally:
        approval_resend_claims.discard(request_id)

async def send_scheduled_approval_reminders(context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get("approval_reminder_running"):
        return

    now = datetime.now(APPROVAL_REMINDER_TZ)
    if (now.hour, now.minute) < (
        APPROVAL_REMINDER_HOUR,
        APPROVAL_REMINDER_MINUTE,
    ):
        return

    context.application.bot_data["approval_reminder_running"] = True
    try:
        rows = sheet.get_all_values()
        for sheet_row_number, row in enumerate(rows[1:], start=2):
            if not is_approval_reminder_due(row, now):
                continue

            try:
                await resend_pending_approval_invoice(
                    context.bot,
                    sheet_row_number,
                    row,
                    now
                )
            except Exception:
                logging.exception(
                    "Failed to resend pending approval request %s",
                    get_cell(row, REQUEST_ID_COL)
                )
    finally:
        context.application.bot_data["approval_reminder_running"] = False

async def handle_payment_received_confirmation(query, context, answer, request_id):
    rows = sheet.get_all_values()

    for i, row in enumerate(rows):
        if get_cell(row, REQUEST_ID_COL) != request_id:
            continue

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logging.exception("Failed to remove payment confirmation keyboard for request %s", request_id)

        if answer == "yes":
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="Напиши /new чтобы отправить счет"
            )
            return

        payment_chat_id = get_cell(row, PAYMENT_CHAT_ID_COL)
        payer_tag = get_cell(row, PAYMENT_PAYER_TAG_COL, "Оплатчик")
        receipt_file_id = get_cell(row, PAYMENT_RECEIPT_FILE_ID_COL)
        receipt_file_type = get_cell(row, PAYMENT_RECEIPT_FILE_TYPE_COL)

        if not payment_chat_id or not receipt_file_id:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"Не удалось вернуть чек по счету #{request_id}: не найдены данные оплаты."
            )
            return

        sheet.update_cell(i+1, STATUS_COL + 1, STATUS_APPROVED)

        caption = (
            f"{payer_tag}\n"
            f"Счет #{request_id}\n\n"
            "Оплата по данному чеку не получена"
        )

        if receipt_file_type == "photo":
            sent_message = await context.bot.send_photo(
                chat_id=int(payment_chat_id),
                photo=receipt_file_id,
                caption=caption,
                reply_markup=build_paid_keyboard(request_id)
            )
        else:
            sent_message = await context.bot.send_document(
                chat_id=int(payment_chat_id),
                document=receipt_file_id,
                caption=caption,
                reply_markup=build_paid_keyboard(request_id)
            )

        save_payment_message(i + 1, sent_message)
        return
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Не удалось найти счет #{request_id}."
    )

def get_project_settings(project_name):
    rows = projects_sheet.get_all_values()

    for row in rows[1:]:
        if get_cell(row, 0).lower() == project_name.strip().lower():
            return {
                "payment_chat_id": parse_int(get_cell(row, 1)),
                "payer_tag": get_cell(row, 2),
                "approval_chat_id": parse_int(get_cell(row, 3)),
                "approver_tag": get_cell(row, 4),
            }

    return None


def replace_migrated_project_chat_id(old_chat_id, new_chat_id):
    old_chat_id = parse_int(old_chat_id)
    new_chat_id = parse_int(new_chat_id)
    if not old_chat_id or not new_chat_id or old_chat_id == new_chat_id:
        return

    rows = projects_sheet.get_all_values()
    updated_cells = []
    for sheet_row_number, row in enumerate(rows[1:], start=2):
        if parse_int(get_cell(row, 1)) == old_chat_id:
            projects_sheet.update_cell(sheet_row_number, 2, str(new_chat_id))
            updated_cells.append(f"B{sheet_row_number}")
        if parse_int(get_cell(row, 3)) == old_chat_id:
            projects_sheet.update_cell(sheet_row_number, 4, str(new_chat_id))
            updated_cells.append(f"D{sheet_row_number}")

    logging.warning(
        "Telegram chat migrated from %s to %s; updated projects cells: %s",
        old_chat_id,
        new_chat_id,
        ", ".join(updated_cells) or "none"
    )

def verify_telegram_init_data(init_data):
    if not TOKEN or not init_data:
        return False

    pairs = parse_qsl(init_data, keep_blank_values=True)
    received_hash = None
    data_pairs = []

    for key, value in pairs:
        if key == "hash":
            received_hash = value
        else:
            data_pairs.append((key, value))

    if not received_hash:
        return False

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(data_pairs)
    )
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return False

    data = dict(pairs)
    auth_date = parse_int(data.get("auth_date"))
    if auth_date and MINIAPP_INIT_DATA_MAX_AGE_SECONDS > 0:
        age = int(datetime.now(timezone.utc).timestamp()) - auth_date
        if age < -60 or age > MINIAPP_INIT_DATA_MAX_AGE_SECONDS:
            return False

    return True

def get_miniapp_user(init_data):
    if MINIAPP_REQUIRE_INIT_DATA and not verify_telegram_init_data(init_data):
        raise ValueError("Не удалось проверить Telegram Mini App.")

    data = dict(parse_qsl(init_data or "", keep_blank_values=True))
    user_data = data.get("user")

    if user_data:
        return json.loads(user_data)

    if MINIAPP_REQUIRE_INIT_DATA:
        raise ValueError("Telegram не передал данные пользователя.")

    return {
        "id": os.getenv("MINIAPP_DEBUG_USER_ID", ""),
        "username": "debug",
        "first_name": "Debug"
    }

def form_value(form, name):
    if name not in form:
        return ""

    field = form[name]
    if isinstance(field, list):
        field = field[0]

    value = field.value
    return value.strip() if isinstance(value, str) else value

def get_uploaded_file(form):
    if "file" not in form:
        return None

    field = form["file"]
    if isinstance(field, list):
        field = field[0]

    if not getattr(field, "filename", ""):
        return None

    content = field.file.read()
    if not content:
        return None

    return {
        "filename": field.filename,
        "content_type": field.type or "application/octet-stream",
        "content": content
    }

def telegram_api_request(method, data, files=None):
    request_data = dict(data)
    original_chat_id = parse_int(request_data.get("chat_id"))

    for attempt in range(2):
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/{method}",
            data=request_data,
            files=files,
            timeout=30
        )
        payload = response.json()

        if response.ok and payload.get("ok"):
            return payload["result"]

        parameters = payload.get("parameters") or {}
        migrated_chat_id = parse_int(parameters.get("migrate_to_chat_id"))
        if migrated_chat_id and attempt == 0 and original_chat_id:
            replace_migrated_project_chat_id(original_chat_id, migrated_chat_id)
            request_data["chat_id"] = str(migrated_chat_id)
            continue

        description = payload.get("description", response.text)
        raise RuntimeError(
            f"Telegram API error: {description}; parameters={parameters}"
        )

    raise RuntimeError("Telegram API error: request retry exhausted")

def approval_reply_markup(request_id):
    return json.dumps({
        "inline_keyboard": [[
            {"text": "✅ Одобрить", "callback_data": f"approve_{request_id}"},
            {"text": "❌ Отклонить", "callback_data": f"reject_{request_id}"}
        ]]
    }, ensure_ascii=False)

def send_approval_request_via_api(chat_id, row, uploaded_file):
    text = build_pending_approval_invoice_text(row)
    data = {
        "chat_id": str(chat_id),
        "reply_markup": approval_reply_markup(get_cell(row, REQUEST_ID_COL))
    }

    if not uploaded_file:
        data["text"] = text
        result = telegram_api_request("sendMessage", data)
        return "", result["message_id"], result["chat"]["id"]

    is_photo = uploaded_file["content_type"].startswith("image/")
    method = "sendPhoto" if is_photo else "sendDocument"
    file_field = "photo" if is_photo else "document"
    data["caption"] = text

    result = telegram_api_request(
        method,
        data,
        files={
            file_field: (
                uploaded_file["filename"],
                uploaded_file["content"],
                uploaded_file["content_type"]
            )
        }
    )
    actual_chat_id = result["chat"]["id"]

    if is_photo:
        return result["photo"][-1]["file_id"], result["message_id"], actual_chat_id

    return result["document"]["file_id"], result["message_id"], actual_chat_id


def create_request_from_miniapp(form):
    init_data = form_value(form, "initData")
    user = get_miniapp_user(init_data)

    project = form_value(form, "project")
    expense_category = form_value(form, "expense_category")
    target = form_value(form, "target")
    amount = form_value(form, "amount")
    comment = form_value(form, "comment")
    uploaded_file = get_uploaded_file(form)

    if not project:
        raise ValueError("Укажите проект.")
    if expense_category not in EXPENSE_CATEGORY_LABELS:
        raise ValueError("Выберите статью расхода.")
    if not target:
        raise ValueError("Укажите, кому платим.")
    if not amount:
        raise ValueError("Укажите сумму.")
    if expense_category == TAXI_EXPENSE_CATEGORY:
        parse_taxi_amount(amount)
    if not comment:
        raise ValueError("Введите комментарий.")

    if expense_category == TAXI_EXPENSE_CATEGORY:
        payment_due_date = None
    else:
        payment_due_date = parse_payment_date(
            form_value(form, "payment_due_date"),
            datetime.now(REMINDER_TZ).date()
        )
    project_settings = get_project_settings(project)
    if not project_settings:
        raise ValueError("Проект не найден в настройках.")
    if not project_settings["approval_chat_id"]:
        raise ValueError("Для проекта не заполнен approval_chat_id.")
    if expense_category != TAXI_EXPENSE_CATEGORY and not project_settings["payment_chat_id"]:
        raise ValueError("Для проекта не заполнен payment_chat_id.")

    creator_chat_id = str(user.get("id", "")).strip()
    if not creator_chat_id:
        raise ValueError("Telegram не передал ID пользователя.")

    rows = sheet.get_all_values()
    request_id = str(len(rows))
    sheet_row_number = len(rows) + 1
    creator_name = user.get("username") or user.get("first_name") or "unknown"

    row = [
        request_id,
        datetime.now(REMINDER_TZ).isoformat(),
        user.get("username", ""),
        project,
        target,
        amount,
        comment,
        STATUS_PENDING_APPROVAL,
        project_settings["approval_chat_id"],
        "",
        creator_chat_id,
        creator_name,
        "",
        project_settings["payer_tag"],
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        expense_category,
        payment_due_date.isoformat() if payment_due_date else "",
        "",
        "",
        format_approval_reminder_timestamp(datetime.now(APPROVAL_REMINDER_TZ))
    ]

    sheet.append_row(row)
    file_id, sent_message_id, actual_chat_id = send_approval_request_via_api(
        project_settings["approval_chat_id"],
        row,
        uploaded_file
    )

    if file_id:
        sheet.update_cell(sheet_row_number, FILE_ID_COL + 1, file_id)
    if actual_chat_id != project_settings["approval_chat_id"]:
        sheet.update_cell(sheet_row_number, APPROVER_CHAT_ID_COL + 1, str(actual_chat_id))
    if sent_message_id:
        save_last_invoice_message_ids(
            sheet_row_number,
            actual_chat_id,
            sent_message_id
        )

    return request_id


def miniapp_username(user):
    username = str(user.get("username", "")).strip()
    if not username:
        raise ValueError(
            "Для персональных списков нужен Telegram username. Добавьте username в настройках Telegram."
        )
    return username


def miniapp_request_lock(request_id):
    key = str(request_id)
    with miniapp_request_locks_guard:
        lock = miniapp_request_locks.get(key)
        if lock is None:
            lock = Lock()
            miniapp_request_locks[key] = lock
        return lock


def find_request_row(request_id):
    for sheet_row_number, row in enumerate(sheet.get_all_values()[1:], start=2):
        if get_cell(row, REQUEST_ID_COL) == str(request_id):
            return sheet_row_number, row
    return None, None


def empty_reply_markup():
    return json.dumps({"inline_keyboard": []}, ensure_ascii=False)


def payment_reply_markup(request_id):
    return json.dumps({
        "inline_keyboard": [
            [{"text": "💰 Оплатил – прикрепить чек", "callback_data": f"paid_{request_id}"}],
            [{"text": "❌ Отменить счет", "callback_data": f"cancel_{request_id}"}],
        ]
    }, ensure_ascii=False)


def payment_received_reply_markup(request_id):
    return json.dumps({
        "inline_keyboard": [[
            {"text": "✅ Да", "callback_data": f"received_yes_{request_id}"},
            {"text": "❌ Нет", "callback_data": f"received_no_{request_id}"},
        ]]
    }, ensure_ascii=False)


def edit_invoice_message_via_api(chat_id, message_id, text):
    data = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "reply_markup": empty_reply_markup(),
    }
    try:
        telegram_api_request("editMessageCaption", {**data, "caption": text})
        return
    except Exception:
        telegram_api_request("editMessageText", {**data, "text": text})


def send_payment_invoice_via_api(sheet_row_number, row, now=None):
    now = now or datetime.now(REMINDER_TZ)
    if not is_payment_due(row, now):
        return None

    project_settings = get_project_settings(get_cell(row, 3))
    chat_id = project_settings.get("payment_chat_id") if project_settings else None
    if not chat_id:
        raise ValueError("Для проекта не заполнен payment_chat_id.")

    request_id = get_cell(row, REQUEST_ID_COL)
    file_id = get_cell(row, FILE_ID_COL)
    data = {
        "chat_id": str(chat_id),
        "reply_markup": payment_reply_markup(request_id),
    }
    text = build_payment_invoice_text(row)
    if file_id:
        if is_photo_file(file_id):
            result = telegram_api_request("sendPhoto", {**data, "photo": file_id, "caption": text})
        else:
            result = telegram_api_request(
                "sendDocument", {**data, "document": file_id, "caption": text}
            )
    else:
        result = telegram_api_request("sendMessage", {**data, "text": text})

    actual_chat_id = result["chat"]["id"]
    message_id = result["message_id"]
    sheet.batch_update([
        {"range": f"P{sheet_row_number}", "values": [[str(actual_chat_id)]]},
        {"range": f"Y{sheet_row_number}:Z{sheet_row_number}", "values": [[
            now.isoformat(),
            str(message_id),
        ]]},
    ])
    set_cell(row, PAYMENT_CHAT_ID_COL, str(actual_chat_id))
    set_cell(row, PAYMENT_SENT_AT_COL, now.isoformat())
    set_cell(row, PAYMENT_MESSAGE_ID_COL, str(message_id))
    return result


def notify_creator_approved_via_api(row):
    creator_chat_id = parse_int(get_cell(row, CREATOR_CHAT_ID_COL))
    if not creator_chat_id:
        return
    if is_taxi_invoice(row):
        text = (
            "✅ Ваш счет согласован:\n\n"
            f"{get_cell(row, 4)}\n\n"
            f"{get_cell(row, 6)}"
        )
    else:
        text = (
            "✅ Ваш счет согласован:\n\n"
            f"{get_cell(row, 4)}\n\n"
            f"Дата оплаты: {get_payment_date_text(row)}"
        )
    telegram_api_request("sendMessage", {"chat_id": str(creator_chat_id), "text": text})


def approve_request_from_miniapp(request_id, user):
    username = miniapp_username(user)
    with miniapp_request_lock(request_id):
        sheet_row_number, row = find_request_row(request_id)
        project_rows = projects_sheet.get_all_values()
        if row is None:
            raise ValueError("Счет не найден.")
        if not user_can_manage(row, project_rows, username, "approval"):
            raise ValueError("Счет уже обработан или назначен другому пользователю.")

        now = datetime.now(REMINDER_TZ)
        approver_name = username
        sheet.batch_update([
            {"range": f"H{sheet_row_number}", "values": [[STATUS_APPROVED]]},
            {"range": f"M{sheet_row_number}", "values": [[approver_name]]},
            {"range": f"O{sheet_row_number}", "values": [[now.isoformat()]]},
        ])
        set_cell(row, STATUS_COL, STATUS_APPROVED)
        set_cell(row, APPROVER_NAME_COL, approver_name)
        set_cell(row, APPROVED_AT_COL, now.isoformat())

    approval_chat_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_CHAT_ID_COL))
    approval_message_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_ID_COL))
    if approval_chat_id and approval_message_id:
        try:
            edit_invoice_message_via_api(
                approval_chat_id,
                approval_message_id,
                build_approved_approval_text(row),
            )
        except Exception:
            logging.exception("Could not edit Mini App approved request %s", request_id)

    try:
        notify_creator_approved_via_api(row)
    except Exception:
        logging.exception("Could not notify creator about Mini App approval %s", request_id)

    if not is_taxi_invoice(row):
        try:
            send_payment_invoice_via_api(sheet_row_number, row, now)
        except Exception:
            logging.exception("Could not dispatch Mini App approved request %s", request_id)
    return row


def reject_request_from_miniapp(request_id, user, reason):
    username = miniapp_username(user)
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("Укажите причину отклонения.")

    with miniapp_request_lock(request_id):
        sheet_row_number, row = find_request_row(request_id)
        project_rows = projects_sheet.get_all_values()
        if row is None:
            raise ValueError("Счет не найден.")
        if not user_can_manage(row, project_rows, username, "approval"):
            raise ValueError("Счет уже обработан или назначен другому пользователю.")
        sheet.update_cell(sheet_row_number, STATUS_COL + 1, STATUS_REJECTED)
        set_cell(row, STATUS_COL, STATUS_REJECTED)

    approval_chat_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_CHAT_ID_COL))
    approval_message_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_ID_COL))
    if approval_chat_id and approval_message_id:
        try:
            edit_invoice_message_via_api(
                approval_chat_id,
                approval_message_id,
                build_closed_invoice_text(row, STATUS_REJECTED, reason),
            )
            telegram_api_request("sendMessage", {
                "chat_id": str(approval_chat_id),
                "text": f"❌ Счет #{request_id} отклонен, комментарий отправлен",
            })
        except Exception:
            logging.exception("Could not edit Mini App rejected request %s", request_id)

    creator_chat_id = parse_int(get_cell(row, CREATOR_CHAT_ID_COL))
    if creator_chat_id:
        try:
            telegram_api_request("sendMessage", {
                "chat_id": str(creator_chat_id),
                "text": (
                    f"❌ Ваш счет #{request_id} не согласован\n\n"
                    f"Причина: {reason}\n\n"
                    "Просьба отправить счет заново с учетом комментария"
                ),
            })
        except Exception:
            logging.exception("Could not notify creator about Mini App rejection %s", request_id)
    return row


def uploaded_telegram_file_id(message, file_type):
    if file_type == "photo":
        photos = message.get("photo") or []
        return photos[-1]["file_id"] if photos else ""
    document = message.get("document") or {}
    return document.get("file_id", "")


def send_uploaded_receipt_via_api(chat_id, request_id, message_id, uploaded_file):
    is_photo = uploaded_file["content_type"].startswith("image/")
    file_type = "photo" if is_photo else "document"
    method = "sendPhoto" if is_photo else "sendDocument"
    file_field = "photo" if is_photo else "document"
    data = {
        "chat_id": str(chat_id),
        "caption": f"Чек по счету #{request_id}",
        "reply_to_message_id": str(message_id),
    }
    files = {
        file_field: (
            uploaded_file["filename"],
            uploaded_file["content"],
            uploaded_file["content_type"],
        )
    }
    try:
        result = telegram_api_request(method, data, files=files)
    except Exception:
        data.pop("reply_to_message_id", None)
        result = telegram_api_request(method, data, files=files)
    file_id = uploaded_telegram_file_id(result, file_type)
    if not file_id:
        raise RuntimeError("Telegram did not return receipt file_id")
    return result, file_id, file_type


def record_paid_invoice_to_dds_background(
    row,
    request_id,
    payment_chat_id,
    receipt_message_id,
    user,
):
    if not dds_writer:
        return
    event_time = datetime.now(REMINDER_TZ)
    if not event_is_in_scope(payment_chat_id, event_time, DDS_ENABLED, DDS_START_AT):
        return
    try:
        candidate = build_bot_invoice_candidate(
            request_id,
            get_cell(row, 5),
            get_cell(row, 4),
            get_cell(row, 6),
            default_currency=DDS_DEFAULT_CURRENCY_BY_CHAT.get(payment_chat_id),
        )
    except Exception:
        logging.exception("Could not build DDS candidate for Mini App payment %s", request_id)
        return

    for attempt in range(len(DDS_RETRY_DELAYS) + 1):
        try:
            result = dds_writer.record_candidate(
                f"invoice:{request_id}",
                event_time,
                candidate,
                payment_chat_id,
                receipt_message_id,
                user.get("id", ""),
                user.get("username") or user.get("first_name", ""),
                request_id,
            )
            logging.info(
                "DDS Mini App invoice %s finished with status %s, row %s",
                request_id,
                result["status"],
                result["dds_row"],
            )
            return
        except Exception as exc:
            if attempt >= len(DDS_RETRY_DELAYS) or not is_retryable_dds_error(exc):
                logging.exception("DDS Mini App invoice %s failed", request_id)
                return
            time.sleep(DDS_RETRY_DELAYS[attempt])


def pay_request_from_miniapp(request_id, user, uploaded_file):
    username = miniapp_username(user)
    if not uploaded_file:
        raise ValueError("Прикрепите чек или подтверждение оплаты.")

    with miniapp_request_lock(request_id):
        sheet_row_number, row = find_request_row(request_id)
        project_rows = projects_sheet.get_all_values()
        if row is None:
            raise ValueError("Счет не найден.")
        if not user_can_manage(row, project_rows, username, "payment"):
            raise ValueError("Счет уже обработан или назначен другому пользователю.")

        payment_chat_id = parse_int(get_cell(row, PAYMENT_CHAT_ID_COL))
        payment_message_id = parse_int(get_cell(row, PAYMENT_MESSAGE_ID_COL))
        if not payment_chat_id or not payment_message_id:
            raise ValueError("Сообщение счета в чате оплаты не найдено.")

        receipt_message, file_id, file_type = send_uploaded_receipt_via_api(
            payment_chat_id,
            request_id,
            payment_message_id,
            uploaded_file,
        )
        payer_tag = f"@{username}"
        try:
            save_paid_receipt(
                sheet_row_number,
                row,
                payment_chat_id,
                payer_tag,
                file_id,
                file_type,
            )
        except Exception:
            try:
                telegram_api_request("deleteMessage", {
                    "chat_id": str(payment_chat_id),
                    "message_id": str(receipt_message["message_id"]),
                })
            except Exception:
                logging.exception(
                    "Could not remove unsaved Mini App receipt for request %s",
                    request_id,
                )
            raise

    try:
        edit_invoice_message_via_api(
            payment_chat_id,
            payment_message_id,
            build_paid_invoice_text(row, payer_tag),
        )
    except Exception:
        logging.exception("Could not edit Mini App paid request %s", request_id)

    creator_chat_id = parse_int(get_cell(row, CREATOR_CHAT_ID_COL))
    if creator_chat_id:
        caption = (
            f"💰 Счет #{request_id} по проекту {get_cell(row, 3, 'неизвестно')} оплачен\n\n"
            f"Сумма: {get_cell(row, 5, 'не указана')}\n\n"
            "Оплата получена?"
        )
        try:
            method = "sendPhoto" if file_type == "photo" else "sendDocument"
            field = "photo" if file_type == "photo" else "document"
            telegram_api_request(method, {
                "chat_id": str(creator_chat_id),
                field: file_id,
                "caption": caption,
                "reply_markup": payment_received_reply_markup(request_id),
            })
        except Exception:
            logging.exception("Could not send Mini App receipt to creator for %s", request_id)

    Thread(
        target=record_paid_invoice_to_dds_background,
        args=(
            list(row),
            request_id,
            payment_chat_id,
            receipt_message["message_id"],
            dict(user),
        ),
        daemon=True,
    ).start()
    return row

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    text = "Привет! Напиши /new чтобы отправить счет"
    reply_markup = None

    if WEBAPP_URL:
        text += "\n\nИли открой форму через мини-приложение:"
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Открыть форму", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])

    await update.message.reply_text(text, reply_markup=reply_markup)

async def new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user_state[update.effective_chat.id] = {}
    await update.message.reply_text("Напишите аббревиатуру проекта:")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Receipt uploaded after the payer clicked "Paid".
    if user_id in payment_state:
        dds_linked_receipt_events.add(
            event_key(chat_id, update.message.message_id)
        )
        data = payment_state.pop(user_id)
        request_id = data["request_id"]
        message_id = data["message_id"]
        original_chat_id = data.get("chat_id", chat_id)
        ask_message_id = data.get("ask_message_id")

        if update.message.document:
            file_id = update.message.document.file_id
            receipt_file_type = "document"
        elif update.message.photo:
            file_id = update.message.photo[-1].file_id
            receipt_file_type = "photo"
        else:
            return

        rows = sheet.get_all_values()
        for i, row in enumerate(rows):
            if get_cell(row, REQUEST_ID_COL) != request_id:
                continue

            if (
                get_cell(row, STATUS_COL) != STATUS_APPROVED
                or not callback_matches_message(
                    row,
                    original_chat_id,
                    message_id,
                    "payment"
                )
            ):
                await update.message.reply_text(
                    "Этот счет уже изменен. Откройте его актуальное сообщение."
                )
                return

            payer_tag = get_user_tag(update.effective_user)
            try:
                save_paid_receipt(
                    i + 1,
                    row,
                    original_chat_id,
                    payer_tag,
                    file_id,
                    receipt_file_type
                )
            except Exception:
                logging.exception("Could not save receipt for request %s", request_id)
                await update.message.reply_text(
                    "Не удалось сохранить чек. Счет остался доступен — "
                    "нажмите «Оплатил» и попробуйте еще раз."
                )
                return

            try:
                await edit_invoice_message(
                    context.bot,
                    original_chat_id,
                    message_id,
                    row,
                    build_paid_invoice_text(row, payer_tag)
                )
                await send_receipt_to_payment_chat(
                    context.bot,
                    original_chat_id,
                    file_id,
                    receipt_file_type,
                    request_id,
                    message_id
                )
            except Exception:
                logging.exception("Could not finish receipt processing for request %s", request_id)
                restored = False
                try:
                    await restore_approved_payment_message(
                        context.bot,
                        i + 1,
                        row,
                        original_chat_id,
                        message_id
                    )
                    restored = True
                except Exception:
                    logging.exception("Could not restore request %s after receipt failure", request_id)

                if restored:
                    error_text = (
                        "Не удалось завершить обработку чека. Статус счета восстановлен; "
                        "нажмите «Оплатил» и попробуйте еще раз."
                    )
                else:
                    error_text = (
                        "Не удалось завершить обработку чека и восстановить статус счета. "
                        "Сообщение с чеком сохранено; сообщите администратору номер счета."
                    )
                await update.message.reply_text(error_text)
                return

            try:
                await update.message.delete()
            except Exception:
                logging.info("Could not delete payer receipt message for request %s", request_id)

            creator_chat_id = parse_int(get_cell(row, CREATOR_CHAT_ID_COL))
            if creator_chat_id:
                project_name = get_cell(row, 3, "неизвестно")
                amount = get_cell(row, 5, "не указана")
                creator_receipt_caption = (
                    f"💰 Счет #{request_id} по проекту {project_name} оплачен\n\n"
                    f"Сумма: {amount}\n\n"
                    "Оплата получена?"
                )
                try:
                    if receipt_file_type == "photo":
                        await context.bot.send_photo(
                            chat_id=creator_chat_id,
                            photo=file_id,
                            caption=creator_receipt_caption,
                            reply_markup=build_payment_received_keyboard(request_id)
                        )
                    else:
                        await context.bot.send_document(
                            chat_id=creator_chat_id,
                            document=file_id,
                            caption=creator_receipt_caption,
                            reply_markup=build_payment_received_keyboard(request_id)
                        )
                except Exception:
                    logging.exception(
                        "Could not send receipt confirmation to creator for request %s",
                        request_id
                    )

            try:
                if ask_message_id:
                    await context.bot.delete_message(
                        chat_id=original_chat_id,
                        message_id=ask_message_id
                    )
            except Exception:
                logging.info("Could not delete receipt prompt for request %s", request_id)

            context.application.create_task(
                write_paid_invoice_to_dds(
                    update,
                    row,
                    request_id,
                    original_chat_id,
                ),
                update=update,
            )
            return

        await update.message.reply_text(f"Не удалось найти счет #{request_id}.")
        return

    if chat_id not in user_state:
        return

    state = user_state[chat_id]
    if "payment_due_date" not in state or "file_step_done" in state:
        return

    if update.message.document:
        file_id = update.message.document.file_id
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
    else:
        return

    state["file_id"] = file_id
    state["file_step_done"] = True
    await update.message.reply_text(
        build_comment_prompt(state.get("expense_category"))
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in reject_state and update.effective_chat.type != "private":
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if user_id in reject_state:
        data = reject_state.pop(user_id)
        request_id = data["request_id"]
        message_id = data["message_id"]
        action_chat_id = data["chat_id"]
        ask_message_id = data.get("ask_message_id")
        result_status = data.get("result_status", STATUS_REJECTED)
        action_text = data.get("action_text", "отклонен")
        creator_message_title = data.get("creator_message_title", "не согласован")
        expected_status = data.get("expected_status", STATUS_PENDING_APPROVAL)
        stage = data.get("stage", "approval")

        rows = sheet.get_all_values()
        matching_row = None
        matching_index = None
        for i, row in enumerate(rows):
            if get_cell(row, REQUEST_ID_COL) == request_id:
                matching_row = row
                matching_index = i
                break

        if matching_row is None:
            await update.message.reply_text(f"Не удалось найти счет #{request_id}.")
            return

        if (
            get_cell(matching_row, STATUS_COL) != expected_status
            or not callback_matches_message(
                matching_row,
                action_chat_id,
                message_id,
                stage
            )
        ):
            await update.message.reply_text(
                "Этот счет уже изменен. Комментарий не применен."
            )
            return

        sheet.update_cell(matching_index + 1, STATUS_COL + 1, result_status)
        set_cell(matching_row, STATUS_COL, result_status)

        try:
            await edit_invoice_message(
                context.bot,
                action_chat_id,
                message_id,
                matching_row,
                build_closed_invoice_text(matching_row, result_status, text)
            )
        except Exception:
            logging.exception("Could not close Telegram message for request %s", request_id)
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=action_chat_id,
                    message_id=message_id,
                    reply_markup=None
                )
            except Exception:
                logging.exception("Could not remove keyboard for request %s", request_id)

        for removable_chat_id, removable_message_id in (
            (action_chat_id, ask_message_id),
            (update.effective_chat.id, update.message.message_id),
        ):
            if not removable_message_id:
                continue
            try:
                await context.bot.delete_message(
                    chat_id=removable_chat_id,
                    message_id=removable_message_id
                )
            except Exception:
                logging.info("Could not delete rejection helper message for request %s", request_id)

        creator_chat_id = parse_int(get_cell(matching_row, CREATOR_CHAT_ID_COL))
        if creator_chat_id:
            try:
                await context.bot.send_message(
                    chat_id=creator_chat_id,
                    text=(
                        f"❌ Ваш счет #{request_id} {creator_message_title}\n\n"
                        f"Причина: {text}\n\n"
                        "Просьба отправить счет заново с учетом комментария"
                    )
                )
            except Exception:
                logging.exception("Could not notify creator about closed request %s", request_id)

        await context.bot.send_message(
            chat_id=action_chat_id,
            text=f"❌ Счет #{request_id} {action_text}, комментарий отправлен"
        )
        return

    if chat_id not in user_state:
        await update.message.reply_text("Напиши /new чтобы отправить счет")
        return

    state = user_state[chat_id]

    if "project" not in state:
        project_settings = get_project_settings(text)
        if not project_settings:
            await update.message.reply_text(
                "❌ Проект не найден в настройках.\n"
                "Пожалуйста, введите аббревиатуру проекта снова:"
            )
            return
        if not project_settings["approval_chat_id"]:
            await update.message.reply_text(
                "❌ Для проекта не заполнен approval_chat_id.\n"
                "Обратитесь к администратору бота."
            )
            return

        state["project"] = text
        state["approval_chat_id"] = project_settings["approval_chat_id"]
        state["payment_chat_id"] = project_settings["payment_chat_id"]
        state["payer_tag"] = project_settings["payer_tag"]
        await update.message.reply_text(
            "К какой статье расхода относится ваш счёт?",
            reply_markup=build_expense_category_keyboard()
        )
        return

    if "expense_category" not in state:
        await update.message.reply_text("Пожалуйста, выберите статью расхода кнопкой.")
        return

    if "target" not in state:
        state["target"] = text
        await update.message.reply_text("Введите сумму:")
        return

    if "amount" not in state:
        if state.get("expense_category") == TAXI_EXPENSE_CATEGORY:
            try:
                parse_taxi_amount(text)
            except ValueError as exc:
                await update.message.reply_text(str(exc))
                return
        state["amount"] = text
        if state.get("expense_category") == TAXI_EXPENSE_CATEGORY:
            state["payment_due_date"] = ""
            keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_file")]]
            await update.message.reply_text(
                "📎 Прикрепите файл (счет, чек и т.д.)\n"
                "Или нажмите «Пропустить»",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        await update.message.reply_text(
            "Введите дату оплаты:\n"
            "Например: 15, 15.07 или 15.07.2026"
        )
        return

    if "payment_due_date" not in state:
        try:
            payment_due_date = parse_payment_date(
                text,
                datetime.now(REMINDER_TZ).date()
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return

        state["payment_due_date"] = payment_due_date.isoformat()
        keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_file")]]
        await update.message.reply_text(
            f"Дата оплаты: {format_payment_date(payment_due_date)}\n\n"
            "📎 Прикрепите файл (счет, чек и т.д.)\n"
            "Или нажмите «Пропустить»",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if "file_step_done" not in state:
        return

    if "comment" not in state:
        state["comment"] = text

    rows = sheet.get_all_values()
    row = [
        str(len(rows)),
        datetime.now(REMINDER_TZ).isoformat(),
        update.effective_user.username or "",
        state["project"],
        state["target"],
        state["amount"],
        state["comment"],
        STATUS_PENDING_APPROVAL,
        str(state["approval_chat_id"]),
        state.get("file_id", ""),
        str(update.effective_user.id),
        update.effective_user.username or update.effective_user.first_name,
        "",
        state.get("payer_tag", ""),
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        state.get("expense_category", ""),
        state["payment_due_date"],
        "",
        "",
        format_approval_reminder_timestamp(datetime.now(APPROVAL_REMINDER_TZ))
    ]

    sheet.append_row(row)
    sheet_row_number = len(rows) + 1
    sent_message = await send_pending_approval_invoice(
        context.bot,
        state["approval_chat_id"],
        row
    )
    sheet.update_cell(
        sheet_row_number,
        APPROVER_CHAT_ID_COL + 1,
        str(sent_message.chat_id)
    )
    save_last_invoice_message(sheet_row_number, sent_message)

    confirmation_text = "Счёт принят и отправлен на согласование.\n\n"
    if not is_taxi_invoice(row):
        confirmation_text += f"Дата оплаты: {get_payment_date_text(row)}\n\n"
    confirmation_text += "Напиши /new чтобы отправить новый счёт"
    await update.message.reply_text(confirmation_text)
    user_state.pop(chat_id, None)

async def answer_callback_safely(query, *args, **kwargs):
    try:
        await query.answer(*args, **kwargs)
        return True
    except BadRequest as exc:
        error_text = str(exc).lower()
        if "query is too old" in error_text or "query id is invalid" in error_text:
            logging.info("Ignored expired Telegram callback query %s", query.id)
            return False
        raise


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "skip_file":
        chat_id = query.message.chat_id
        state = user_state.get(chat_id)
        if not state or "payment_due_date" not in state:
            await answer_callback_safely(query, "Форма уже неактуальна.", show_alert=True)
            return

        state["file_step_done"] = True
        await answer_callback_safely(query)
        await query.message.reply_text(
            build_comment_prompt(state.get("expense_category"))
        )
        return

    if data.startswith("expense_"):
        category_key = data.split("_", 1)[1]
        category = EXPENSE_CATEGORY_BY_KEY.get(category_key)
        chat_id = query.message.chat_id
        state = user_state.get(chat_id)
        if not category or not state or "project" not in state:
            await answer_callback_safely(query, "Не удалось выбрать статью расхода", show_alert=True)
            return

        if category != TAXI_EXPENSE_CATEGORY and not state.get("payment_chat_id"):
            await answer_callback_safely(query, "Для проекта не заполнен payment_chat_id", show_alert=True)
            await query.message.reply_text(
                "❌ Для проекта не заполнен payment_chat_id.\n"
                "Обратитесь к администратору бота."
            )
            user_state.pop(chat_id, None)
            return

        state["expense_category"] = category
        await answer_callback_safely(query)
        try:
            await query.message.edit_text(f"Статья расхода: {category}")
        except Exception:
            logging.info("Could not edit expense category prompt")
        await query.message.reply_text(
            "Кому платим? (Имя Фамилия, компания, сервис)"
        )
        return

    if data.startswith("received_"):
        parts = data.split("_", 2)
        if len(parts) != 3:
            await answer_callback_safely(query, "Некорректная команда.", show_alert=True)
            return
        _, answer, request_id = parts
        await answer_callback_safely(query)
        await handle_payment_received_confirmation(query, context, answer, request_id)
        return

    try:
        action, request_id = data.split("_", 1)
    except ValueError:
        await answer_callback_safely(query, "Некорректная команда.", show_alert=True)
        return

    await answer_callback_safely(query)

    rows = sheet.get_all_values()
    row = None
    sheet_row_number = None
    for i, candidate in enumerate(rows):
        if get_cell(candidate, REQUEST_ID_COL) == request_id:
            row = candidate
            sheet_row_number = i + 1
            break

    if row is None:
        await query.message.reply_text("Счет не найден.")
        return

    if action in {"approve", "reject"}:
        if (
            get_cell(row, STATUS_COL) != STATUS_PENDING_APPROVAL
            or not callback_matches_message(
                row,
                query.message.chat_id,
                query.message.message_id,
                "approval"
            )
        ):
            await query.message.reply_text("Это сообщение уже неактуально.")
            return

        if action == "approve":
            now = datetime.now(REMINDER_TZ)
            approver_name = query.from_user.username or query.from_user.first_name
            sheet.update_cell(sheet_row_number, STATUS_COL + 1, STATUS_APPROVED)
            sheet.update_cell(sheet_row_number, APPROVED_AT_COL + 1, now.isoformat())
            sheet.update_cell(sheet_row_number, APPROVER_NAME_COL + 1, approver_name)
            set_cell(row, STATUS_COL, STATUS_APPROVED)
            set_cell(row, APPROVED_AT_COL, now.isoformat())
            set_cell(row, APPROVER_NAME_COL, approver_name)

            try:
                await edit_invoice_message(
                    context.bot,
                    query.message.chat_id,
                    query.message.message_id,
                    row,
                    build_approved_approval_text(row)
                )
            except Exception:
                logging.exception("Could not edit approved request %s", request_id)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    logging.exception("Could not remove approval keyboard for request %s", request_id)

            await notify_creator_invoice_approved(context.bot, row)
            await send_due_payment_invoice(
                context.bot,
                sheet_row_number,
                row,
                now
            )
            return

        msg = await query.message.reply_text("Введите причину отклонения:")
        reject_state[query.from_user.id] = {
            "request_id": request_id,
            "message_id": query.message.message_id,
            "chat_id": query.message.chat_id,
            "ask_message_id": msg.message_id,
            "result_status": STATUS_REJECTED,
            "action_text": "отклонен",
            "creator_message_title": "не согласован",
            "expected_status": STATUS_PENDING_APPROVAL,
            "stage": "approval"
        }
        return

    if action in {"paid", "cancel"}:
        if (
            get_cell(row, STATUS_COL) != STATUS_APPROVED
            or not callback_matches_message(
                row,
                query.message.chat_id,
                query.message.message_id,
                "payment"
            )
        ):
            await query.message.reply_text("Это сообщение уже неактуально.")
            return

        if action == "paid":
            payment_state[query.from_user.id] = {
                "request_id": request_id,
                "message_id": query.message.message_id,
                "chat_id": query.message.chat_id
            }
            msg = await query.message.reply_text(
                "📎 Прикрепите чек или подтверждение оплаты"
            )
            payment_state[query.from_user.id]["ask_message_id"] = msg.message_id
            return

        msg = await query.message.reply_text("Введите причину отмены счета:")
        reject_state[query.from_user.id] = {
            "request_id": request_id,
            "message_id": query.message.message_id,
            "chat_id": query.message.chat_id,
            "ask_message_id": msg.message_id,
            "result_status": STATUS_CANCELLED,
            "action_text": "отменен",
            "creator_message_title": "отменен",
            "expected_status": STATUS_APPROVED,
            "stage": "payment"
        }
        return

    await query.message.reply_text("Неизвестное действие.")

class MiniAppHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        redacted_args = []
        for arg in args:
            if isinstance(arg, str) and MIGRATION_SECRET:
                redacted_args.append(arg.replace(MIGRATION_SECRET, "[migration-secret]"))
            else:
                redacted_args.append(arg)

        logging.info("web: " + format, *redacted_args)

    def send_headers(self, status, content_type, content_length=0):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.end_headers()

    def send_bytes(self, status, content, content_type):
        self.send_headers(status, content_type, len(content))
        self.wfile.write(content)

    def send_json(self, status, payload):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, content, "application/json; charset=utf-8")

    def send_text(self, status, text):
        self.send_bytes(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def query_value(self, query, name, default=""):
        values = query.get(name)
        return values[0] if values else default

    def do_migration(self, parsed):
        if not MIGRATION_SECRET:
            self.send_text(403, "Migration endpoint is disabled: MIGRATION_SECRET is not set.")
            return

        query = parse_qs(parsed.query)
        if self.query_value(query, "secret") != MIGRATION_SECRET:
            self.send_text(403, "Forbidden.")
            return

        mode = self.query_value(query, "mode", "dry-run")
        if mode not in ("dry-run", "run"):
            self.send_text(400, "mode must be dry-run or run.")
            return

        if mode == "run" and self.query_value(query, "confirm") != "RUN":
            self.send_text(400, "For mode=run add confirm=RUN.")
            return

        command = [sys.executable, str(BASE_DIR / "migrate_active_invoices.py")]
        command.append("--run" if mode == "run" else "--dry-run")

        for request_id in query.get("request_id", []):
            if request_id.strip():
                command.extend(["--request-id", request_id.strip()])

        limit = self.query_value(query, "limit")
        if limit:
            if not limit.isdigit():
                self.send_text(400, "limit must be a positive number.")
                return
            command.extend(["--limit", limit])

        keep_old = self.query_value(query, "keep_old").lower()
        if keep_old in ("1", "true", "yes", "y"):
            command.append("--keep-old")

        try:
            result = subprocess.run(
                command,
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                timeout=MIGRATION_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            output = (
                f"Migration timed out after {MIGRATION_TIMEOUT_SECONDS} seconds.\n\n"
                f"STDOUT:\n{exc.stdout or ''}\n\n"
                f"STDERR:\n{exc.stderr or ''}"
            )
            self.send_text(504, output)
            return

        output = (
            f"Command: {' '.join(command)}\n"
            f"Exit code: {result.returncode}\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )
        self.send_text(200 if result.returncode == 0 else 500, output)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/health"):
            self.send_bytes(200, b"OK", "text/plain; charset=utf-8")
            return

        if path == "/health/details":
            self.send_json(200, {
                "ok": True,
                "ocr": {
                    "enabled": DDS_OCR_ENABLED,
                    "runtime_available": OCR_RUNTIME_AVAILABLE,
                    "mode": DDS_OCR_MODE,
                },
            })
            return

        if path in ("/miniapp", "/miniapp/"):
            html = (BASE_DIR / "miniapp.html").read_bytes()
            self.send_bytes(200, html, "text/html; charset=utf-8")
            return

        if path == "/api/dashboard":
            try:
                query = parse_qs(parsed.query)
                user = get_miniapp_user(self.query_value(query, "initData"))
                username = miniapp_username(user)
                dashboard = build_dashboard(
                    sheet.get_all_values(),
                    projects_sheet.get_all_values(),
                    username,
                )
                self.send_json(200, {
                    "ok": True,
                    "user": {
                        "id": user.get("id"),
                        "username": username,
                    },
                    **dashboard,
                })
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception:
                logging.exception("Failed to load Mini App dashboard")
                self.send_json(500, {"ok": False, "error": "Не удалось загрузить счета."})
            return

        if path in ("/migration", "/migration/"):
            self.do_migration(parsed)
            return

        self.send_json(404, {"ok": False, "error": "Not found"})

    def do_HEAD(self):
        path = urlparse(self.path).path

        if path in ("/", "/health", "/health/details"):
            self.send_headers(200, "text/plain; charset=utf-8", 0)
            return

        if path in ("/miniapp", "/miniapp/"):
            self.send_headers(200, "text/html; charset=utf-8", 0)
            return

        self.send_headers(404, "text/plain; charset=utf-8", 0)

    def do_POST(self):
        path = urlparse(self.path).path
        action_match = re.fullmatch(
            r"/api/requests/([^/]+)/(approve|reject|pay)",
            path,
        )
        if path != "/api/requests" and not action_match:
            self.send_json(404, {"ok": False, "error": "Not found"})
            return

        content_length = parse_int(self.headers.get("Content-Length")) or 0
        if content_length > MINIAPP_MAX_UPLOAD_BYTES:
            self.send_json(413, {
                "ok": False,
                "error": "Файл слишком большой. Максимальный размер — 10 МБ.",
            })
            return

        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(content_length),
                }
            )
            if path == "/api/requests":
                request_id = create_request_from_miniapp(form)
                self.send_json(200, {"ok": True, "request_id": request_id})
                return

            request_id, action = action_match.groups()
            user = get_miniapp_user(form_value(form, "initData"))
            if action == "approve":
                approve_request_from_miniapp(request_id, user)
            elif action == "reject":
                reject_request_from_miniapp(
                    request_id,
                    user,
                    form_value(form, "reason"),
                )
            else:
                pay_request_from_miniapp(request_id, user, get_uploaded_file(form))
            self.send_json(200, {"ok": True, "request_id": request_id})
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception:
            logging.exception("Failed to process Mini App request: %s", path)
            self.send_json(500, {"ok": False, "error": "Не удалось выполнить действие."})

def run_web():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), MiniAppHandler)
    server.serve_forever()

async def setup_bot_menu(application):
    if not WEBAPP_URL:
        logging.warning("Telegram Mini App menu button is disabled: WEBAPP_URL is not set")
        return

    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Открыть",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
        logging.info("Telegram Mini App menu button configured: %s", WEBAPP_URL)
    except Exception:
        logging.exception("Failed to configure Telegram Mini App menu button")

def main():
    Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).post_init(setup_bot_menu).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.Document.ALL | filters.PHOTO,
            handle_dds_standalone_message,
        ),
        group=1,
    )

    if app.job_queue:
        app.job_queue.run_repeating(
            send_scheduled_taxi_summaries,
            interval=PAYMENT_DISPATCH_INTERVAL_SECONDS,
            first=20,
            name="scheduled_taxi_summaries"
        )

        app.job_queue.run_repeating(
            send_scheduled_payments,
            interval=PAYMENT_DISPATCH_INTERVAL_SECONDS,
            first=15,
            name="scheduled_payments"
        )

        app.job_queue.run_repeating(
            send_scheduled_approval_reminders,
            interval=APPROVAL_REMINDER_INTERVAL_SECONDS,
            first=25,
            name="scheduled_approval_reminders"
        )

    else:
        logging.warning("Scheduled payments are disabled because JobQueue is not available")

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
