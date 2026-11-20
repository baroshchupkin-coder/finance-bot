# Sol for SKP DDS

The live pre-Sol deployment is Render deploy `dep-dbracrnf3r2c73bq4gvg`,
Git commit `ef1b10274fd5289fa21205d636335b7d60906647`, tag
`finance-pre-sol-2026-11-16`. This baseline predates all Sol work on the
original finance bot. Do not copy environment secret values into Git.

## Enable

1. Deploy the code with `SKP_DDS_MODE` unset or `legacy`. Verify the service
   and its existing functionality before changing the DDS mode.
2. Add `OPENAI_API_KEY` in the Render service's environment. Keep
   `SKP_DDS_MODEL` unset for the tested default `gpt-6.1-sol`.
3. Set `SKP_DDS_MODE=sol` and `SKP_DDS_START_AT` to an ISO 8601 date/time
   with an offset. Deploy the environment changes. Verify the startup
   log `SKP DDS mode: sol` lists both `-1003721730980` and
   `-1003764038215`, and verify the existing health endpoint.

Sol replaces standalone message parsing only in those two chats, starting
at the activation time. No historical messages are replayed. The office
chat's KGS wallet hint remains `Офис подотчет`. Sol alone interprets the
chat text and explicitly calls `write_dds`. Questions keep their context
across restarts in the hidden `_skp_sol_state` worksheet of the SKP DDS.
The worksheet payload is compressed, not encrypted; restrict its access
the same way as the rest of the SKP DDS.

Invoice, receipt, Mini App, scheduled balance report, and other project
paths are unchanged. The existing confirmed-invoice DDS event remains
separate; it does not get passed through the standalone parser. Both
paths serialize writes to the same SKP sheet within the process.

## Office Balance Reconciliation

Only the assigned office manager's reports in the office chat take this
path. After a verified write to the office wallet, reconciliation reads the
DDS balance for the linked row, excluding later office movements in the
balance period. Arithmetic is in soms to cent precision. A match gets a
reaction; a new or changed discrepancy gets a reply to the report. It
does not create fee or balancing entries.

For newly recorded reports, an amount-only edit may update one linked
office row. The original transaction date, wallet, link, and prior amount
are checked; changed rows, formulas, and linked transfers require a manual
review. The updated amount is verified before checking the balance again.
Pending checks and edits live in the existing journal and survive restarts.
Previously recorded messages without an original snapshot are not
rewritten.

## Roll Back

- Set `SKP_DDS_MODE=legacy` and deploy. The original standalone parser
  resumes; the Sol queue stops without replaying its pending records.
- For a complete code rollback, deploy the tagged pre-Sol commit (or
  select the pre-Sol Render deploy if the build artifact is retained).
- Neither rollback deletes rows from the spreadsheet. Use the Telegram
  source links to review any rows that need correcting. If enabling
  again after a rollback, inspect the retained queue before doing so.
