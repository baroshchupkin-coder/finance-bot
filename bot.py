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

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    chat_id = update.effective_chat.id
    text = update.message.text

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
        state.get("file_id", "")
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
        text=f"Новый счет #{request_id}\n"
             f"Проект: {state['project']}\n"
             f"Сумма: {state['amount']}\n"
             f"Комментарий: {state['comment']}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    # ===== ВАЖНО: ФАЙЛ =====
    if state.get("file_id"):
        await context.bot.send_document(
            chat_id=state["approver_id"],
            document=state["file_id"],
            caption="📎 Прикреплённый файл"
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
                # 👉 ОТПРАВКА ФАЙЛА ОПЛАТЧИКУ
                if len(row) > 8 and row[8]:
                    await context.bot.send_document(
                        chat_id=PAYMENT_CHAT_ID,
                        document=row[8],
                        caption=f"📎 Счет #{request_id}"
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
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(CallbackQueryHandler(button))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
