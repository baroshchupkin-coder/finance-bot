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

PAYMENT_CHAT_ID = 5293695558  # сюда id оплатчика

logging.basicConfig(level=logging.INFO)

user_state = {}

def get_approver_chat_id(project_name):
    rows = projects_sheet.get_all_values()

    for row in rows[1:]:  # пропускаем заголовок
        if row[0].strip().lower() == project_name.strip().lower():
            return int(row[1])

    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Напиши /new чтобы отправить счет")

async def new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state[update.effective_chat.id] = {}
    await update.message.reply_text("Введите проект:")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    if chat_id not in user_state:
        return

    state = user_state[chat_id]

    # ЭТАП 1 — ПРОЕКТ
    if "project" not in state:
        approver_id = get_approver_chat_id(text)

        if not approver_id:
            await update.message.reply_text(
                "❌ Для этого проекта не найден согласующий\n"
                "Пожалуйста, введите проект снова:"
            )
            return

        state["project"] = text
        state["approver_id"] = approver_id

        await update.message.reply_text("Введите сумму или реквизиты:")
        return

    # ЭТАП 2 — СУММА
    if "amount" not in state:
        state["amount"] = text
        await update.message.reply_text("Введите комментарий:")
        return

    # ЭТАП 3 — КОММЕНТ
    if "comment" not in state:
        state["comment"] = text

        row = [
            str(len(sheet.get_all_values())),
            str(update.message.date),
            update.effective_user.username,
            state["project"],
            state["amount"],
            state["comment"],
            "На согласовании",
            state["approver_id"],
            ""
        ]

        sheet.append_row(row)

        await update.message.reply_text(
            "Счёт принят! Ответственный получил уведомление.\n\n"
            "Напиши /new чтобы отправить новый счёт"
        )

        request_id = row[0]

        keyboard = [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{request_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{request_id}")
            ]
        ]

        await context.bot.send_message(
            chat_id=state["approver_id"],
            text=f"Новый счет #{request_id}\n"
                 f"Проект: {state['project']}\n"
                 f"Сумма: {state['amount']}\n"
                 f"Комментарий: {state['comment']}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        user_state.pop(chat_id)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, request_id = query.data.split("_")

    rows = sheet.get_all_values()

    for i, row in enumerate(rows):
        if row[0] == request_id:

            if action == "paid":
                sheet.update_cell(i+1, 7, "Оплачено")

            elif action == "approve":
                sheet.update_cell(i+1, 7, "Согласован")

                keyboard = [
                    [
                        InlineKeyboardButton("💰 Оплатил", callback_data=f"paid_{request_id}")
                    ]
                ]

                await context.bot.send_message(
                    chat_id=PAYMENT_CHAT_ID,
                    text=f"Счет #{request_id} одобрен\n\n"
                         f"Проект: {row[3]}\n"
                         f"Сумма: {row[4]}\n"
                         f"Комментарий: {row[5]}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            else:
                sheet.update_cell(i+1, 7, "Отклонен")

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
    app.add_handler(CallbackQueryHandler(button))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
