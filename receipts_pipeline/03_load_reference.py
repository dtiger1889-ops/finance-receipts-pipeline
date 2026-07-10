"""
Phase C: load Monarch CSVs + Amazon Order History.csv + Amazon mbox into the DB.

Idempotent: each loader truncates its source's rows and reloads, so re-running
after a fresh export just refreshes the data.

Source-of-truth conventions (see plan: "Monarch is truth, receipts/Amazon are enrichment"):
- monarch_transactions is the spending ledger. Never sum amounts from receipts/Amazon.
- amazon_orders has source='order_history_csv' (canonical) and source='mbox' (enrichment).
  Default queries should filter to source='order_history_csv'.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PIPELINE_DIR.parent
DB_PATH = PIPELINE_DIR / "receipts.db"
MONARCH_DIR = PROJECT_DIR / "Monarch Exports"
AMAZON_CSV = PROJECT_DIR / "Prime Analysis" / "Your Orders" / "Your Amazon Orders" / "Order History.csv"

# Monarch's Merchant field drifts (auto-categorization re-guesses the name from the
# bank's raw statement text on each import), so the same recurring charge can
# appear under several merchant labels. Two alias keying strategies:
#
# 1. By original_statement — preferred when the bank's raw description is stable.
# 2. By merchant prefix — fallback when original_statement is NULL but the merchant
#    label has a stable lead substring with a date-suffix tail (e.g. ACH payments
#    that Monarch stamps with the cycle date: "MY LOAN SERVICER PAYMENTS 050125").
#
# Form: match-key -> (canonical_merchant, canonical_merchant_norm)
# Both maps ship EMPTY: alias entries are your own recurring charges and are
# personal by construction. Fill them in from your own statement data.
MONARCH_STATEMENT_ALIASES: dict[str, tuple[str, str]] = {}
MONARCH_MERCHANT_PREFIX_ALIASES: dict[str, tuple[str, str]] = {
    # Lowercase prefix; matched case-insensitively against `merchant`.
}


def normalize_merchant(s: str | None) -> str | None:
    if not s:
        return None
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def parse_amount_to_cents(raw: str) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return None


def parse_iso_date(raw: str) -> str | None:
    """Accepts 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SSZ'. Returns YYYY-MM-DD."""
    if not raw:
        return None
    s = raw.strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def load_monarch(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    # AUTOINCREMENT will bump IDs on reload, invalidating match references.
    # Clear matches too so re-running this script + 04_build_matches.py is safe.
    cur.execute("DELETE FROM matches")
    cur.execute("DELETE FROM monarch_transactions")
    inserted = 0
    files = sorted(MONARCH_DIR.glob("*.csv"))
    if not files:
        print(f"  WARN: no CSVs found in {MONARCH_DIR}", file=sys.stderr)
        return 0

    for path in files:
        n_file = 0
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                amount_cents = parse_amount_to_cents(row.get("Amount", ""))
                txn_date = parse_iso_date(row.get("Date", ""))
                if amount_cents is None or txn_date is None:
                    continue
                merchant = (row.get("Merchant") or "").strip()
                cur.execute(
                    """
                    INSERT INTO monarch_transactions (
                        account_name, txn_date, merchant, merchant_norm,
                        category, original_statement, notes, amount_cents,
                        tags, owner, source_file
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (row.get("Account Name") or "").strip(),
                        txn_date,
                        merchant or None,
                        normalize_merchant(merchant),
                        (row.get("Category") or "").strip() or None,
                        (row.get("Original Statement") or "").strip() or None,
                        (row.get("Notes") or "").strip() or None,
                        amount_cents,
                        (row.get("Tags") or "").strip() or None,
                        (row.get("Owner") or "").strip() or None,
                        path.name,
                    ),
                )
                inserted += 1
                n_file += 1
        print(f"  {path.name}: {n_file} rows")
    conn.commit()
    return inserted


def apply_monarch_statement_aliases(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    n_total = 0
    for stmt, (merchant, merchant_norm) in MONARCH_STATEMENT_ALIASES.items():
        cur.execute(
            """
            UPDATE monarch_transactions
            SET merchant = ?, merchant_norm = ?
            WHERE original_statement = ?
              AND (merchant != ? OR merchant_norm != ?)
            """,
            (merchant, merchant_norm, stmt, merchant, merchant_norm),
        )
        n = cur.rowcount
        n_total += n
        if n:
            print(f"  stmt={stmt!r:<40} -> {merchant!r:<20} ({n} rows)")
    conn.commit()
    return n_total


def apply_monarch_merchant_prefix_aliases(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    n_total = 0
    for prefix, (merchant, merchant_norm) in MONARCH_MERCHANT_PREFIX_ALIASES.items():
        cur.execute(
            """
            UPDATE monarch_transactions
            SET merchant = ?, merchant_norm = ?
            WHERE LOWER(merchant) LIKE ? || '%'
              AND (merchant != ? OR merchant_norm != ?)
            """,
            (merchant, merchant_norm, prefix, merchant, merchant_norm),
        )
        n = cur.rowcount
        n_total += n
        if n:
            print(f"  prefix={prefix!r:<30} -> {merchant!r:<20} ({n} rows)")
    conn.commit()
    return n_total


def load_amazon_csv(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("DELETE FROM amazon_orders WHERE source='order_history_csv'")
    if not AMAZON_CSV.exists():
        print(f"  WARN: {AMAZON_CSV} not found", file=sys.stderr)
        return 0

    inserted = 0
    line_counters: dict[str, int] = {}
    with open(AMAZON_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            order_id = (row.get("Order ID") or "").strip() or None
            if not order_id:
                continue
            line_no = line_counters.get(order_id, 0) + 1
            line_counters[order_id] = line_no

            order_date = parse_iso_date(row.get("Order Date", ""))
            ship_date = parse_iso_date(row.get("Ship Date", ""))
            unit_cents = parse_amount_to_cents(row.get("Unit Price", ""))
            total_cents = parse_amount_to_cents(row.get("Total Amount", ""))

            cur.execute(
                """
                INSERT OR REPLACE INTO amazon_orders (
                    order_id, line_no, order_date, ship_date,
                    product_name, unit_price_cents, total_amount_cents,
                    payment_method, merchant, source, raw_row
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'order_history_csv', NULL)
                """,
                (
                    order_id,
                    line_no,
                    order_date,
                    ship_date,
                    (row.get("Product Name") or "").strip() or None,
                    unit_cents,
                    total_cents,
                    (row.get("Payment Method Type") or "").strip() or None,
                    (row.get("Website") or "").strip() or None,
                ),
            )
            inserted += 1
    conn.commit()
    return inserted


RE_ORDER_ID_NOTE = re.compile(r"order_id=([\d\-]+)")
RE_ORDER_ID_SUBJECT = re.compile(r"#\s*([\d]{3}-[\d]{7}-[\d]{7})")


def load_amazon_from_mbox(conn: sqlite3.Connection) -> int:
    """Pull amazon receipts already extracted into the receipts table and copy
    them into amazon_orders with source='mbox'. This is enrichment-only — adds
    refunds/cancellations/gift-card receipts that don't appear in Order History.csv.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM amazon_orders WHERE source='mbox'")

    # Collect all order_ids already in the CSV source so we know what's "new"
    csv_order_ids = {row[0] for row in cur.execute("SELECT DISTINCT order_id FROM amazon_orders WHERE source='order_history_csv'")}

    inserted = 0
    skipped_dup = 0

    rows = cur.execute("""
        SELECT message_id_hash, sent_date_local, subject, amount_cents, extraction_notes
        FROM receipts
        WHERE merchant_norm = 'amazon'
          AND amount_cents IS NOT NULL
    """).fetchall()

    for msg_hash, sent_date, subject, amount_cents, notes in rows:
        # Try to extract Amazon order ID from notes or subject
        order_id = None
        if notes:
            m = RE_ORDER_ID_NOTE.search(notes)
            if m:
                order_id = m.group(1)
        if not order_id and subject:
            m = RE_ORDER_ID_SUBJECT.search(subject)
            if m:
                order_id = m.group(1)
        # Use message_id_hash as synthetic order_id when none found (for refund-only emails, gift cards, etc.)
        if not order_id:
            order_id = f"mbox-{msg_hash[:12]}"

        if order_id in csv_order_ids:
            # CSV already covers this order. Skip to honor "default queries trust CSV" rule.
            skipped_dup += 1
            continue

        cur.execute(
            """
            INSERT OR REPLACE INTO amazon_orders (
                order_id, line_no, order_date, total_amount_cents,
                merchant, source, raw_row
            ) VALUES (?, 1, ?, ?, 'Amazon', 'mbox', ?)
            """,
            (
                order_id,
                sent_date[:10] if sent_date else None,
                amount_cents,
                f"receipt:{msg_hash}",
            ),
        )
        inserted += 1

    conn.commit()
    print(f"  mbox-only orders inserted: {inserted}  (skipped {skipped_dup} that exist in CSV)")
    return inserted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-monarch", action="store_true")
    ap.add_argument("--skip-amazon-csv", action="store_true")
    ap.add_argument("--skip-amazon-mbox", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if not args.skip_monarch:
        print("=== Loading Monarch CSVs ===")
        n = load_monarch(conn)
        print(f"Monarch rows inserted: {n}\n")

        print("=== Canonicalizing Monarch merchant aliases (by original_statement) ===")
        n_aliased = apply_monarch_statement_aliases(conn)
        print(f"Monarch rows realiased: {n_aliased}\n")

        print("=== Canonicalizing Monarch merchant aliases (by merchant prefix) ===")
        n_aliased_pre = apply_monarch_merchant_prefix_aliases(conn)
        print(f"Monarch rows realiased: {n_aliased_pre}\n")

    if not args.skip_amazon_csv:
        print("=== Loading Amazon Order History.csv ===")
        n = load_amazon_csv(conn)
        print(f"Amazon CSV rows inserted: {n}\n")

    if not args.skip_amazon_mbox:
        print("=== Loading Amazon mbox-only orders (refunds/cancellations the CSV lacks) ===")
        load_amazon_from_mbox(conn)
        print()

    print("=== verification ===")
    cur = conn.cursor()
    n_monarch = cur.execute("SELECT COUNT(*) FROM monarch_transactions").fetchone()[0]
    mn_date, mx_date = cur.execute("SELECT MIN(txn_date), MAX(txn_date) FROM monarch_transactions").fetchone()
    print(f"  monarch_transactions: {n_monarch} rows, {mn_date} -> {mx_date}")
    for src, n in cur.execute("SELECT source, COUNT(*) FROM amazon_orders GROUP BY source"):
        print(f"  amazon_orders source={src}: {n} rows")
    print()
    print("Spending ledger by account (Monarch totals — this is the truth):")
    for row in cur.execute("""
        SELECT account_name, COUNT(*),
               SUM(CASE WHEN amount_cents < 0 THEN amount_cents ELSE 0 END)/100.0 AS debits,
               SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END)/100.0 AS credits
        FROM monarch_transactions GROUP BY account_name ORDER BY 2 DESC
    """):
        print(f"  {row[0]:<40} rows={row[1]:>5}  debits=${row[2]:>11,.2f}  credits=${row[3]:>11,.2f}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
