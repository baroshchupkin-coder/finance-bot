# DDS integration

## Scope

- Spreadsheet: `1YCHamDIfI0TMCEuNOXLmNCnbQLThW8-woWOwE-7_cSw`
- Sheet: `ДДС: месяц`
- Telegram chats:
  - `-1003806940668`
  - `-1003764038215`
- DDS fields written automatically:
  - `D`: payment date
  - `E`: signed numeric amount
  - `F`: exact wallet name, or blank when it cannot be determined
  - `H`: compact payment description and optional Telegram message link

The integration does not overwrite formulas in `A:C` and `J:L`.
Columns `G` and `I` remain unchanged.

## Sources

1. A bot invoice after the payer attaches a receipt and the request is
   successfully changed to `Оплачено`.
2. A new standalone text/caption in one of the two configured payment chats.

Old Telegram history is not scanned. The default activation boundary is
`2026-08-04T06:17:42+00:00`; it can be overridden with `DDS_START_AT`.

## Standalone message rules

1. An amount with KGS, RUB or USD/USDT at the start is accepted. For a media
   caption, one explicit amount with currency may also appear after the text.
2. In the two configured chats, a short message without an explicit currency
   is accepted as KGS only when it has exactly one amount at the start or after
   a separator at the end. The inferred currency is visible in `source_kind`.
3. An explicit `+` is income; an explicit minus is expense.
4. An unsigned leading amount is treated as an expense.
5. `Остаток` and the balance after it are not used as the transaction amount.
6. A balance-only message is ignored.
7. A receipt without a caption is ignored.
8. An amount without a description is accepted only with attached media.
9. Discussion containing numbers later in the text is ignored.
10. Media with a caption is written even when the amount is unknown. The chat's
    KGS default selects the payer's KGS wallet, and the source Telegram message
    link is added as a clickable rich-text link.
11. A description-first text ending with a conversion such as
    `81,54 $ = 7 134,75 сом` uses the charged amount and currency on the left.
12. Every accepted standalone payment gets a clickable link to its Telegram
    source message, including text-only payments.

## Receipt OCR

Image-only receipts can be inspected by Tesseract. PDF documents are not OCRed in the first phase.

- OCR runs in a background task and never blocks Telegram callback handling.
- Only one image is processed at a time; another image falls back to the existing DDS rules.
- Each image is limited by file size, pixel count and an 8-second timeout.
- `DDS_OCR_MODE=shadow` is the safe initial mode. The recognized text, candidate amount, duration and decision reason are written only to `dds_logs` under an `ocr:<chat_id>:<message_id>` key.
- `DDS_OCR_MODE=write` uses only a high-confidence amount. Multiple unlabelled amounts are treated as ambiguous and are not guessed.
- If Tesseract is missing or fails, the existing text/caption processing continues unchanged.
- The Render Docker image installs English and Russian Tesseract language data.

## Wallet mappings

- `@n0visad`: `Александр KGS`, `Александр`, `Александр $`
- `@bulat_sufyanov`: `Булат KGS`, `Булат`, `Булат $`
- `@KirillVorontcov`: `Офис подотчет` for KGS

Unknown users/currencies are not guessed. The DDS row is still created with a
blank wallet, while `dds_logs.reason` keeps the missing-mapping explanation.
Known payers are mapped both by Telegram user ID and username.

## Bot invoice descriptions

- If a bot invoice in one of the configured payment chats omits the currency,
  the chat default is KGS; this also selects the payer's KGS wallet.
- The DDS purpose starts with `Счет #<id> — <target>`.
- Amount breakdown lines such as fixed part, KPI and percentage are preserved.
- Total-only lines, payment dates, bank/card/phone/wallet details and transfer
  instructions are removed from the DDS purpose.
- The complete original request remains in the `requests` sheet.

## Safety and idempotency

- `DDS_ENABLED=false` disables the integration without changing code.
- `DDS_START_AT` rejects events before the activation timestamp.
- `DDS_WRITE_START_ROW` defaults to row `606`.
- `DDS_OCR_ENABLED=false` disables receipt OCR independently of the main DDS integration.
- Bot invoices use `invoice:<request_id>` as the idempotency key.
- Standalone messages use `message:<chat_id>:<message_id>`.
- `dds_logs` records processing status, target DDS row, payer, currency,
  amount and the original description.
- DDS writes run outside the Telegram update handler. Temporary Google API
  errors (`429`, `500`, `502`, `503`, `504` and connection timeouts) are
  retried after `2`, `5`, `15`, `30` and `60` seconds.
- Telegram callback queries are acknowledged before the slower Google Sheets
  lookup, so a temporary DDS outage cannot make later button clicks expire.
