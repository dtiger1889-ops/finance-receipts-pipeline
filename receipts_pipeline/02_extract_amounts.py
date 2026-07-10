"""
Phase B: amount + merchant extraction.

Three-tier cascade per receipt:
  1. Template extractors keyed on sender_domain (uber, lyft, capitalbikeshare, amazon).
  2. Generic regex (Total/Order Total/Amount Charged patterns).
  3. LLM fallback via `claude -p` (Haiku), batched. Run last on whatever's left.

Idempotent: skips receipts that already have extraction_method set.
Pass --reextract to overwrite all existing extractions.
Pass --no-llm to skip the LLM tier (rules only).
Pass --limit N to process only the first N candidate rows.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
DB_PATH = PIPELINE_DIR / "receipts.db"

sys.path.insert(0, str(PIPELINE_DIR))
from extractors import TEMPLATE_REGISTRY, generic_regex, llm_fallback


def select_candidates(conn: sqlite3.Connection, reextract: bool, limit: int | None):
    cur = conn.cursor()
    where = "WHERE body_path IS NOT NULL"
    if not reextract:
        where += " AND extraction_method IS NULL"
    sql = f"""
        SELECT message_id_hash, sender_domain, subject, body_path
        FROM receipts
        {where}
        ORDER BY sent_date_local DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return cur.execute(sql).fetchall()


def read_body(rel_path: str) -> str:
    p = PIPELINE_DIR / rel_path
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def update_extraction(conn: sqlite3.Connection, msg_hash: str, result: dict | None) -> None:
    cur = conn.cursor()
    if result is None:
        cur.execute(
            """UPDATE receipts SET
                amount_cents=NULL, merchant_norm=NULL, merchant_raw=NULL, txn_date=NULL,
                extraction_method='none', extraction_confidence=0.0, extraction_notes=NULL
               WHERE message_id_hash=?""",
            (msg_hash,),
        )
        return
    cur.execute(
        """UPDATE receipts SET
            amount_cents=?, merchant_norm=?, merchant_raw=?, txn_date=?,
            extraction_method=?, extraction_confidence=?, extraction_notes=?
           WHERE message_id_hash=?""",
        (
            result.get("amount_cents"),
            result.get("merchant_norm"),
            result.get("merchant_raw"),
            result.get("txn_date"),
            result.get("method"),
            result.get("confidence"),
            result.get("notes"),
            msg_hash,
        ),
    )


def try_template(sender_domain: str, body: str, subject: str) -> dict | None:
    extractors = TEMPLATE_REGISTRY.get(sender_domain or "", [])
    for mod in extractors:
        try:
            res = mod.extract(body, subject)
            if res:
                return res
        except Exception as e:
            print(f"  [template:{mod.__name__}] error: {e}", file=sys.stderr)
    return None


def try_generic(body: str, subject: str, sender_domain: str) -> dict | None:
    try:
        return generic_regex.extract(body, subject, sender_domain)
    except Exception as e:
        print(f"  [generic_regex] error: {e}", file=sys.stderr)
        return None


def run_rules(conn, candidates):
    """Run template + generic regex tiers. Returns list of (msg_hash, sender_domain, subject, body_path) still NULL."""
    template_hits = 0
    regex_hits = 0
    residual = []
    for i, (msg_hash, sender_domain, subject, body_path) in enumerate(candidates, 1):
        body = read_body(body_path)
        result = try_template(sender_domain, body, subject)
        if result:
            update_extraction(conn, msg_hash, result)
            template_hits += 1
        else:
            result = try_generic(body, subject, sender_domain)
            if result:
                update_extraction(conn, msg_hash, result)
                regex_hits += 1
            else:
                residual.append((msg_hash, sender_domain, subject, body_path))
        if i % 500 == 0:
            conn.commit()
            print(f"  rules progress: {i}/{len(candidates)} (template={template_hits} regex={regex_hits} residual={len(residual)})")
    conn.commit()
    print(f"\nRule tiers complete: template={template_hits} regex={regex_hits} residual={len(residual)}")
    return residual


def run_llm(conn, residual):
    """Run LLM fallback on residual rows in batches."""
    BATCH = llm_fallback.BATCH_SIZE
    llm_hits = 0
    llm_none = 0  # llm said 'no charge'
    llm_fails = 0
    started = time.time()
    total = len(residual)

    for batch_start in range(0, total, BATCH):
        batch = residual[batch_start:batch_start + BATCH]
        prepared = []
        for msg_hash, sender_domain, subject, body_path in batch:
            prepared.append({
                "subject": subject or "",
                "sender_domain": sender_domain or "",
                "body": read_body(body_path),
            })

        results = llm_fallback.extract_batch(prepared)

        for (msg_hash, _, _, _), result in zip(batch, results):
            if result is None:
                # LLM call failed - leave extraction_method NULL so re-runs retry.
                llm_fails += 1
            elif result.get("amount_cents") is None:
                # LLM said this is not a charge
                update_extraction(conn, msg_hash, {
                    "amount_cents": None,
                    "merchant_norm": result.get("merchant_norm"),
                    "merchant_raw": None,
                    "txn_date": None,
                    "method": "none",
                    "confidence": result.get("confidence", 1.0),
                    "notes": result.get("notes") or "llm: no charge",
                })
                llm_none += 1
            else:
                update_extraction(conn, msg_hash, result)
                llm_hits += 1

        conn.commit()
        elapsed = time.time() - started
        done = min(batch_start + BATCH, total)
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  llm batch {done}/{total}: hits={llm_hits} no_charge={llm_none} fails={llm_fails} ({elapsed:.0f}s, eta {eta:.0f}s)")

    print(f"\nLLM tier complete: hits={llm_hits} no_charge={llm_none} fails={llm_fails}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reextract", action="store_true", help="Re-run extraction on rows with extraction_method already set.")
    ap.add_argument("--no-llm", action="store_true", help="Skip LLM tier; rule-based only.")
    ap.add_argument("--limit", type=int, default=None, help="Process only the first N candidate rows.")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found. Run 01_parse_mbox.py first.", file=sys.stderr)
        return 2

    conn = sqlite3.connect(DB_PATH)
    candidates = select_candidates(conn, reextract=args.reextract, limit=args.limit)
    print(f"Candidates to extract: {len(candidates)}")
    if not candidates:
        print("Nothing to do.")
        return 0

    print("\n=== Tier 1+2: rules ===")
    residual = run_rules(conn, candidates)

    if args.no_llm:
        print(f"\n--no-llm set; leaving {len(residual)} residual rows unextracted.")
    elif residual:
        print(f"\n=== Tier 3: LLM on {len(residual)} residuals ===")
        run_llm(conn, residual)

    print("\n=== final extraction_method distribution ===")
    cur = conn.cursor()
    for row in cur.execute("""
        SELECT extraction_method, COUNT(*), ROUND(AVG(extraction_confidence),3)
        FROM receipts
        WHERE body_path IS NOT NULL
        GROUP BY extraction_method
        ORDER BY 2 DESC
    """):
        print(f"  {str(row[0]):<30}  {row[1]:>5}  conf_avg={row[2]}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
