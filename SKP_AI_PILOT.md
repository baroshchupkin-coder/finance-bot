# SKP DDS: isolated AI pilot

The production process still runs python bot.py. It does not import these
modules or read the pilot variables. Its payment handling, CPP DDS and
daily SKP balance report are unchanged. Do not switch the production start
command to this pilot or run two pollers with the same Telegram token.

## Test setup

1. Use a separate Telegram bot and private test supergroup. Add the test
   bot as an admin so it receives ordinary messages. Do not add the test
   bot to a production chat. Use synthetic or redacted examples.
2. In the OpenAI API project, create a service-account key authorized to
   call Responses. Add it to the test runtime as OPENAI_API_KEY. Do not
   paste it into chat, source code, a Google Sheet or command-line arguments.
3. Give the Google service account used by the test runtime Editor access
   to the copy only. For testing with the existing credentials, share
   the copy with finance-bot@finance-bot-494205.iam.gserviceaccount.com.
   The copy is pinned by ID in skp_ai_sheet.py; the writer rejects every
   other workbook, including the production workbook.
4. In a separate single-instance worker or local Python process, install
   requirements.txt and start python skp_ai_pilot.py. Set:

   - SKP_AI_TEST_BOT_TOKEN: the separate test bot's token;
   - SKP_AI_TEST_CHAT_ID: the test supergroup ID, starting with -100;
   - GOOGLE_CREDENTIALS: the Google service-account JSON, as a secret;
   - SKP_AI_STATE_PATH: an absolute path to a SQLite file on persistent
     storage, writable only by the worker. Use one DB per test group;
   - OPENAI_API_KEY: the OpenAI project key;
   - OPENAI_MODEL: optional; defaults to gpt-6.1-sol (gpt-6-luna also works
     with the same API request shape, subject to project access);
   - SKP_AI_PROFILE_CHAT_ID: optional; use -1003721730980 for the common
     chat profile or -1003764038215 to test the office-wallet defaults.

   This worker does not start a web server or need the production bot's
   secrets other than Google credentials if you intentionally reuse them.

## Processing

- The worker queues incoming text and captions only. It does not send
  receipt files, screenshots or bank statements to the API. A replied-to
  message is limited context for classification, never evidence that a
  payment occurred. Answers to the bot's question must be replies to that
  question, from the author of the original operation.
- Structured Outputs, local checks, and the live catalog are separate:
  the model proposes an action, the validator checks amount quotes, signs,
  dates, wallets, article direction and valid project/article pairs.
  Low-confidence or inconsistent results ask one question at a time.
- Pilot writes are D:J on the copy only, with the original Telegram link.
  Two-sided movements are one Sheets batch. The project and article
  dropdowns, formulas and column Q are preserved. Duplicate detection
  checks the exact Telegram link, including rows entered manually. If a
  manual entry has no link, it cannot be safely identified automatically.
- A lost Sheets response is retried by re-reading the link and comparing
  the staged operation. Existing partial/conflicting rows go to review,
  not to a second write. Edits to recorded messages require manual review.
  Use one writer and do not manually edit the append area while it runs.
- The SQLite queue keeps pending messages and prepared rows across restarts.
  After four failed attempts, the event stays in review. It does not fall
  through to the old parser. A durable cap reserves at most 300 Responses
  requests per UTC month, including retries. Logs show token usage, not
  message text. This is a request cap, not a guarantee of spend.
- Only wallet names/currencies, project/article names and selected message
  text go to Responses, not wallet balances or the full spreadsheet.
  Card-like numbers, common wallet addresses and URLs are redacted.
  Redaction is best-effort. store=false is not zero data retention.

## Acceptance before production

1. Exercise ordinary expenses and income, a transfer, an exchange, a
   principal/interest split, a request before payment, and an unclear
   wallet. Confirm the office top-up still comes from Александр С.
2. Verify that each pair appears together, the correct wallet balances
   recalculate, dependent dropdowns still work and Q is unchanged.
3. Test an existing row carrying the same source link, duplicate delivery,
   a lost write response, an edit after writing, and an API outage. Compare
   observed token usage and latency on a representative test set.
4. Only then design and approve a production switch: route each new SKP
   standalone event to exactly one writer, keep bot-invoice payments and
   the evening report intact, and choose an activation time to avoid
   processing old messages. No production switch is provided by this pilot.
