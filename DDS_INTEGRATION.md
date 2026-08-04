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
  - `F`: exact wallet name
  - `H`: full payment description

The integration does not overwrite formulas in `A:C` and `J:L`.
Columns `G` and `I` remain unchanged.

## Sources

1. A bot invoice after the payer attaches a receipt and the request is
   successfully changed to `Оплачено`.
2. A new standalone text/caption in one of the two configured payment chats.

Old Telegram history is not scanned. The default activation boundary is
`2026-08-04T06:17:42+00:00`; it can be overridden with `DDS_START_AT`.

## Standalone message rules

1. The first meaningful token must be an amount with KGS, RUB or USD/USDT.
2. An explicit `+` is income; an explicit minus is expense.
3. An unsigned leading amount is treated as an expense.
4. `Остаток` and the balance after it are not used as the transaction amount.
5. A balance-only message is ignored.
6. A receipt without a caption is ignored.
7. An amount without a description is accepted only with attached media.
8. Discussion containing numbers later in the text is ignored.

OCR of image-only and PDF-only receipts is not enabled.

## Wallet mappings

- `@n0visad`: `Александр KGS`, `Александр`, `Александр $`
- `@bulat_sufyanov`: `Булат KGS`, `Булат`, `Булат $`
- `@KirillVorontcov`: `Офис подотчет` for KGS

Unknown users/currencies are not guessed. They are written to `dds_logs`
with status `needs_wallet` and do not create a DDS row.

## Safety and idempotency

- `DDS_ENABLED=false` disables the integration without changing code.
- `DDS_START_AT` rejects events before the activation timestamp.
- `DDS_WRITE_START_ROW` defaults to row `606`.
- Bot invoices use `invoice:<request_id>` as the idempotency key.
- Standalone messages use `message:<chat_id>:<message_id>`.
- `dds_logs` records processing status, target DDS row, payer, currency,
  amount and the original description.
