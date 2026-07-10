"""
Phase B post-cleanup pass.

Two corrections to apply after the LLM extraction tier:

1. Sender-domain blacklist for non-receipt notifications (e.g. a brokerage's
   trade alerts that the LLM treated as charges).
   These get amount_cents=NULL, extraction_method='none', a clear note.

2. Merchant alias canonicalization. The LLM produces inconsistent merchant
   names across independent batches (spacing/short-form variants of the same
   merchant). The generic-regex extractor also derives merchant_norm from
   sender_domain, which leaks subdomain artifacts (mail-subdomain forms of the
   merchant name).

Idempotent: runs only on rows that need it. Safe to re-run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "receipts.db"

NON_RECEIPT_DOMAINS = {
    # sender domains whose mails are alerts, not spending -- fill in from your own data
}

MERCHANT_ALIASES = {
    # form: bad_norm -> canonical_norm. Ships EMPTY: alias entries mirror your own
    # merchant footprint and are personal by construction. Fill in as dupes appear.
}


def cleanup_non_receipt_domains(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    n_total = 0
    for domain, reason in NON_RECEIPT_DOMAINS.items():
        cur.execute(
            """
            UPDATE receipts SET
                amount_cents = NULL,
                merchant_norm = NULL,
                merchant_raw = NULL,
                txn_date = NULL,
                extraction_method = 'none',
                extraction_confidence = 1.0,
                extraction_notes = ?
            WHERE sender_domain = ?
              AND amount_cents IS NOT NULL
            """,
            (f"cleanup: {reason}", domain),
        )
        n = cur.rowcount
        n_total += n
        if n:
            print(f"  blacklisted {n:>3} rows from {domain}")
    conn.commit()
    return n_total


def cleanup_merchant_aliases(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    n_total = 0
    for bad, good in MERCHANT_ALIASES.items():
        cur.execute(
            "UPDATE receipts SET merchant_norm = ? WHERE merchant_norm = ?",
            (good, bad),
        )
        n = cur.rowcount
        n_total += n
        if n:
            print(f"  {bad:<25} -> {good:<25} ({n} rows)")
    conn.commit()
    return n_total


def main() -> int:
    conn = sqlite3.connect(DB_PATH)

    print("=== Step 1: blacklist non-receipt sender domains ===")
    n_blacklist = cleanup_non_receipt_domains(conn)
    print(f"Total rows reset to method='none': {n_blacklist}")

    print()
    print("=== Step 2: canonicalize merchant aliases ===")
    n_aliases = cleanup_merchant_aliases(conn)
    print(f"Total merchant_norm values updated: {n_aliases}")

    print()
    print("=== Post-cleanup top merchants (>=10 receipts) ===")
    cur = conn.cursor()
    for row in cur.execute(
        """
        SELECT merchant_norm, COUNT(*), COALESCE(SUM(amount_cents)/100.0, 0)
        FROM receipts
        WHERE merchant_norm IS NOT NULL
        GROUP BY merchant_norm
        HAVING COUNT(*) >= 10
        ORDER BY COUNT(*) DESC
        """
    ):
        print(f"  {row[1]:>4}  ${row[2]:>11.2f}  {row[0]}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
