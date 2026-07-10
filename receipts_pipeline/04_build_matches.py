"""
Phase D: enrichment matcher.

Attaches receipt context to Monarch transactions one-way:
  monarch_transactions <- (matches) <- receipts

Strategy:
  - For each Monarch row, find candidate receipts within ±3 days and
    where receipt.amount_cents == -monarch.amount_cents (since Monarch debits
    are negative; receipt charges are positive; same logic for refunds).
  - Score each candidate by fuzzy-matching merchant_norm strings.
  - Keep the highest-scoring receipt per Monarch row (one match per row).

Important per the data model: this is enrichment-only. Unmatched Monarch rows
are expected and fine — many real charges don't have email receipts (cash, in-person,
retailers without receipt emails, pre-mbox-window transactions).

Idempotent: clears the matches table and rebuilds.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from rapidfuzz import fuzz

DB_PATH = Path(__file__).resolve().parent / "receipts.db"

# Tuning knobs
DATE_WINDOW_DAYS = 3
AMOUNT_TOLERANCE_CENTS = 50
MIN_MERCHANT_SIMILARITY = 60   # rapidfuzz returns 0-100; below this we don't write a match


def fuzzy_score(receipt_merchant: str | None, monarch_merchant: str | None) -> float:
    """Return best of several rapidfuzz variants (0-100). Handles either side missing.

    Combines: token_set_ratio, partial_ratio, and a no-spaces variant. Takes the max
    so spaced vs. concatenated forms of the same merchant name ('some store' vs
    'SomeStore') score correctly.
    """
    if not receipt_merchant or not monarch_merchant:
        return 0.0
    a = receipt_merchant.lower()
    b = monarch_merchant.lower()
    a_nospace = a.replace(" ", "")
    b_nospace = b.replace(" ", "")
    return max(
        fuzz.token_set_ratio(a, b),
        fuzz.partial_ratio(a, b),
        fuzz.ratio(a_nospace, b_nospace),
    )


def build_matches(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    cur.execute("DELETE FROM matches")

    monarch_rows = cur.execute("""
        SELECT id, txn_date, merchant_norm, amount_cents, account_name
        FROM monarch_transactions
        WHERE amount_cents < 0
        ORDER BY txn_date
    """).fetchall()

    matched = 0
    skipped_low_sim = 0
    no_candidates = 0
    receipts_used = set()

    for monarch_id, txn_date, m_merchant, m_amount, m_account in monarch_rows:
        receipt_amount = -m_amount  # flip sign: Monarch debits are negative, receipts positive

        candidates = cur.execute("""
            SELECT message_id_hash, sent_date_local, merchant_norm, amount_cents,
                   ABS(julianday(?) - julianday(sent_date_local)) AS date_delta
            FROM receipts
            WHERE amount_cents BETWEEN ? AND ?
              AND date_delta <= ?
              AND extraction_method != 'none'
            ORDER BY date_delta
        """, (
            txn_date,
            receipt_amount - AMOUNT_TOLERANCE_CENTS,
            receipt_amount + AMOUNT_TOLERANCE_CENTS,
            DATE_WINDOW_DAYS,
        )).fetchall()

        if not candidates:
            no_candidates += 1
            continue

        # Score each candidate; pick best (highest similarity, then closest date)
        best = None
        for receipt_id, sent_date, r_merchant, r_amount, date_delta in candidates:
            if receipt_id in receipts_used:
                continue  # don't reuse a receipt for multiple Monarch rows
            sim = fuzzy_score(r_merchant, m_merchant)
            score = (sim, -date_delta)  # primary: similarity, secondary: closer date
            if best is None or score > best[0]:
                best = (score, receipt_id, r_merchant, r_amount, sent_date, sim, date_delta)

        if best is None:
            no_candidates += 1
            continue

        _, receipt_id, r_merchant, r_amount, sent_date, sim, date_delta = best

        if sim < MIN_MERCHANT_SIMILARITY:
            skipped_low_sim += 1
            continue

        match_type = "exact" if (sim >= 95 and abs(r_amount - receipt_amount) == 0) else (
            "fuzzy" if sim >= 80 else "weak"
        )

        cur.execute("""
            INSERT INTO matches (
                receipt_id, monarch_id, match_type, date_delta_days,
                amount_delta_cents, merchant_similarity, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            receipt_id, monarch_id, match_type,
            int(round(date_delta)),
            r_amount - receipt_amount,
            float(sim),
            min(1.0, sim / 100.0),
        ))
        receipts_used.add(receipt_id)
        matched += 1

    conn.commit()
    return {
        "monarch_total": len(monarch_rows),
        "matched": matched,
        "no_candidates": no_candidates,
        "skipped_low_sim": skipped_low_sim,
    }


def main() -> int:
    global MIN_MERCHANT_SIMILARITY
    ap = argparse.ArgumentParser()
    ap.add_argument("--similarity", type=int, default=MIN_MERCHANT_SIMILARITY,
                    help="Minimum merchant similarity 0-100 (default: 60)")
    args = ap.parse_args()
    MIN_MERCHANT_SIMILARITY = args.similarity

    conn = sqlite3.connect(DB_PATH)
    print(f"Date window:        ±{DATE_WINDOW_DAYS} days")
    print(f"Amount tolerance:   ±{AMOUNT_TOLERANCE_CENTS} cents")
    print(f"Min merchant sim:   {MIN_MERCHANT_SIMILARITY}/100")
    print()

    stats = build_matches(conn)
    print(f"Monarch debit rows:        {stats['monarch_total']}")
    print(f"  matched:                 {stats['matched']}  ({100*stats['matched']/max(stats['monarch_total'],1):.1f}%)")
    print(f"  no amount/date candidate: {stats['no_candidates']}")
    print(f"  candidate but low sim:   {stats['skipped_low_sim']}")

    print()
    print("=== match_type distribution ===")
    cur = conn.cursor()
    for row in cur.execute("SELECT match_type, COUNT(*), ROUND(AVG(merchant_similarity),1), ROUND(AVG(date_delta_days),1) FROM matches GROUP BY match_type ORDER BY 2 DESC"):
        print(f"  {row[0]:<10} count={row[1]:<5}  avg_sim={row[2]}  avg_date_delta={row[3]}d")

    print()
    print("=== sample matches (10 random) ===")
    for row in cur.execute("""
        SELECT m.match_type, m.merchant_similarity, m.date_delta_days,
               mt.txn_date, mt.merchant, mt.amount_cents/100.0,
               r.subject, r.merchant_raw
        FROM matches m
        JOIN monarch_transactions mt ON mt.id = m.monarch_id
        JOIN receipts r ON r.message_id_hash = m.receipt_id
        ORDER BY RANDOM() LIMIT 10
    """):
        sub = (row[6] or "")[:50]
        print(f"  {row[0]:<6} sim={row[1]:>5.1f}  Δd={row[2]}  {row[3]}  ${row[5]:>9.2f}  '{row[4]}' <-> '{sub}'")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
