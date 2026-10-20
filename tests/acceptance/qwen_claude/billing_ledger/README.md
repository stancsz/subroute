# Payment export reconciliation task

A payment provider sends a CSV export with exactly these columns in order: `event_id,account_id,kind,amount_cents,charge_id`. Rows are chronological. `kind` is `charge` or `refund`; a charge has an empty `charge_id`, and a refund references a preceding charge event ID. All IDs must be nonempty after trimming. Amounts are positive base-10 integer cents, with no sign, decimal point, or whitespace. A UTF-8 BOM on the header is allowed. Blank lines may be ignored. Preserve CSV quoting.

`parse_events(csv_text)` returns event dictionaries with `amount_cents` as an integer and trimmed IDs. Reject malformed rows and headers with `LedgerError`; include the row number in row errors. `reconcile(events)` returns account balances in cents. A repeated `event_id` with identical normalized fields is a replay and must not change balances; a conflicting repeat is an error. A refund must match the charge's account and total refunds against that charge cannot exceed its amount. Reject invalid input before returning any result. The function must not mutate the caller's events.

Edit only `csv_ingest.py` and `ledger.py`. Run `python -m unittest -q` before and after.
