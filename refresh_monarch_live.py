"""Refresh the LIVE Monarch transaction cache in `Monarch Exports/live/`.

Pulls recent transactions through the authenticated Monarch API session (the one
the monarch-mcp-server holds — cookie login, lasts months) and writes them as a
single CSV in the SAME column shape as the manual Monarch web exports, so
`life-os/medtrack/medtrack.py match-monarch` (and any other consumer) can read
manual exports and live pulls identically.

Design constraints:
- Output goes to `Monarch Exports/live/` (a SUBFOLDER) so the receipts_pipeline's
  top-level CSV glob never sees it — the audited pipeline stays CSV-export-only.
- One file, overwritten each run (`monarch_live_pull.csv`): no accumulation, no
  cross-run duplicates. Consumers dedupe against the manual exports.
- MUST run under the monarch-mcp-server venv (has monarchmoney + the session):
    <monarch-mcp-server venv>/Scripts/python.exe refresh_monarch_live.py [--since YYYY-MM-DD]
"""

import argparse
import asyncio
import csv
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_SRC = os.path.normpath(os.path.join(
    HERE, "..", "mcp-extensions", "monarch-mcp-server", "src"))
sys.path.insert(0, SERVER_SRC)

OUT_DIR = os.path.join(HERE, "Monarch Exports", "live")
OUT_PATH = os.path.join(OUT_DIR, "monarch_live_pull.csv")
HEADER = ["Account Name", "Date", "Merchant", "Category", "Account",
          "Original Statement", "Notes", "Amount", "Tags", "Owner"]


async def pull(since):
    from monarch_mcp_server.client import get_monarch_client
    mm = await get_monarch_client()

    rows = []
    offset = 0
    page = 500
    while True:
        res = await mm.get_transactions(limit=page, offset=offset, start_date=since,
                                        end_date=date.today().isoformat())
        results = (res.get("allTransactions") or {}).get("results") or []
        for t in results:
            acct = ((t.get("account") or {}).get("displayName") or "").strip()
            rows.append({
                "Account Name": acct,
                "Date": t.get("date") or "",
                "Merchant": ((t.get("merchant") or {}).get("name") or "").strip(),
                "Category": ((t.get("category") or {}).get("name") or "").strip(),
                "Account": acct,
                "Original Statement": (t.get("plaidName") or "").strip(),
                "Notes": (t.get("notes") or "").strip(),
                "Amount": t.get("amount"),
                "Tags": ",".join(tg.get("name", "") for tg in (t.get("tags") or [])),
                "Owner": "",
            })
        if len(results) < page:
            break
        offset += page
    return rows


def main():
    p = argparse.ArgumentParser(description="Pull live Monarch transactions to CSV")
    p.add_argument("--since", default=None,
                   help="ISO start date (default: 120 days back — generous overlap "
                        "with the manual exports so nothing falls in a gap)")
    args = p.parse_args()
    since = args.since or (date.today() - timedelta(days=120)).isoformat()

    rows = asyncio.run(pull(since))
    if not rows:
        print(f"No transactions returned since {since} -- NOT overwriting the cache.")
        sys.exit(1)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    newest = max(r["Date"] for r in rows)
    oldest = min(r["Date"] for r in rows)
    print(f"Wrote {len(rows)} transactions ({oldest} .. {newest}) to {OUT_PATH}")


if __name__ == "__main__":
    main()
