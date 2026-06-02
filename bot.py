import logging
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

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

STATUS_APPROVED = "Согласован"
STATUS_PAID = "Оплачено"
REMINDER_TIMEZONE_NAME = os.getenv("REMINDER_TIMEZONE", "Asia/Bishkek")
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))

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

def get_cell(row, index, default=""):
    return row[index].strip() if len(row) > index and row[index] else default

def set_cell(row, index, value):
    while len(row) <= index:
        row.append("")
    row[index] = value

def is_photo_file(file_id):
    return file_id.startswith(("Ag", "AQ"))

def build_paid_keyboard(request_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Оплатил – прикрепить чек", callback_data=f"paid_{request_id}")
        ]
    ])

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

def was_approved_before_today(row, today):
    approved_at = get_cell(row, APPROVED_AT_COL)

    if not approved_at:
        return True

    try:
        return datetime.fromisoformat(approved_at).date() < today
    except ValueError:
        return True

def get_unpaid_rows_due_for_reminder(rows):
    today = datetime.now(REMINDER_TZ).date()
    due_rows = []

    for row in rows[1:]:
        if get_cell(row, STATUS_COL) != STATUS_APPROVED:
            continue
        if not was_approved_before_today(row, today):
            continue
        due_rows.append(row)

    return due_rows

async def send_approved_invoice(bot, chat_id, row):
    request_id = get_cell(row, REQUEST_ID_COL)
    file_id = get_cell(row, FILE_ID_COL)
    text = build_approved_invoice_text(row)
    keyboard = build_paid_keyboard(request_id)

    if file_id:
        if is_photo_file(file_id):
            await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=text,
                reply_markup=keyboard
            )
        else:
            await bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=text,
                reply_markup=keyboard
            )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard
        )

async def send_payment_reminders(context: ContextTypes.DEFAULT_TYPE):
    rows = sheet.get_all_values()
    reminders = {}

    for row in get_unpaid_rows_due_for_reminder(rows):
        try:
            chat_id = int(get_cell(row, APPROVER_CHAT_ID_COL))
        except ValueError:
            logging.warning("Skipping reminder for request %s: invalid chat id", get_cell(row, REQUEST_ID_COL))
            continue

        payer_tag = get_cell(row, PAYER_TAG_COL)
        reminders.setdefault((chat_id, payer_tag), []).append(row)

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

            for row in unpaid_rows:
                await send_approved_invoice(context.bot, chat_id, row)
        except Exception:
            logging.exception("Failed to send payment reminder to chat %s", chat_id)

def get_project_settings(project_name):
    rows = projects_sheet.get_all_values()

    for row in rows[1:]:  # пропускаем заголовок
        if row[0].strip().lower() == project_name.strip().lower():
            return {
                "approver_chat_id": int(row[1]),
                "payer_tag": row[2].strip() if len(row) > 2 else ""
            }

    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text("Привет! Напиши /new чтобы отправить счет")

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
        elif update.message.photo:
            file_id = update.message.photo[-1].file_id

        rows = sheet.get_all_values()

        for i, row in enumerate(rows):
            if row[0] == request_id:

                payer_name = update.effective_user.username or update.effective_user.first_name
                approver_name = get_cell(row, APPROVER_NAME_COL, "неизвестно")

                sheet.update_cell(i+1, 8, STATUS_PAID)

                text = (
                    f"Счет #{request_id}\n\n"
                    f"{row[4]}\n\n"
                    f"{row[6]}\n\n"
                    f"Согласовано: @{approver_name}\n"
                    f"Оплачено: @{payer_name}\n\n"
                    f"💰 Счет оплачен"
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

                if update.message.photo:
                    await context.bot.send_photo(
                        chat_id=original_chat_id,
                        photo=file_id,
                        caption=f"Чек по счету #{request_id}"
                    )
                else:
                    await context.bot.send_document(
                        chat_id=original_chat_id,
                        document=file_id,
                        caption=f"Чек по счету #{request_id}"
                    )
                creator_chat_id = int(row[CREATOR_CHAT_ID_COL])

                if update.message.photo:
                    await context.bot.send_photo(
                        chat_id=creator_chat_id,
                        photo=file_id,
                        caption=(
                            f"💰 Счет #{request_id} оплачен\n\n"
                            f"Подтвердите получение оплаты"
                        )
                    )
                else:
                    await context.bot.send_document(
                        chat_id=creator_chat_id,
                        document=file_id,
                        caption=(
                            f"💰 Счет #{request_id} оплачен\n\n"
                            f"Подтвердите получение оплаты"
                        )
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

                sheet.update_cell(i+1, 8, "Отклонен")

                creator_chat_id = int(row[10])

                comment = text

                await context.bot.send_message(
                    chat_id=creator_chat_id,
                    text=f"❌ Ваш счет #{request_id} не согласован\n\n"
                         f"Причина: {comment}\n\n"
                         f"Просьба отправить счет заново с учетом комментария"
                )
                break

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Счет #{request_id} отклонен и комментарий отправлен"
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

        await update.message.reply_text("Кому платим? (Имя Фамилия, компания, сервис)")
        return

    # ЭТАП 2 — КОМУ ПЛАТИМ
    if "target" not in state:
        state["target"] = text

        await update.message.reply_text("Введите сумму:")
        return     
         
    # ЭТАП 3 — СУММА
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

    # ЭТАП 4 — КОММЕНТАРИЙ
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
        state.get("payer_tag", "")
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

                await send_approved_invoice(
                    context.bot,
                    int(get_cell(row, APPROVER_CHAT_ID_COL)),
                    row
                )
            elif action == "reject":                   
                msg = await query.message.reply_text("Введите причину отклонения:")

                reject_state[query.from_user.id] = {
                    "request_id": request_id,
                    "message_id": query.message.message_id,
                    "chat_id": query.message.chat_id,
                    "ask_message_id": msg.message_id  # 👈 ВАЖНО
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
    else:
        text = action

    if action not in ["approve", "paid"]:  # approve уже удаляет сообщение
        await query.edit_message_text(f"Счет {request_id}\n{text}")
         
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_web():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()

def main():
    Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

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
