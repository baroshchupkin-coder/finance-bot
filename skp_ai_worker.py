"""Sol conversation worker for the SKP DDS chats."""

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

from skp_ai import (
    QUESTIONS, Entry, memory_phrase,
    request_decision, sanitize_text,
)
from skp_ai_sheet import read_catalog


LOG = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Novosibirsk")
CALLS_PER_MONTH = 300
MAX_ATTEMPTS = 4
FOLLOWUP_WINDOW = 15 * 60
MEMORY_REASONS = {"wallet", "amount", "currency", "split", "exchange_amounts"}
REVIEW_NOTICES = {
    "amount": "Не записано в ДДС: модель вернула сумму, которую нельзя сохранить числом.",
    "wallet": "Не записано в ДДС: кошелёк не найден в справочнике или его валюта не совпала.",
    "currency": "Не записано в ДДС: валюта не поддерживается.",
    "pair": "Не записано в ДДС: не сходятся две стороны перевода или обмена.",
    "date": "Не записано в ДДС: дата операции не прошла проверку.",
    "context_budget": "Не записано в ДДС: исчерпан лимит запросов тестового бота.",
}


def encode_entries(entries):
    rows = [{**asdict(e), "date": e.date.isoformat(), "amount": str(e.amount)} for e in entries]
    return json.dumps(rows, ensure_ascii=False)


def preview_entries(entries, limit=1250):
    lines, remaining = [], limit
    for entry in entries:
        amount = f"{abs(entry.amount):,f}".rstrip("0").rstrip(".") if entry.amount % 1 else f"{abs(entry.amount):,.0f}"
        amount = amount.replace(",", " ").replace(".", ",")
        sign = "−" if entry.amount < 0 else "+"
        unit = "сом" if entry.currency == "KGS" else entry.currency
        details = ", ".join(part for part in (entry.wallet, entry.purpose[:100]) if part)
        line = f"{sign}{amount} {unit} — {details}"
        if len(line) + 1 > remaining:
            lines.append(f"Ещё проводок: {len(entries) - len(lines)}")
            break
        lines.append(line)
        remaining -= len(line) + 1
    return "\n".join(lines)


def confirmation_text(entries, completed_count=0, question=""):
    sections = ["Пока в ДДС ничего не записано."]
    if completed_count:
        sections.append("Уже проведено по сообщению:\n" + preview_entries(entries[:completed_count]))
    if entries[completed_count:]:
        sections.append("Нужно подтвердить:\n" + preview_entries(entries[completed_count:]))
    sections.append(question[:1000] or "Эти операции уже провели? Если не все, укажите какие.")
    return "\n\n".join(sections)


class Queue:
    def __init__(self, path, *, check_same_thread=True):
        if path != ":memory:" and not Path(path).is_absolute():
            raise ValueError("SKP_AI_STATE_PATH must be absolute and on persistent storage")
        self.db = sqlite3.connect(path, check_same_thread=check_same_thread)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, username TEXT NOT NULL,
                date TEXT NOT NULL, body TEXT NOT NULL, replies TEXT NOT NULL DEFAULT '[]',
                parent_text TEXT NOT NULL DEFAULT '', entries TEXT, state TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, question_id INTEGER,
                ack_message_id INTEGER, classification_review_ready INTEGER NOT NULL DEFAULT 0,
                question_reason TEXT NOT NULL DEFAULT '', question_at REAL NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS calls (
                month TEXT PRIMARY KEY, count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clarification_memory (
                event_id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, facts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS clarification_memory_owner
                ON clarification_memory(owner, event_id);
        """)
        names = {row["name"] for row in self.db.execute("PRAGMA table_info(events)")}
        if "ack_message_id" not in names:
            with self.db:
                self.db.execute("ALTER TABLE events ADD COLUMN ack_message_id INTEGER")
        for name, definition in (
            ("classification_review_ready", "INTEGER NOT NULL DEFAULT 0"),
            ("question_reason", "TEXT NOT NULL DEFAULT ''"),
            ("question_at", "REAL NOT NULL DEFAULT 0"),
            ("reply_to_id", "INTEGER"),
            ("question_text", "TEXT NOT NULL DEFAULT ''"),
            ("completed_count", "INTEGER NOT NULL DEFAULT 0"),
            ("dialogue", "TEXT NOT NULL DEFAULT '[]'"),
            ("question_reply_id", "INTEGER"),
            ("write_note", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in names:
                with self.db:
                    self.db.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")

    def enqueue(self, message_id, owner, username, day, body, parent_text="", reply_to_id=None):
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO events(id,owner,username,date,body,parent_text,reply_to_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (message_id, owner, username, day.isoformat(), body, parent_text, reply_to_id),
            )

    def get(self, message_id):
        return self.db.execute("SELECT * FROM events WHERE id=?", (message_id,)).fetchone()

    def pending_for(self, row):
        candidates = self.db.execute(
            """SELECT * FROM events WHERE owner=? AND state IN ('waiting','confirming')
               AND question_id<? AND ((? IS NOT NULL AND (question_id=? OR EXISTS (
                   SELECT 1 FROM json_each(dialogue) WHERE json_extract(value, '$.question_id')=?
               ))) OR (? IS NULL AND
               question_at>=?)) LIMIT 2""",
            (row["owner"], row["id"], row["reply_to_id"], row["reply_to_id"],
             row["reply_to_id"], row["reply_to_id"], time.time() - FOLLOWUP_WINDOW),
        ).fetchall()
        return candidates[0] if len(candidates) == 1 else None

    def remember_clarification(self, row, entries):
        replies = json.loads(row["replies"])
        phrase = memory_phrase(row["body"])
        if not replies or row["question_reason"] not in MEMORY_REASONS or len(phrase) < 8:
            return
        facts = {
            "message": phrase,
            "question": memory_phrase(row["question_text"]),
            "reply": memory_phrase(replies[-1]),
            "reason": row["question_reason"],
            "accounts": [
                {"side": "credit" if e.amount > 0 else "debit", "wallet": e.wallet, "currency": e.currency}
                for e in entries
            ],
        }
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO clarification_memory VALUES (?,?,?)",
                            (row["id"], row["owner"], json.dumps(facts, ensure_ascii=False)))

    def recall_clarifications(self, owner, text, *, exclude_id):
        phrase = memory_phrase(text)
        if len(phrase) < 8:
            return ()
        matches = []
        for row in self.db.execute(
            """SELECT facts FROM clarification_memory WHERE owner=? AND event_id<>?
               ORDER BY event_id DESC LIMIT 80""", (owner, exclude_id),
        ):
            facts = json.loads(row["facts"])
            score = SequenceMatcher(None, phrase, facts["message"]).ratio()
            if score >= 0.72 and facts not in [item for _, item in matches]:
                matches.append((score, facts))
        return tuple(item for _, item in sorted(matches, key=lambda match: match[0], reverse=True)[:4])

    def _mark_answered(self, row):
        self.db.execute(
            """UPDATE events SET state='answered', body='', parent_text='', revision=revision+1
               WHERE id=? AND revision=?""", (row["id"], row["revision"]),
        )

    def resolve_followup(self, pending, answer, decision):
        state = {"write": "writing", "skip": "skipped"}[decision.action]
        with self.db:
            changed = self.db.execute(
                """UPDATE events SET state=?, entries=?, replies=?, attempts=0, next_try=0, question_id=NULL,
                   ack_message_id=?, write_note=?,
                   revision=revision+1 WHERE id=? AND revision=?""",
                (state, encode_entries(decision.entries) if state == "writing" else None,
                 json.dumps([*json.loads(pending["replies"]), answer["body"]], ensure_ascii=False),
                 answer["id"], decision.write_note, pending["id"], pending["revision"]),
            ).rowcount
            if changed:
                self._mark_answered(answer)
        return bool(changed)

    def edit(self, message_id, body):
        row = self.get(message_id)
        if row is None:
            return False
        if row["state"] in {"written", "already_present", "writing", "review"}:
            with self.db:
                self.db.execute("DELETE FROM clarification_memory WHERE event_id=?", (message_id,))
            self.finish(message_id, "review", clear=True)
            return True
        if row["body"] != body:
            with self.db:
                self.db.execute(
                    """UPDATE events SET body=?, replies='[]', entries=NULL, state='queued', attempts=0,
                       next_try=0, question_id=NULL, ack_message_id=NULL, dialogue='[]', write_note='',
                       revision=revision+1 WHERE id=?""", (body, message_id),
                )
        return False

    def next(self):
        return self.db.execute(
            "SELECT * FROM events WHERE state IN ('queued','writing','asking') AND next_try<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()

    def claim_call(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO calls VALUES (?,0)", (month,))
            result = self.db.execute("UPDATE calls SET count=count+1 WHERE month=? AND count<?",
                                     (month, CALLS_PER_MONTH))
        return result.rowcount == 1

    def finish(self, message_id, state, *, clear=False, question_id=None, history_eligible=False,
               question_reason="", question_text=""):
        with self.db:
            self.db.execute(
                """UPDATE events SET state=?, question_id=?, question_reason=?, question_at=?, question_text=?,
                   body=CASE WHEN ? THEN '' ELSE body END,
                   replies=CASE WHEN ? THEN '[]' ELSE replies END, parent_text=CASE WHEN ? THEN '' ELSE parent_text END,
                   entries=CASE WHEN ? THEN NULL ELSE entries END,
                   dialogue=CASE WHEN ? THEN '[]' ELSE dialogue END,
                   write_note=CASE WHEN ? THEN '' ELSE write_note END,
                   classification_review_ready=MAX(classification_review_ready, ?),
                   revision=revision+1 WHERE id=?""",
                (state, question_id, question_reason, time.time() if question_id is not None else 0, question_text,
                 clear, clear, clear, clear, clear, clear, history_eligible, message_id),
            )

    def history_eligible_ids(self):
        return {
            row["id"] for row in self.db.execute(
                """SELECT id FROM events WHERE classification_review_ready=1
                   AND state IN ('written','already_present')""",
            )
        }

    def stage_write(self, message_id, entries, write_note=""):
        with self.db:
            self.db.execute(
                """UPDATE events SET state='writing', entries=?, write_note=?, attempts=0, next_try=0,
                   revision=revision+1 WHERE id=?""", (encode_entries(entries), write_note, message_id),
            )

    def stage_question(self, row, decision, question, pending=None):
        origin = pending if pending is not None else row
        replies = json.loads(origin["replies"])
        dialogue = json.loads(origin["dialogue"])
        if pending is not None:
            replies.append(row["body"])
            dialogue.append({"question_id": pending["question_id"], "question": pending["question_text"],
                             "answer": row["body"]})
        reason = "draft" if decision.action == "confirm" else decision.reason
        keep_existing = pending is not None and not decision.entries
        entries = origin["entries"] if keep_existing else encode_entries(decision.entries)
        completed_count = origin["completed_count"] if keep_existing else decision.completed_count
        with self.db:
            changed = self.db.execute(
                """UPDATE events SET state='asking', question_id=NULL, entries=?, question_text=?, question_reason=?,
                   completed_count=?, question_reply_id=?, replies=?, dialogue=?, attempts=0, next_try=0,
                   revision=revision+1 WHERE id=? AND revision=?""",
                (entries, question, reason, completed_count, row["id"],
                 json.dumps(replies, ensure_ascii=False), json.dumps(dialogue, ensure_ascii=False),
                 origin["id"], origin["revision"]),
            ).rowcount
            if changed and pending is not None:
                self._mark_answered(row)
        return origin["id"] if changed else None

    def question_delivered(self, row, question_id):
        state = "confirming" if row["question_reason"] == "draft" else "waiting"
        with self.db:
            self.db.execute(
                """UPDATE events SET state=?, question_id=?, question_at=?, attempts=0, next_try=0,
                   revision=revision+1 WHERE id=? AND revision=?""",
                (state, question_id, time.time(), row["id"], row["revision"]),
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


class SolWorker:
    def __init__(self, queue, writer, api_key, model, chat_id, source_chat):
        self.queue, self.writer = queue, writer
        self.api_key, self.model = api_key, model
        self.chat_id, self.source_chat = chat_id, source_chat
        self.lock = asyncio.Lock()
        self.catalog = None
        self.catalog_until = 0

    async def _queue(self, method, *args, **kwargs):
        if hasattr(self.queue, "call"):
            return await asyncio.to_thread(self.queue.call, method, *args, **kwargs)
        return getattr(self.queue, method)(*args, **kwargs)

    async def handle(self, update, context):
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if not message or not chat or chat.id != self.chat_id:
            return
        sender_chat = getattr(message, "sender_chat", None)
        if not sender_chat and (not user or user.is_bot):
            return
        sender = sender_chat or user
        body = sanitize_text(message.text or message.caption or "")
        if update.edited_message:
            if await self._queue("edit", message.message_id, body):
                await message.reply_text("Сообщение изменено после обработки. Сверьте строки в тестовой ДДС.")
            return
        if not body.strip() or body.startswith("/"):
            return
        parent = message.reply_to_message
        message_day = message.date.astimezone(TZ).date()
        reply_to_id = (parent.message_id if parent and parent.from_user and parent.from_user.id == context.bot.id
                       else None)
        parent_text = sanitize_text(parent.text or parent.caption)[:1000] if parent else ""
        await self._queue("enqueue", message.message_id, sender.id, sender.username or "", message_day, body, parent_text,
                           reply_to_id=reply_to_id)

    async def acknowledge_write(self, bot, message_id, write_note=""):
        if not write_note:
            try:
                # PTB 20.3 predates reactions; reuse its authenticated Bot API transport.
                result = await bot._post("setMessageReaction", {
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": "\N{THUMBS UP SIGN}"}],
                })
                if result is True:
                    return
            except Exception as error:
                LOG.info("SKP pilot reaction unavailable message=%s error=%s", message_id, type(error).__name__)
        message = "Записано в ДДС."
        if write_note:
            message += "\n" + write_note
        try:
            await bot.send_message(self.chat_id, message, reply_to_message_id=message_id)
        except Exception as error:
            LOG.warning("SKP pilot acknowledgement failed message=%s error=%s", message_id, type(error).__name__)

    async def write_staged(self, message_id, entries, bot):
        status, rows = await asyncio.to_thread(self.writer.write, self.chat_id, message_id, entries)
        staged = await self._queue("get", message_id)
        if staged["state"] == "writing":
            success = status in {"written", "already_present"}
            staged_entries = json.loads(staged["entries"])
            new_review_policy = all("review_category" in entry for entry in staged_entries)
            if success:
                await self._queue("remember_clarification", staged, entries)
            await self._queue("finish", message_id, status, clear=status != "review",
                              history_eligible=success and new_review_policy)
            if status in {"written", "already_present"}:
                await self.acknowledge_write(bot, staged["ack_message_id"] or message_id, staged["write_note"])
            elif status == "review":
                await self.notify_review(bot, staged["ack_message_id"] or message_id, "existing_rows")
        LOG.info("SKP pilot %s message=%s rows=%s", status, message_id, rows)

    async def notify_review(self, bot, message_id, reason):
        LOG.info("SKP pilot needs review message=%s reason=%s", message_id, reason)
        notice = REVIEW_NOTICES.get(reason, "Не записано в ДДС. Нужна ручная сверка.")
        try:
            await bot.send_message(self.chat_id, notice, reply_to_message_id=message_id)
        except Exception as error:
            LOG.warning("SKP pilot review notice failed message=%s error=%s", message_id, type(error).__name__)

    async def deliver_question(self, row, bot):
        sent = await bot.send_message(
            self.chat_id, row["question_text"], reply_to_message_id=row["question_reply_id"],
        )
        await self._queue("question_delivered", row, sent.message_id)

    async def ask(self, row, decision, bot, pending=None):
        if decision.action == "confirm":
            question = confirmation_text(decision.entries, decision.completed_count, decision.question)
        else:
            question = (decision.question or QUESTIONS.get(decision.reason, "Что нужно уточнить в операции?"))[:1000]
            if decision.entries:
                question = ("Пока в ДДС ничего не записано. Вот что удалось разобрать:\n"
                            + preview_entries(decision.entries) + "\n\n" + question)
        event_id = await self._queue("stage_question", row, decision, question, pending)
        if event_id is not None:
            await self.deliver_question(await self._queue("get", event_id), bot)

    async def process(self, context):
        if self.lock.locked():
            return
        async with self.lock:
            row = await self._queue("next")
            if row is None:
                return
            message_id = row["id"]
            pending = None
            try:
                if row["state"] == "writing":
                    entries = [
                        Entry(**{**e, "date": date.fromisoformat(e["date"]), "amount": Decimal(e["amount"])})
                        for e in json.loads(row["entries"])
                    ]
                    await self.write_staged(message_id, entries, context.bot)
                    return
                if row["state"] == "asking":
                    await self.deliver_question(row, context.bot)
                    return
                replies = json.loads(row["replies"])
                pending = await self._queue("pending_for", row)
                if self.catalog is None or time.monotonic() > self.catalog_until:
                    self.catalog = await asyncio.to_thread(read_catalog, self.writer.book)
                    self.catalog_until = time.monotonic() + 300
                operation_date = date.fromisoformat(row["date"])
                review_ids = await self._queue("history_eligible_ids")
                existing, examples = await asyncio.to_thread(
                    self.writer.inspect_message, self.chat_id, message_id, row["body"],
                    self.catalog, review_ids,
                )
                if existing:
                    if (await self._queue("get", message_id))["revision"] == row["revision"]:
                        await self._queue("finish", message_id, "already_present", clear=True)
                    return
                if not await self._queue("claim_call"):
                    await self._queue("defer", row)
                    return
                loop = asyncio.get_running_loop()
                pending_context = None
                if pending is not None:
                    is_draft = pending["state"] == "confirming"
                    staged_entries = json.loads(pending["entries"] or "[]")
                    completed_count = pending["completed_count"]
                    pending_context = {
                        "message": pending["body"], "message_date": pending["date"],
                        "replies": json.loads(pending["replies"]),
                        "dialogue": json.loads(pending["dialogue"]),
                        "reason": "draft" if is_draft else pending["question_reason"],
                        "question": pending["question_text"] or QUESTIONS.get(pending["question_reason"], ""),
                        "completed_entries": staged_entries[:completed_count],
                        "proposed_entries": staged_entries[completed_count:],
                        "direct_reply": row["reply_to_id"] is not None,
                    }
                memory = await self._queue("recall_clarifications", row["owner"], row["body"], exclude_id=message_id)

                async def reserve_search_call():
                    return await self._queue("claim_call")

                def search_history(query):
                    return self.writer.inspect_message(
                        self.chat_id, message_id, query, self.catalog, review_ids,
                    )[1]

                decision, usage = await asyncio.to_thread(
                    request_decision, self.api_key, self.model, self.catalog, row["body"],
                    operation_date, self.source_chat, row["username"],
                    replies, parent_text=row["parent_text"], reviewed_examples=examples, pending=pending_context,
                    confirmed_clarifications=memory,
                    search_history=search_history,
                    reserve_search_call=lambda: asyncio.run_coroutine_threadsafe(
                        reserve_search_call(), loop,
                        ).result(timeout=20),
                )
                LOG.info("SKP Sol %s chat=%s message=%s input_tokens=%s output_tokens=%s",
                         decision.action, self.chat_id, message_id,
                         usage.get("input_tokens", 0), usage.get("output_tokens", 0))
                if (await self._queue("get", message_id))["revision"] != row["revision"]:
                    return
                target = pending if decision.applies_to_pending else None
                if decision.action in {"confirm", "clarify"}:
                    await self.ask(row, decision, context.bot, target)
                    return
                if target is not None and decision.action in {"write", "skip"}:
                    if await self._queue("resolve_followup", target, row, decision):
                        if decision.action == "write":
                            await self.write_staged(target["id"], decision.entries, context.bot)
                        else:
                            await self._queue("finish", target["id"], "skipped", clear=True)
                    return
                if decision.action == "skip":
                    await self._queue("finish", message_id, "skipped", clear=True)
                elif decision.action == "review":
                    await self._queue("finish", message_id, "review", question_reason=decision.reason)
                    await self.notify_review(context.bot, row["ack_message_id"] or message_id, decision.reason)
                elif decision.action == "write":
                    await self._queue("stage_write", message_id, decision.entries, decision.write_note)
                    await self.write_staged(message_id, decision.entries, context.bot)
            except Exception as exc:
                LOG.warning("SKP pilot message=%s error=%s", message_id, type(exc).__name__)
                current = await self._queue("get", message_id)
                if pending is not None and current["state"] == "answered":
                    current = await self._queue("get", pending["id"])
                fixable = current["state"] in {"writing", "asking"}
                same_job = current["revision"] == row["revision"] or fixable
                if same_job and (fixable or current["state"] == "queued") and not await self._queue("retry", current):
                    try:
                        await context.bot.send_message(
                            self.chat_id, "Не удалось обработать операцию. Нужна ручная сверка в тестовой ДДС.",
                            reply_to_message_id=message_id,
                        )
                    except Exception:
                        LOG.warning("Could not notify pilot chat about message=%s", message_id)
