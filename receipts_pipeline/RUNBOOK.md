# Receipts pipeline — Runbook

How to run the Gmail Takeout → SQLite receipts pipeline, including incremental re-imports.

## What it does

Turns a Gmail Takeout export (mbox files) + Monarch CSVs + Amazon Order History into a single SQLite DB (`receipts.db`) plus an interactive `dashboard.html`. Receipts enrich Monarch transactions; **Monarch is the spending truth** — never sum amounts from receipts.

## One-time setup

- Python packages: `pandas`, `plotly`, `rapidfuzz`, `tzdata` (and stdlib `mailbox`, `sqlite3`).
- Claude CLI on PATH for the LLM extraction tier (`claude -p`, Haiku). Set `--no-llm` on `02_extract_amounts.py` to skip.

## Gmail Takeout export — what to select

1. https://takeout.google.com → deselect all → select **Mail** only.
2. "All Mail data included" → choose **Select labels**. Select every label under which financial mail lands in YOUR account — typically the Gmail auto-categories (`Category Bills`, `Category Purchases`, `Category Travel`) plus whatever receipt/bill labels you maintain yourself. Keep the same label set run-to-run for consistency.
3. Format: **mbox**. Delivery: download link by email. Expect ~400 MB.

## Where to put the export

`01_parse_mbox.py` reads from a hardcoded path:

```
Financial/<DATE> Gmail Financial Label Export/Mail/*.mbox
```

For a new Takeout, **edit `MBOX_DIR` in `01_parse_mbox.py:36`** to point at the new dated folder. (Don't merge mboxes into the old folder — the date in the folder name is your only freshness marker.)

## Run order

```
python 01_parse_mbox.py          # mbox → receipts skeleton + body files
python 02_extract_amounts.py     # template → regex → LLM (Haiku) cascade
python 02b_cleanup_extractions.py  # Schwab blacklist + merchant alias normalize
python 03_load_reference.py      # Monarch CSVs + Amazon Order History → DB
python 04_build_matches.py       # fuzzy match Monarch debits ↔ receipts
python 05_build_dashboard.py     # write dashboard.html
```

Each script is idempotent. Specifics:

| Script | Dedup mechanism | Re-run behavior |
|---|---|---|
| 01 | `INSERT OR IGNORE` on `sha1(Message-ID)` | Same email across two Takeouts is skipped silently — safe to re-import overlapping windows |
| 02 | Skips rows where `extraction_method IS NOT NULL` | Only processes new rows. Pass `--reextract` to redo all, `--no-llm` to skip Haiku, `--limit N` to test |
| 02b | Updates only matching rows | Always safe |
| 03 | Truncates each source's table and reloads | Always reflects current CSV exports |
| 04 | Clears `matches` table and rebuilds | Always |
| 05 | Overwrites `dashboard.html` | Always |

## Incremental re-run (new Takeout, same Monarch/Amazon)

1. Take a fresh Gmail Takeout (same labels), drop into a new dated folder.
2. Update `MBOX_DIR` in `01_parse_mbox.py:36`.
3. Run `01` → `02` → `02b` → `04` → `05`. Skip `03` if Monarch/Amazon CSVs haven't changed.
4. New emails get inserted; old Message-IDs are skipped. Only new receipts hit the LLM tier.

## Refreshing Monarch / Amazon (no new mbox)

> **Note (2026-07-04):** live Monarch queries no longer need this — the Monarch MCP (`mcp__monarch__*`) reads the API directly. This re-export flow is still required to refresh `receipts.db`; the pipeline loads from CSVs only.

1. Re-export Monarch CSVs (one per account) into `Financial/Monarch Exports/` — keep `Account Name` prepended as column 1.
2. Re-export Amazon Order History into `Financial/Prime Analysis/Your Orders/Your Amazon Orders/Order History.csv`.
3. Run `03` → `04` → `05`.

## Cost & runtime expectations (last full run, ~4,877 receipts)

- `01`: ~1–2 min, mostly I/O. Writes ~12 MB of body text files to `bodies/`.
- `02`: templates + regex are instant. **LLM tier: 758 emails via Haiku, $0 failures.** Estimate at current Haiku pricing — well under $1 for a full re-run.
- `02b` / `04` / `05`: seconds.
- Incremental runs are dominated by however many *new* rows hit the LLM tier.

## Failure modes to watch

- **Mbox path wrong** → `01` errors immediately. Check `MBOX_DIR`.
- **Haiku CLI missing** → `02` errors at the LLM tier. Use `--no-llm` to ship without it (758 rows last run would stay un-extracted).
- **Schwab trade alerts** counted as charges → `02b` blacklists `mail.schwab.com`. If a new non-receipt sender slips through, add it to `NON_RECEIPT_DOMAINS` there.
- **Pre-2024-04 spending charts look thin** — that's by design until the Chase CC PDF backfill runs (see `Financial/CHECKPOINT.md` open thread on `csvconv`).

## Sharing this with someone else

What's portable: the scripts in this folder, `schema.sql`, this runbook.

What's personal and must be replaced:
- The hardcoded `MBOX_DIR` path in `01_parse_mbox.py`
- The Monarch account list / preprocessing convention (`Account Name` prepended) in `Financial/CLAUDE.md`
- Amazon path in `03_load_reference.py`
- Schwab-specific blacklist in `02b_cleanup_extractions.py`
- Template extractors in `extractors/` are merchant-specific (uber/lyft/capitalbikeshare/amazon) — useful as patterns, but they'll want their own.
