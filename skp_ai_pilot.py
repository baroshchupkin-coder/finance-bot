"""Isolated Telegram pilot. Never run it with the production bot token."""

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from shared_dds import SHARED_DDS_CHAT_IDS, SHARED_DDS_OFFICE_CHAT_ID
from skp_ai import DEFAULT_MODEL, QUESTIONS, Entry, request_decision, sanitize_text
from skp_ai_sheet import PILOT_SPREADSHEET_ID, PilotWriter, read_catalog


LOG = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Novosibirsk")
CALLS_PER_MONTH = 300
MAX_ATTEMPTS = 4


class Queue:
    def __init__(self, path):
        if not Path(path).is_absolute():
            raise ValueError("SKP_AI_STATE_PATH must be absolute and on persistent storage")
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, username TEXT NOT NULL,
                date TEXT NOT NULL, body TEXT NOT NULL, replies TEXT NOT NULL DEFAULT '[]',
                parent_text TEXT NOT NULL DEFAULT '', entries TEXT, state TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, question_id INTEGER, revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS calls (
                month TEXT PRIMARY KEY, count INTEGER NOT NULL
            );
        """)

    def enqueue(self, message_id, owner, username, day, body, parent_text=""):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO events(id,owner,username,date,body,parent_text) VALUES (?,?,?,?,?,?)",
                (message_id, owner, username, day.isoformat(), body, parent_text),
            )

    def get(self, message_id):
        return self.db.execute("SELECT * FROM events WHERE id=?", (message_id,)).fetchone()

    def answer(self, reply_to, user_id, body):
        row = self.db.execute(
            "SELECT * FROM events WHERE question_id=? AND owner=? AND state='waiting'",
            (reply_to, user_id),
        ).fetchone()
        if row is None:
            return False
        replies = json.loads(row["replies"]) + [body]
        with self.db:
            self.db.execute(
                """UPDATE events SET replies=?, state='queued', attempts=0, next_try=0,
                   revision=revision+1 WHERE id=?""", (json.dumps(replies, ensure_ascii=False), row["id"]),
            )
        return True

    def edit(self, message_id, body):
        row = self.get(message_id)
        if row is None:
            return False
        if row["state"] in {"written", "already_present", "writing", "review"}:
            self.finish(message_id, "review", clear=True)
            return True
        if row["body"] != body:
            with self.db:
                self.db.execute(
                    """UPDATE events SET body=?, replies='[]', state='queued', attempts=0,
                       next_try=0, question_id=NULL, revision=revision+1 WHERE id=?""", (body, message_id),
                )
        return False

    def next(self):
        return self.db.execute(
            "SELECT * FROM events WHERE state IN ('queued','writing') AND next_try<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()

    def claim_call(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO calls VALUES (?,0)", (month,))
            result = self.db.execute("UPDATE calls SET count=count+1 WHERE month=? AND count<?",
                                     (month, CALLS_PER_MONTH))
        return result.rowcount == 1

    def finish(self, message_id, state, *, clear=False, question_id=None):
        with self.db:
            self.db.execute(
                """UPDATE events SET state=?, question_id=?, body=CASE WHEN ? THEN '' ELSE body END,
                   replies=CASE WHEN ? THEN '[]' ELSE replies END, parent_text=CASE WHEN ? THEN '' ELSE parent_text END,
                   entries=CASE WHEN ? THEN NULL ELSE entries END, revision=revision+1 WHERE id=?""",
                (state, question_id, clear, clear, clear, clear, message_id),
            )

    def stage_write(self, message_id, entries):
        encoded = [{**asdict(e), "date": e.date.isoformat(), "amount": str(e.amount)} for e in entries]
        with self.db:
            self.db.execute(
                """UPDATE events SET state='writing', entries=?, attempts=0, next_try=0,
                   revision=revision+1 WHERE id=?""", (json.dumps(encoded, ensure_ascii=False), message_id),
            )

    def retry(self, row):
        attempts = row["attempts"] + 1
        if attempts >= MAX_ATTEMPTS:
            self.finish(row["id"], "review")
            return False
        with self.db:
            self.db.execute("UPDATE events SET attempts=?,next_try=? WHERE id=?",
                            (attempts, time.time() + 30 * 2 ** attempts, row["id"]))
        return True

    def defer(self, row):
        with self.db:
            self.db.execute("UPDATE events SET next_try=? WHERE id=?", (time.time() + 3600, row["id"]))


class Pilot:
    def __init__(self, queue, writer, api_key, model, chat_id, source_chat):
        self.queue, self.writer = queue, writer
        self.api_key, self.model = api_key, model
        self.chat_id, self.source_chat = chat_id, source_chat
        self.lock = asyncio.Lock()
        self.catalog = None
        self.catalog_until = 0

    async def handle(self, update, context):
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if not message or not user or user.is_bot or not chat or chat.id != self.chat_id:
            return
        body = sanitize_text(message.text or message.caption or "")
        if update.edited_message:
            if self.queue.edit(message.message_id, body):
                await message.reply_text("Сообщение изменено после обработки. Сверьте строки в тестовой ДДС.")
            return
        if not body.strip() or body.startswith("/"):
            return
        parent = message.reply_to_message
        if parent and parent.from_user and parent.from_user.id == context.bot.id:
            self.queue.answer(parent.message_id, user.id, body)
            return
        parent_text = sanitize_text(parent.text or parent.caption)[:1000] if parent else ""
        self.queue.enqueue(message.message_id, user.id, user.username or "",
                           message.date.astimezone(TZ).date(), body, parent_text)

    async def write_staged(self, message_id, entries):
        status, rows = await asyncio.to_thread(self.writer.write, self.chat_id, message_id, entries)
        if self.queue.get(message_id)["state"] == "writing":
            self.queue.finish(message_id, status, clear=status != "review")
        LOG.info("SKP pilot %s message=%s rows=%s", status, message_id, rows)

    async def process(self, context):
        if self.lock.locked():
            return
        async with self.lock:
            row = self.queue.next()
            if row is None:
                return
            message_id = row["id"]
            try:
                if row["state"] == "writing":
                    entries = [
                        Entry(**{**e, "date": date.fromisoformat(e["date"]), "amount": Decimal(e["amount"])})
                        for e in json.loads(row["entries"])
                    ]
                    await self.write_staged(message_id, entries)
                    return
                if await asyncio.to_thread(self.writer.seen, self.chat_id, message_id):
                    current = self.queue.get(message_id)
                    if current["revision"] == row["revision"]:
                        self.queue.finish(message_id, "already_present", clear=True)
                    return
                if self.catalog is None or time.monotonic() > self.catalog_until:
                    self.catalog = await asyncio.to_thread(read_catalog, self.writer.book)
                    self.catalog_until = time.monotonic() + 300
                if not self.queue.claim_call():
                    self.queue.defer(row)
                    return
                decision, usage = await asyncio.to_thread(
                    request_decision, self.api_key, self.model, self.catalog, row["body"],
                    datetime.fromisoformat(row["date"]).date(), self.source_chat, row["username"],
                    json.loads(row["replies"]), parent_text=row["parent_text"],
                )
                LOG.info("SKP pilot %s message=%s input_tokens=%s output_tokens=%s",
                         decision.action, message_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
                if self.queue.get(message_id)["revision"] != row["revision"]:
                    return
                if decision.action == "skip":
                    self.queue.finish(message_id, "skipped", clear=True)
                elif decision.action == "clarify":
                    if len(json.loads(row["replies"])) >= 3:
                        self.queue.finish(message_id, "review", clear=True)
                        return
                    sent = await context.bot.send_message(
                        self.chat_id, QUESTIONS[decision.reason], reply_to_message_id=message_id,
                    )
                    if self.queue.get(message_id)["revision"] == row["revision"]:
                        self.queue.finish(message_id, "waiting", question_id=sent.message_id)
                else:
                    self.queue.stage_write(message_id, decision.entries)
                    await self.write_staged(message_id, decision.entries)
            except Exception as exc:
                LOG.warning("SKP pilot message=%s error=%s", message_id, type(exc).__name__)
                current = self.queue.get(message_id)
                same_job = current["revision"] == row["revision"] or current["state"] == "writing"
                if same_job and current["state"] in {"queued", "writing"} and not self.queue.retry(current):
                    try:
                        await context.bot.send_message(
                            self.chat_id, "Не удалось обработать операцию. Нужна ручная сверка в тестовой ДДС.",
                            reply_to_message_id=message_id,
                        )
                    except Exception:
                        LOG.warning("Could not notify pilot chat about message=%s", message_id)


def main():
    import gspread
    from telegram.ext import ApplicationBuilder, MessageHandler, filters

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ["SKP_AI_TEST_BOT_TOKEN"]
    if token in {os.getenv("BOT_TOKEN"), os.getenv("NEW_BOT_TOKEN")}:
        raise ValueError("Use a separate test Telegram bot")
    chat_id = int(os.environ["SKP_AI_TEST_CHAT_ID"])
    if (not str(chat_id).startswith("-100") or chat_id in SHARED_DDS_CHAT_IDS
            or chat_id in {-1003964698486, -1003806940668}):
        raise ValueError("Use an isolated test group")
    source_chat = int(os.getenv("SKP_AI_PROFILE_CHAT_ID", "-1003721730980"))
    if source_chat not in SHARED_DDS_CHAT_IDS:
        raise ValueError("Unknown SKP source profile")
    client = gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_CREDENTIALS"]))
    writer = PilotWriter(client.open_by_key(PILOT_SPREADSHEET_ID))
    pilot = Pilot(Queue(os.environ["SKP_AI_STATE_PATH"]), writer, os.environ["OPENAI_API_KEY"],
                  os.getenv("OPENAI_MODEL", DEFAULT_MODEL), chat_id, source_chat)
    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL, pilot.handle))
    app.job_queue.run_repeating(pilot.process, interval=5, first=2, name="skp-ai-pilot")
    app.run_polling(allowed_updates=["message", "edited_message"])


if __name__ == "__main__":
    main()
