import logging
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

import os
TOKEN = os.getenv("BOT_TOKEN")

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

def get_approver_chat_id(project_name):
    rows = projects_sheet.get_all_values()

    for row in rows[1:]:  # пропускаем заголовок
        if row[0].strip().lower() == project_name.strip().lower():
            return int(row[1])

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
    if update.effective_chat.type != "private":
        return
    chat_id = update.effective_chat.id

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

        await update.message.reply_text("Введите комментарий:")

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
        request_id = reject_state.pop(update.effective_user.id)

        rows = sheet.get_all_values()

        for i, row in enumerate(rows):
            if row[0] == request_id:

                sheet.update_cell(i+1, 7, "Отклонен")

                creator_chat_id = int(row[9])

                comment = text

                await context.bot.send_message(
                    chat_id=creator_chat_id,
                    text=f"❌ Ваш счет #{request_id} не согласован\n\n"
                         f"Причина: {comment}\n\n"
                         f"Просьба отправить счет заново с учетом комментария"
                )
                break

        await update.message.reply_text("Счет отклонен и комментарий отправлен")
        return
    if chat_id not in user_state:
        await update.message.reply_text(
            "Напиши /new чтобы отправить счет"
        )
        return

    state = user_state[chat_id]

    # ЭТАП 1 — ПРОЕКТ
    if "project" not in state:
        approver_id = get_approver_chat_id(text)

        if not approver_id:
            await update.message.reply_text(
                "❌ Для этого проекта не найден согласующий\n"
                "Пожалуйста, введите аббревиатуру проекта снова:"
            )
            return

        state["project"] = text
        state["approver_id"] = approver_id

        await update.message.reply_text("Введите сумму или реквизиты:")
        return

    # ЭТАП 2 — СУММА
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

    # ЭТАП 3 — КОММЕНТАРИЙ
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
        state["amount"],
        state["comment"],
        "На согласовании",
        state["approver_id"],
        state.get("file_id", ""),
        update.effective_user.username or update.effective_user.first_name  # 👈 НОВОЕ (кто создал)
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
    await context.bot.send_message(
        chat_id=state["approver_id"],
        text = (
            f"Новый счет #{request_id}\n"
            f"Проект: {state['project']}\n"
            f"Сумма: {state['amount']}\n"
            f"Комментарий: {state['comment']}"
        )

        # если есть файл → отправляем его СРАЗУ с кнопками
        if state.get("file_id"):
            await context.bot.send_document(
                chat_id=state["approver_id"],
                document=state["file_id"],
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

        await query.message.reply_text("Введите комментарий:")
        await query.answer()
        return

    # обычные кнопки
    action, request_id = data.split("_")

    rows = sheet.get_all_values()

    for i, row in enumerate(rows):
        if row[0] == request_id:

            if action == "paid":
                sheet.update_cell(i+1, 7, "Оплачено")

                payer_name = query.from_user.username or query.from_user.first_name

                approver_name = row[10] if len(row) > 10 else "неизвестно"
                # можно хранить в будущем, но пока просто текст

                await query.edit_message_text(
                    f"Счет #{request_id}\n\n"
                    f"Проект: {row[3]}\n"
                    f"Сумма: {row[4]}\n"
                    f"Комментарий: {row[5]}\n\n"
                    f"Согласовано: {approver_name}\n"
                    f"Оплачено: @{payer_name}\n\n"
                    f"💰 Счет оплачен"
                )

            elif action == "approve":
                sheet.update_cell(i+1, 7, "Согласован")

                approver_name = query.from_user.username or query.from_user.first_name
                sheet.update_cell(i+1, 11, approver_name)
                # ❗ удаляем сообщение с кнопками
                await query.message.delete()

                keyboard = [
                    [
                        InlineKeyboardButton("💰 Оплатил", callback_data=f"paid_{request_id}")
                    ]
                ]

                await context.bot.send_message(
                    chat_id=row[7],  # 👈 берем из таблицы
                    text = (
                        f"Счет #{request_id} одобрен\n\n"
                        f"Проект: {row[3]}\n"
                        f"Сумма: {row[4]}\n"
                        f"Комментарий: {row[5]}\n\n"
                        f"Согласовано: @{approver_name}"
                    )

                    if len(row) > 8 and row[8]:
                        await context.bot.send_document(
                            chat_id=int(row[7]),
                            document=row[8],
                            caption=text,
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=row[7],
                            text=text,
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
            elif action == "reject":
                reject_state[query.from_user.id] = request_id

                await query.message.reply_text("Введите причину отклонения:")
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

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
