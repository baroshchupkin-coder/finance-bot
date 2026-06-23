import logging
import gspread
import cgi
import hashlib
import hmac
import requests
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
from urllib.parse import parse_qsl, urlparse

import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
LAST_PAYMENT_REMINDER_AT_COL = 19
LAST_INVOICE_MESSAGE_CHAT_ID_COL = 20
LAST_INVOICE_MESSAGE_ID_COL = 21
EXPENSE_CATEGORY_COL = 22

STATUS_APPROVED = "Согласован"
STATUS_PAID = "Оплачено"
STATUS_REJECTED = "Отклонен"
STATUS_CANCELLED = "Отменен"
REMINDER_TIMEZONE_NAME = os.getenv("REMINDER_TIMEZONE", "Asia/Bishkek")
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))
WEEKLY_REMINDER_WEEKDAY = int(os.getenv("WEEKLY_REMINDER_WEEKDAY", "0"))
REMINDER_EXISTING_ROWS_PAUSE_FROM = os.getenv("REMINDER_EXISTING_ROWS_PAUSE_FROM", "2026-06-04")
EXPENSE_CATEGORIES = [
    ("team", "Команда"),
    ("ads", "Рекламный бюджет"),
    ("services", "Сервисы")
]
EXPENSE_CATEGORY_BY_KEY = dict(EXPENSE_CATEGORIES)
EXPENSE_CATEGORY_LABELS = [label for _, label in EXPENSE_CATEGORIES]

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
BASE_DIR = Path(__file__).resolve().parent
MINIAPP_REQUIRE_INIT_DATA = os.getenv("MINIAPP_REQUIRE_INIT_DATA", "true").lower() != "false"
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
if not WEBAPP_URL and os.getenv("RENDER_EXTERNAL_URL"):
    WEBAPP_URL = os.getenv("RENDER_EXTERNAL_URL").rstrip("/") + "/miniapp"

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

def get_user_tag(user):
    if user.username:
        return f"@{user.username}"
    return user.first_name

def get_expense_category(row):
    return get_cell(row, EXPENSE_CATEGORY_COL, "Без статьи")

def build_approved_invoice_text(row):
    payer_tag = get_cell(row, PAYER_TAG_COL)
    approver_name = get_cell(row, APPROVER_NAME_COL, "неизвестно")

    return (
        f"{payer_tag}\n"
        f"Счет #{get_cell(row, REQUEST_ID_COL)} одобрен\n\n"
        f"{get_cell(row, 4)}\n\n"
        f"{get_cell(row, 6)}\n\n"
        f"Согласовано: @{approver_name}"
    )

def get_payment_reminder_kind(row, today):
    if get_cell(row, STATUS_COL) != STATUS_APPROVED:
        return None

    approved_at = parse_iso_date(get_cell(row, APPROVED_AT_COL))
    last_reminder_at = parse_iso_date(get_cell(row, LAST_PAYMENT_REMINDER_AT_COL))

    if last_reminder_at:
        if today.weekday() == WEEKLY_REMINDER_WEEKDAY and last_reminder_at < today:
            return "weekly"
        return None

    if not approved_at:
        return None

    existing_rows_pause_from = parse_iso_date(REMINDER_EXISTING_ROWS_PAUSE_FROM)

    if existing_rows_pause_from and approved_at < existing_rows_pause_from:
        if today.weekday() == WEEKLY_REMINDER_WEEKDAY and today > existing_rows_pause_from:
            return "weekly"
        return None

    if approved_at < today:
        return "first"

    return None

def get_unpaid_rows_due_for_reminder(rows):
    today = datetime.now(REMINDER_TZ).date()
    due_rows = []

    for sheet_row_number, row in enumerate(rows[1:], start=2):
        reminder_kind = get_payment_reminder_kind(row, today)
        if reminder_kind:
            due_rows.append((sheet_row_number, row, reminder_kind))

    return due_rows

async def send_approved_invoice(bot, chat_id, row):
    request_id = get_cell(row, REQUEST_ID_COL)
    file_id = get_cell(row, FILE_ID_COL)
    text = build_approved_invoice_text(row)
    keyboard = build_paid_keyboard(request_id)

    if file_id:
        if is_photo_file(file_id):
            return await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=text,
                reply_markup=keyboard
            )
        else:
            return await bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=text,
                reply_markup=keyboard
            )
    else:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard
        )

def save_last_invoice_message(sheet_row_number, message):
    sheet.update_cell(sheet_row_number, LAST_INVOICE_MESSAGE_CHAT_ID_COL + 1, str(message.chat_id))
    sheet.update_cell(sheet_row_number, LAST_INVOICE_MESSAGE_ID_COL + 1, str(message.message_id))

async def delete_last_invoice_message(bot, row):
    chat_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_CHAT_ID_COL))
    message_id = parse_int(get_cell(row, LAST_INVOICE_MESSAGE_ID_COL))

    if not chat_id or not message_id:
        return

    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logging.info(
            "Could not delete previous invoice message %s in chat %s",
            message_id,
            chat_id
        )

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

async def send_payment_reminders(context: ContextTypes.DEFAULT_TYPE):
    rows = sheet.get_all_values()
    reminders = {}
    today = datetime.now(REMINDER_TZ).date().isoformat()

    for sheet_row_number, row, reminder_kind in get_unpaid_rows_due_for_reminder(rows):
        try:
            chat_id = int(get_cell(row, APPROVER_CHAT_ID_COL))
        except ValueError:
            logging.warning("Skipping reminder for request %s: invalid chat id", get_cell(row, REQUEST_ID_COL))
            continue

        payer_tag = get_cell(row, PAYER_TAG_COL)
        reminders.setdefault((chat_id, payer_tag), []).append((sheet_row_number, row, reminder_kind))

    for (chat_id, payer_tag), unpaid_rows in reminders.items():
        if payer_tag:
            reminder_text = f"{payer_tag}, напоминаю про оплаты."
        else:
            reminder_text = "Напоминаю про оплаты."

        reminder_text += (
            "\n\nЕсли счета были оплачены, прошу нажать кнопку \"оплатил\" "
            "и прикрепить платежные поручения."
        )

        try:
            await context.bot.send_message(chat_id=chat_id, text=reminder_text)

            first_rows = [
                (sheet_row_number, row)
                for sheet_row_number, row, reminder_kind in unpaid_rows
                if reminder_kind == "first"
            ]
            weekly_rows = [
                (sheet_row_number, row)
                for sheet_row_number, row, reminder_kind in unpaid_rows
                if reminder_kind == "weekly"
            ]

            for sheet_row_number, row in first_rows:
                await delete_last_invoice_message(context.bot, row)
                sent_message = await send_approved_invoice(context.bot, chat_id, row)
                sheet.update_cell(sheet_row_number, LAST_PAYMENT_REMINDER_AT_COL + 1, today)
                save_last_invoice_message(sheet_row_number, sent_message)

            rows_by_category = {}
            for sheet_row_number, row in weekly_rows:
                rows_by_category.setdefault(get_expense_category(row), []).append((sheet_row_number, row))

            category_order = EXPENSE_CATEGORY_LABELS + sorted(
                category for category in rows_by_category
                if category not in EXPENSE_CATEGORY_LABELS
            )

            for category in category_order:
                category_rows = rows_by_category.get(category)
                if not category_rows:
                    continue

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"<b>{category}</b>",
                    parse_mode="HTML"
                )

                for sheet_row_number, row in category_rows:
                    await delete_last_invoice_message(context.bot, row)
                    sent_message = await send_approved_invoice(context.bot, chat_id, row)
                    sheet.update_cell(sheet_row_number, LAST_PAYMENT_REMINDER_AT_COL + 1, today)
                    save_last_invoice_message(sheet_row_number, sent_message)
        except Exception:
            logging.exception("Failed to send payment reminder to chat %s", chat_id)

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
        sheet.update_cell(i+1, LAST_PAYMENT_REMINDER_AT_COL + 1, datetime.now(REMINDER_TZ).date().isoformat())

        caption = (
            f"{payer_tag}\n"
            f"Счет #{request_id}\n\n"
            "Оплата по данному чеку не получена"
        )

        if receipt_file_type == "photo":
            await context.bot.send_photo(
                chat_id=int(payment_chat_id),
                photo=receipt_file_id,
                caption=caption,
                reply_markup=build_paid_keyboard(request_id)
            )
        else:
            await context.bot.send_document(
                chat_id=int(payment_chat_id),
                document=receipt_file_id,
                caption=caption,
                reply_markup=build_paid_keyboard(request_id)
            )
        return

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Не удалось найти счет #{request_id}."
    )

def get_project_settings(project_name):
    rows = projects_sheet.get_all_values()

    for row in rows[1:]:  # пропускаем заголовок
        if row[0].strip().lower() == project_name.strip().lower():
            return {
                "approver_chat_id": int(row[1]),
                "payer_tag": row[2].strip() if len(row) > 2 else ""
            }

    return None

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
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=data,
        files=files,
        timeout=30
    )
    payload = response.json()

    if not response.ok or not payload.get("ok"):
        description = payload.get("description", response.text)
        raise RuntimeError(f"Telegram API error: {description}")

    return payload["result"]

def approval_reply_markup(request_id):
    return json.dumps({
        "inline_keyboard": [[
            {"text": "✅ Одобрить", "callback_data": f"approve_{request_id}"},
            {"text": "❌ Отклонить", "callback_data": f"reject_{request_id}"}
        ]]
    }, ensure_ascii=False)

def send_approval_request_via_api(chat_id, request_id, target, comment, uploaded_file):
    text = (
        f"Новый счет #{request_id}\n\n"
        f"{target}\n\n"
        f"{comment}"
    )
    data = {
        "chat_id": str(chat_id),
        "reply_markup": approval_reply_markup(request_id)
    }

    if not uploaded_file:
        data["text"] = text
        telegram_api_request("sendMessage", data)
        return ""

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

    if is_photo:
        return result["photo"][-1]["file_id"]

    return result["document"]["file_id"]

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
    if not comment:
        raise ValueError("Введите комментарий.")

    project_settings = get_project_settings(project)
    if not project_settings:
        raise ValueError("Для этого проекта не найден согласующий.")

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
        "На согласовании",
        project_settings["approver_chat_id"],
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
        expense_category
    ]

    sheet.append_row(row)
    file_id = send_approval_request_via_api(
        project_settings["approver_chat_id"],
        request_id,
        target,
        comment,
        uploaded_file
    )

    if file_id:
        sheet.update_cell(sheet_row_number, FILE_ID_COL + 1, file_id)

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
    # ===== ЗАГРУЗКА ЧЕКА ПОСЛЕ ОПЛАТЫ =====
    user_id = update.effective_user.id

    if user_id in payment_state:

        data = payment_state.pop(user_id)

        request_id = data["request_id"]
        message_id = data["message_id"]
        original_chat_id = data.get("chat_id", chat_id)
        ask_message_id = data.get("ask_message_id")

        file_id = None

        if update.message.document:
            file_id = update.message.document.file_id
            receipt_file_type = "document"
        elif update.message.photo:
            file_id = update.message.photo[-1].file_id
            receipt_file_type = "photo"

        rows = sheet.get_all_values()

        for i, row in enumerate(rows):
            if row[0] == request_id:

                payer_tag = get_user_tag(update.effective_user)
                approver_name = get_cell(row, APPROVER_NAME_COL, "неизвестно")

                sheet.update_cell(i+1, 8, STATUS_PAID)
                sheet.update_cell(i+1, PAYMENT_CHAT_ID_COL + 1, str(original_chat_id))
                sheet.update_cell(i+1, PAYMENT_PAYER_TAG_COL + 1, payer_tag)
                sheet.update_cell(i+1, PAYMENT_RECEIPT_FILE_ID_COL + 1, file_id)
                sheet.update_cell(i+1, PAYMENT_RECEIPT_FILE_TYPE_COL + 1, receipt_file_type)

                text = (
                    f"Счет #{request_id} — Оплачен✅\n\n"
                    f"{row[4]}\n\n"
                    f"{row[6]}\n\n"
                    f"Согласовано: @{approver_name}\n"
                    f"Оплачено: {payer_tag}"
                )

                try:
                    await context.bot.edit_message_caption(
                        chat_id=original_chat_id,
                        message_id=message_id,
                        caption=text
                    )
                except:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=original_chat_id,
                            message_id=message_id,
                            text=text
                        )
                    except:
                        pass

                try:
                    await update.message.delete()
                except:
                    pass

                await send_receipt_to_payment_chat(
                    context.bot,
                    original_chat_id,
                    file_id,
                    receipt_file_type,
                    request_id,
                    message_id
                )
                creator_chat_id = int(row[CREATOR_CHAT_ID_COL])
                project_name = get_cell(row, 3, "неизвестно")
                amount = get_cell(row, 5, "не указана")
                creator_receipt_caption = (
                    f"💰 Счет #{request_id} по проекту {project_name} оплачен\n\n"
                    f"Сумма: {amount}\n\n"
                    f"Оплата получена?"
                )

                if update.message.photo:
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
                # удаляем сообщение "прикрепите чек"
                try:
                    if ask_message_id:
                        await context.bot.delete_message(
                            chat_id=original_chat_id,
                            message_id=ask_message_id
                        )
                except:
                    pass

                return
    if chat_id not in user_state:
        return

    state = user_state[chat_id]

    # 🔒 принимаем файл ТОЛЬКО если ждем его
    if "amount" not in state or "file_step_done" in state:
        return

    file_id = None

    if update.message.document:
        file_id = update.message.document.file_id
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id

    if file_id:
        state["file_id"] = file_id
        state["file_step_done"] = True

        await update.message.reply_text("Введите комментарий:\n\n"
        "*Пример*\n"
        "??? сом - фиксированная часть за ...-...\n"
        "??? сом - KPI за *месяц*\n"
        "??? сом - % за *месяц*\n\n"
        "??? сом - итоговая сумма к оплате\n\n"
                                        
        "или\n\n"

        "??? сом - услуга\n\n"

        "перевод на карту 'номер телефона, банк' (если оплата не по счету)"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 👉 РАЗРЕШАЕМ ввод причины отклонения В ЛЮБОМ ЧАТЕ
    if update.effective_user.id in reject_state:
        pass
    elif update.effective_chat.type != "private":
        return
    chat_id = update.effective_chat.id
    text = update.message.text

    # ===== ОБРАБОТКА ОТКЛОНЕНИЯ (причина) =====
    if update.effective_user.id in reject_state:
        data = reject_state.pop(update.effective_user.id)

        request_id = data["request_id"]
        message_id = data["message_id"]
        chat_id_to_delete = data["chat_id"]
        ask_message_id = data.get("ask_message_id") 
        result_status = data.get("result_status", STATUS_REJECTED)
        action_text = data.get("action_text", "отклонен")
        creator_message_title = data.get("creator_message_title", "не согласован")

        rows = sheet.get_all_values()
        # ❗ удаляем сообщение со счетом (с кнопками)
        try:
            await context.bot.delete_message(
                chat_id=chat_id_to_delete,
                message_id=message_id
            )
        except:
            pass

        # ❗ удаляем сообщение "Введите причину..."
        try:
            if ask_message_id:
                await context.bot.delete_message(
                    chat_id=chat_id_to_delete,
                    message_id=ask_message_id
                )
        except:
            pass
             
        # ❗ удаляем сообщение с текстом причины пользователя
        try:
            await update.message.delete()
        except:
            pass 
         
        for i, row in enumerate(rows):
            if row[0] == request_id:

                sheet.update_cell(i+1, 8, result_status)

                creator_chat_id = int(row[10])

                comment = text

                await context.bot.send_message(
                    chat_id=creator_chat_id,
                    text=f"❌ Ваш счет #{request_id} {creator_message_title}\n\n"
                         f"Причина: {comment}\n\n"
                         f"Просьба отправить счет заново с учетом комментария"
                )
                break

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Счет #{request_id} {action_text} и комментарий отправлен"
        )
        return
    if chat_id not in user_state:
        await update.message.reply_text(
            "Напиши /new чтобы отправить счет"
        )
        return

    state = user_state[chat_id]

    # ЭТАП 1 — ПРОЕКТ
    if "project" not in state:
        project_settings = get_project_settings(text)

        if not project_settings:
            await update.message.reply_text(
                "❌ Для этого проекта не найден согласующий\n"
                "Пожалуйста, введите аббревиатуру проекта снова:"
            )
            return

        state["project"] = text
        state["approver_id"] = project_settings["approver_chat_id"]
        state["payer_tag"] = project_settings["payer_tag"]

        await update.message.reply_text(
            "К какой статье расхода относится ваш счёт?",
            reply_markup=build_expense_category_keyboard()
        )
        return

    # ЭТАП 2 — СТАТЬЯ РАСХОДА
    if "expense_category" not in state:
        await update.message.reply_text(
            "Пожалуйста, выберите статью расхода кнопкой."
        )
        return

    # ЭТАП 3 — КОМУ ПЛАТИМ
    if "target" not in state:
        state["target"] = text

        await update.message.reply_text("Введите сумму:")
        return

    # ЭТАП 4 — СУММА
    if "amount" not in state:
        state["amount"] = text

        keyboard = [
            [InlineKeyboardButton("⏭ Пропустить", callback_data="skip_file")]
        ]

        await update.message.reply_text(
            "📎 Прикрепите файл (счет, чек и т.д.)\n"
            "Или нажмите 'Пропустить'",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # ЭТАП 5 — КОММЕНТАРИЙ
    if "file_step_done" not in state:
        return

    if "comment" not in state:
        state["comment"] = text

    # ===== СОЗДАЁМ ЗАЯВКУ =====
    row = [
        str(len(sheet.get_all_values())),
        str(update.message.date),
        update.effective_user.username,
        state["project"],
        state["target"],
        state["amount"],
        state["comment"],
        "На согласовании",
        state["approver_id"],
        state.get("file_id", ""),
        str(update.effective_user.id),  # 👈 chat_id (ВАЖНО)
        update.effective_user.username or update.effective_user.first_name,  # 👈 имя
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
        state.get("expense_category", "")
    ]

    sheet.append_row(row)

    request_id = row[0]

    await update.message.reply_text(
        "Счёт принят! Ответственный получил уведомление.\n\n"
        "Напиши /new чтобы отправить новый счёт"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{request_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{request_id}")
        ]
    ]

    # ===== ОТПРАВКА СОГЛАСУЮЩЕМУ =====
    text = (
        f"Новый счет #{request_id}\n\n"
        f"{row[4]}\n\n" # Кому платим
        f"{row[6]}" # Комментарий
    )

    file_id = state.get("file_id")

    if file_id:

        if file_id.startswith(("Ag", "AQ")):
            await context.bot.send_photo(
                chat_id=state["approver_id"],
                photo=file_id,
                caption=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await context.bot.send_document(
                chat_id=state["approver_id"],
                document=file_id,
                caption=text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    else:
        await context.bot.send_message(
            chat_id=state["approver_id"],
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    user_state.pop(chat_id)
    return

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # 👉 ОБРАБОТКА ПРОПУСКА ФАЙЛА
    if data == "skip_file":
        chat_id = query.message.chat_id
        state = user_state.get(chat_id)

        if state:
            state["file_step_done"] = True

        await query.message.reply_text("Введите комментарий:\n\n"
        "*Пример*\n"
        "??? сом - фиксированная часть за ...-...\n"
        "??? сом - KPI за *месяц*\n"
        "??? сом - % за *месяц*\n\n"
        "??? сом - итоговая сумма к оплате\n\n"
                                       
        "или\n\n"

        "??? сом - услуга\n\n"

        "перевод на карту 'номер телефона, банк' (если оплата не по счету)"
        )
        await query.answer()
        return

    if data.startswith("expense_"):
        category_key = data.split("_", 1)[1]
        category = EXPENSE_CATEGORY_BY_KEY.get(category_key)
        chat_id = query.message.chat_id
        state = user_state.get(chat_id)

        if not category or not state or "project" not in state:
            await query.answer("Не удалось выбрать статью расхода")
            return

        state["expense_category"] = category

        try:
            await query.message.edit_text(f"Статья расхода: {category}")
        except Exception:
            logging.info("Could not edit expense category prompt")

        await query.message.reply_text("Кому платим? (Имя Фамилия, компания, сервис)")
        return

    if data.startswith("received_"):
        _, answer, request_id = data.split("_", 2)
        await handle_payment_received_confirmation(query, context, answer, request_id)
        return

    # обычные кнопки
    action, request_id = data.split("_")

    rows = sheet.get_all_values()

    for i, row in enumerate(rows):
        if row[0] == request_id:

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

            elif action == "approve":
                sheet.update_cell(i+1, 8, STATUS_APPROVED)
                sheet.update_cell(i+1, APPROVED_AT_COL + 1, datetime.now(REMINDER_TZ).date().isoformat())

                approver_name = query.from_user.username or query.from_user.first_name
                sheet.update_cell(i+1, 13, approver_name)

                await query.message.delete()

                set_cell(row, STATUS_COL, STATUS_APPROVED)
                set_cell(row, APPROVER_NAME_COL, approver_name)

                sent_message = await send_approved_invoice(
                    context.bot,
                    int(get_cell(row, APPROVER_CHAT_ID_COL)),
                    row
                )
                save_last_invoice_message(i+1, sent_message)
            elif action == "reject":                   
                msg = await query.message.reply_text("Введите причину отклонения:")

                reject_state[query.from_user.id] = {
                    "request_id": request_id,
                    "message_id": query.message.message_id,
                    "chat_id": query.message.chat_id,
                    "ask_message_id": msg.message_id,  # 👈 ВАЖНО
                    "result_status": STATUS_REJECTED,
                    "action_text": "отклонен",
                    "creator_message_title": "не согласован"
                }
                return

            elif action == "cancel":
                msg = await query.message.reply_text("Введите причину отмены счета:")

                reject_state[query.from_user.id] = {
                    "request_id": request_id,
                    "message_id": query.message.message_id,
                    "chat_id": query.message.chat_id,
                    "ask_message_id": msg.message_id,
                    "result_status": STATUS_CANCELLED,
                    "action_text": "отменен",
                    "creator_message_title": "отменен"
                }
                return

            break

    # ТЕКСТ ДЛЯ КНОПКИ
    if action == "approve":
        text = "✅ Счет согласован"
    elif action == "reject":
        text = "❌ Счет отклонен"
    elif action == "paid":
        text = "💰 Счет оплачен"
    elif action == "cancel":
        text = "❌ Счет отменен"
    else:
        text = action

    if action not in ["approve", "paid"]:  # approve уже удаляет сообщение
        await query.edit_message_text(f"Счет {request_id}\n{text}")
         
class MiniAppHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logging.info("web: " + format, *args)

    def send_bytes(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status, payload):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, content, "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/health"):
            self.send_bytes(200, b"OK", "text/plain; charset=utf-8")
            return

        if path in ("/miniapp", "/miniapp/"):
            html = (BASE_DIR / "miniapp.html").read_bytes()
            self.send_bytes(200, html, "text/html; charset=utf-8")
            return

        self.send_json(404, {"ok": False, "error": "Not found"})

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
        app.job_queue.run_daily(
            send_payment_reminders,
            time=time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=REMINDER_TZ),
            name="payment_reminders"
        )
    else:
        logging.warning("Payment reminders are disabled because JobQueue is not available")

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
