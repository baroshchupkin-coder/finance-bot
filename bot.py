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

logging.basicConfig(level=logging.INFO)

user_state = {}

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

    if "project" not in state:
        state["project"] = text
        await update.message.reply_text("Введите сумму или реквизиты:")
        return

    if "amount" not in state:
        state["amount"] = text
        await update.message.reply_text("Введите комментарий:")
        return

    if "comment" not in state:
        state["comment"] = text

        # запись в таблицу
        row = [
            str(len(sheet.get_all_values())),
            str(update.message.date),
            update.effective_user.username,
            state["project"],
            state["amount"],
            state["comment"],
            "На согласовании",
            "5293695558",
            ""
        ]

        sheet.append_row(row)
        await update.message.reply_text(
        "Счёт принят! Ответственный получил уведомление.\n\n"
        "Напиши /new чтобы отправить новый счёт")

        request_id = row[0]

        keyboard = [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{request_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{request_id}")
            ]
        ]

        await context.bot.send_message(
            chat_id=5293695558,
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
            if action == "approve":
                sheet.update_cell(i+1, 7, "Согласован")
            else:
                sheet.update_cell(i+1, 7, "Отклонен")

            break

    await query.edit_message_text(f"Счет {request_id} обработан: {action}")

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
