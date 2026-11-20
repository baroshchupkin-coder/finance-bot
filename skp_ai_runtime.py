"""Production transport for the tested Sol DDS worker."""

import asyncio
import base64
import json
import logging
import time
import zlib
from datetime import datetime, timezone
from threading import RLock
from urllib.parse import quote
from urllib.request import Request, urlopen

from gspread.exceptions import WorksheetNotFound

from shared_dds import SHARED_DDS_CHAT_ID, SHARED_DDS_CHAT_IDS, SHARED_DDS_OFFICE_CHAT_ID
from skp_ai import DEFAULT_MODEL
from skp_ai_sheet import SolDdsWriter, read_catalog
from skp_ai_worker import CALLS_PER_MONTH, Queue, SolWorker


LOG = logging.getLogger(__name__)
STATE_SHEET = "_skp_sol_state"
STATE_HEADER = "skp-sol-state-v1"
STATE_ROWS = {SHARED_DDS_CHAT_ID: 2, SHARED_DDS_OFFICE_CHAT_ID: 3}
BUDGET_ROW = 4
CHUNK_SIZE = 40000
MAX_CHUNKS = 24
TERMINAL_TAIL = 500
TERMINAL_STATES = ("written", "already_present", "answered", "skipped")


class SheetJournal:
    """A durable, per-chat snapshot on the same restricted DDS workbook."""

    def __init__(self, book):
        self.lock = RLock()
        try:
            self.sheet = book.worksheet(STATE_SHEET)
        except WorksheetNotFound:
            self.sheet = book.add_worksheet(title=STATE_SHEET, rows=4, cols=26)
        header = self.sheet.row_values(1)
        if not header:
            self.sheet.update(range_name="A1", values=[[STATE_HEADER]], raw=True)
            self.sheet.hide()
        elif header != [STATE_HEADER]:
            raise ValueError("SKP Sol state sheet has an unexpected header")
        self.budget = None

    def load(self, row, key, default):
        values = self.sheet.row_values(row)
        if not values:
            return default
        if len(values) < 3 or values[0] != key:
            raise ValueError("SKP Sol state row does not match its scope")
        count = int(values[1])
        if not 1 <= count <= MAX_CHUNKS or len(values) < count + 2:
            raise ValueError("Invalid SKP Sol state framing")
        packed = base64.b64decode("".join(values[2:count + 2]), validate=True)
        return json.loads(zlib.decompress(packed))

    def save(self, row, key, data):
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        packed = base64.b64encode(zlib.compress(encoded)).decode("ascii")
        chunks = [packed[i:i + CHUNK_SIZE] for i in range(0, len(packed), CHUNK_SIZE)]
        if len(chunks) > MAX_CHUNKS:
            raise ValueError("SKP Sol journal capacity exceeded")
        self.sheet.update(range_name=f"A{row}", values=[[key, len(chunks), *chunks]], raw=True)

    def claim_call(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        if self.budget is None:
            self.budget = self.load(BUDGET_ROW, "budget", {})
        count = self.budget.get(month, 0)
        if count >= CALLS_PER_MONTH:
            return False
        next_budget = {month: count + 1}
        try:
            self.save(BUDGET_ROW, "budget", next_budget)
        except Exception:
            self.budget = None
            raise
        self.budget = next_budget
        return True


class DurableQueue:
    def __init__(self, journal, chat_id):
        self.journal = journal
        self.row, self.key = STATE_ROWS[chat_id], str(chat_id)
        self.queue = Queue(":memory:", check_same_thread=False)
        self.fresh = False
        with journal.lock:
            self._restore()

    def _restore(self):
        snapshot = self.journal.load(self.row, self.key, {})
        with self.queue.db:
            for table in ("events", "clarification_memory", "office_reports"):
                self.queue.db.execute(f"DELETE FROM {table}")
                columns = {row["name"] for row in self.queue.db.execute(f"PRAGMA table_info({table})")}
                for record in snapshot.get(table, []):
                    if not record.keys() <= columns:
                        raise ValueError("Unsupported columns in SKP Sol state")
                    names = ",".join(record)
                    placeholders = ",".join("?" for _ in record)
                    self.queue.db.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(record.values()))
        self.fresh = True

    def _snapshot(self):
        db = self.queue.db
        params = ",".join("?" for _ in TERMINAL_STATES)
        rows = db.execute(
            f"""SELECT * FROM events WHERE state NOT IN ({params}) OR id IN (
                SELECT id FROM events WHERE state IN ({params}) ORDER BY id DESC LIMIT ?
            ) ORDER BY id""",
            (*TERMINAL_STATES, *TERMINAL_STATES, TERMINAL_TAIL),
        )
        events = [dict(row) for row in rows]
        kept_ids = {row["id"] for row in events}
        return {
            "events": events,
            "clarification_memory": [dict(row) for row in db.execute("SELECT * FROM clarification_memory")],
            "office_reports": [
                dict(row) for row in db.execute("SELECT * FROM office_reports") if row["event_id"] in kept_ids
            ],
        }

    def call(self, method, *args, **kwargs):
        with self.journal.lock:
            if method == "claim_call":
                return self.journal.claim_call()
            if not self.fresh:
                self._restore()
            before = self.queue.db.total_changes
            result = getattr(self.queue, method)(*args, **kwargs)
            if self.queue.db.total_changes != before:
                try:
                    self.journal.save(self.row, self.key, self._snapshot())
                except Exception:
                    # A timeout can mean the remote commit succeeded. Reload before retrying.
                    self.fresh = False
                    raise
            return result


class SkpSolRuntime:
    def __init__(self, shared_writer, api_key, starts_at, model=DEFAULT_MODEL):
        if not api_key or starts_at.tzinfo is None:
            raise ValueError("SKP Sol requires an API key and a timezone-aware activation time")
        request = Request(f"https://api.openai.com/v1/models/{quote(model, safe='')}",
                          headers={"Authorization": f"Bearer {api_key}"})
        with urlopen(request, timeout=10) as response:
            if json.load(response).get("id") != model:
                raise ValueError("Unexpected OpenAI model during SKP Sol preflight")
        self.starts_at = starts_at
        book = shared_writer.dds_book
        writer = SolDdsWriter(book, production=True, lock=shared_writer.lock)
        catalog = read_catalog(book)
        journal = SheetJournal(book)
        self.workers = {}
        for chat_id in SHARED_DDS_CHAT_IDS:
            worker = SolWorker(DurableQueue(journal, chat_id), writer, api_key, model, chat_id, chat_id)
            worker.catalog, worker.catalog_until = catalog, time.monotonic() + 300
            self.workers[chat_id] = worker

    async def receive(self, update, context):
        worker = self.workers.get(update.effective_chat.id)
        if worker is None:
            return
        for delay in (0, 2, 5):
            if delay:
                await asyncio.sleep(delay)
            try:
                await worker.handle(update, context)
                return
            except Exception as error:
                LOG.warning("SKP Sol could not queue chat=%s message=%s: %s",
                            worker.chat_id, update.effective_message.message_id, type(error).__name__)
        try:
            await update.effective_message.reply_text("Не удалось сохранить сообщение в очереди ДДС.")
        except Exception:
            LOG.warning("SKP Sol queue failure notification could not be delivered")

    async def process(self, context):
        for worker in self.workers.values():
            try:
                await worker.process(context)
            except Exception as error:
                LOG.warning("SKP Sol worker will retry on the next pass: %s", type(error).__name__)


def start_skp_sol(shared_writer, environ):
    if environ.get("SKP_DDS_MODE", "legacy") != "sol" or shared_writer is None:
        return None
    starts_at = datetime.fromisoformat(environ["SKP_DDS_START_AT"])
    model = environ.get("SKP_DDS_MODEL", DEFAULT_MODEL)
    return SkpSolRuntime(shared_writer, environ["OPENAI_API_KEY"].strip(), starts_at, model)
