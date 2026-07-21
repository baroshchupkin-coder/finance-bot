import logging
import gspread
import cgi
import hashlib
import hmac
import requests
import subprocess
import sys
from oauth2client.service_account import ServiceAccountCredentials
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    WebAppInfo
)
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
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

TOKEN = os.getenv("BOT_TOKEN")

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

STATUS_APPROVED = "Согласован"
STATUS_PENDING_APPROVAL = "На согласовании"
STATUS_PAID = "Оплачено"
STATUS_REJECTED = "Отклонен"
STATUS_CANCELLED = "Отменен"
REMINDER_TIMEZONE_NAME = os.getenv("REMINDER_TIMEZONE", "Asia/Bishkek")
PAYMENT_DISPATCH_HOUR = int(os.getenv("PAYMENT_DISPATCH_HOUR", os.getenv("REMINDER_HOUR", "10")))
PAYMENT_DISPATCH_MINUTE = int(os.getenv("PAYMENT_DISPATCH_MINUTE", os.getenv("REMINDER_MINUTE", "0")))
PAYMENT_DISPATCH_INTERVAL_SECONDS = int(os.getenv("PAYMENT_DISPATCH_INTERVAL_SECONDS", "300"))
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
OR_ADS_PAYER_TAG = "@bulat_sufyanov"
OR_PROJECT_KEYS = {"or", "or kg", "orkg"}
OR_ADS_EXPENSE_CATEGORY = EXPENSE_CATEGORY_BY_KEY["ads"]
OR_PROJECT_TRANSLATION = str.maketrans({
    "\u043e": "o",
    "\u0440": "r",
    "\u043a": "k",
    "\u0433": "g",
})

try:
    REMINDER_TZ = ZoneInfo(REMINDER_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    REMINDER_TZ = timezone.utc

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
reject_state = {}
user_state = {}
payment_state = {}
payment_dispatch_claims = set()
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
                "approval_chat_id": parse_int(get_cell(row, 3))
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

    return hmac.compare_digest(calculated_hash, received_hash)

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
        resolve_payer_tag(project, expense_category, project_settings["payer_tag"]),
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
        ""
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
        resolve_payer_tag(
            state["project"],
            state.get("expense_category", ""),
            state.get("payer_tag", "")
        ),
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
        ""
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

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "skip_file":
        chat_id = query.message.chat_id
        state = user_state.get(chat_id)
        if not state or "payment_due_date" not in state:
            await query.answer("Форма уже неактуальна.", show_alert=True)
            return

        state["file_step_done"] = True
        await query.answer()
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
            await query.answer("Не удалось выбрать статью расхода", show_alert=True)
            return

        if category != TAXI_EXPENSE_CATEGORY and not state.get("payment_chat_id"):
            await query.answer("Для проекта не заполнен payment_chat_id", show_alert=True)
            await query.message.reply_text(
                "❌ Для проекта не заполнен payment_chat_id.\n"
                "Обратитесь к администратору бота."
            )
            user_state.pop(chat_id, None)
            return

        state["expense_category"] = category
        await query.answer()
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
            await query.answer("Некорректная команда.", show_alert=True)
            return
        _, answer, request_id = parts
        await query.answer()
        await handle_payment_received_confirmation(query, context, answer, request_id)
        return

    try:
        action, request_id = data.split("_", 1)
    except ValueError:
        await query.answer("Некорректная команда.", show_alert=True)
        return

    rows = sheet.get_all_values()
    row = None
    sheet_row_number = None
    for i, candidate in enumerate(rows):
        if get_cell(candidate, REQUEST_ID_COL) == request_id:
            row = candidate
            sheet_row_number = i + 1
            break

    if row is None:
        await query.answer("Счет не найден.", show_alert=True)
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
            await query.answer("Это сообщение уже неактуально.", show_alert=True)
            return

        if action == "approve":
            await query.answer("Счет согласован")
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

        await query.answer()
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
            await query.answer("Это сообщение уже неактуально.", show_alert=True)
            return

        if action == "paid":
            await query.answer()
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

        await query.answer()
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

    await query.answer("Неизвестное действие.", show_alert=True)

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

        if path in ("/miniapp", "/miniapp/"):
            html = (BASE_DIR / "miniapp.html").read_bytes()
            self.send_bytes(200, html, "text/html; charset=utf-8")
            return

        if path in ("/migration", "/migration/"):
            self.do_migration(parsed)
            return

        self.send_json(404, {"ok": False, "error": "Not found"})

    def do_HEAD(self):
        path = urlparse(self.path).path

        if path in ("/", "/health"):
            self.send_headers(200, "text/plain; charset=utf-8", 0)
            return

        if path in ("/miniapp", "/miniapp/"):
            self.send_headers(200, "text/html; charset=utf-8", 0)
            return

        self.send_headers(404, "text/plain; charset=utf-8", 0)

    def do_POST(self):
        path = urlparse(self.path).path

        if path != "/api/requests":
            self.send_json(404, {"ok": False, "error": "Not found"})
            return

        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", "")
                }
            )
            request_id = create_request_from_miniapp(form)
            self.send_json(200, {"ok": True, "request_id": request_id})
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception:
            logging.exception("Failed to create request from Mini App")
            self.send_json(500, {"ok": False, "error": "Не удалось отправить счет."})

def run_web():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), MiniAppHandler)
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

    else:
        logging.warning("Scheduled payments are disabled because JobQueue is not available")

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
