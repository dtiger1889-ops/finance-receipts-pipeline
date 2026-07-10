"""
Phase A: mbox -> receipts skeleton + body files

Walks every .mbox file in the Gmail Takeout export, extracts metadata
(message-id, date, sender, subject), writes the message body to disk,
and inserts a skeleton row into the receipts table with amount fields NULL.

Idempotent: re-running with the same input produces the same DB state
(INSERT OR IGNORE on message_id_hash primary key).

Usage:
    python 01_parse_mbox.py                  # all 19 mbox files
    python 01_parse_mbox.py --only "Donation Receipts.mbox"  # one file
    python 01_parse_mbox.py --only "Financial-Retirement.mbox" "CredRep.mbox"
"""

from __future__ import annotations

import argparse
import hashlib
import mailbox
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PIPELINE_DIR.parent
MBOX_DIR = PROJECT_DIR / "2026-04-25 Gmail Financial Label Export" / "Mail"
DB_PATH = PIPELINE_DIR / "receipts.db"
BODIES_DIR = PIPELINE_DIR / "bodies"
SCHEMA_PATH = PIPELINE_DIR / "schema.sql"
LOCAL_TZ = ZoneInfo("America/New_York")
COMMIT_EVERY = 500


class _HTMLToText(HTMLParser):
    """Minimal HTML -> text converter. Skips script/style, adds newlines on block tags."""
    BLOCK_TAGS = {
        "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "thead", "tbody", "ul", "ol", "header", "footer", "section",
        "article", "blockquote", "pre", "hr",
    }
    # Skip the *contents* of these container tags. Void tags (meta, link, br, etc.)
    # have no content and no end tag - excluding them avoids a stuck _skip_depth.
    SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        text = "".join(self._chunks)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return parser.get_text()


def decode_str_header(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("latin-1", errors="replace")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def extract_body(msg) -> tuple[str, bool, bool]:
    """Return (text, has_html, has_text). Prefer text/plain; fall back to text/html stripped."""
    text_part = None
    html_part = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.is_multipart():
                continue
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain" and text_part is None:
                text_part = part
            elif ctype == "text/html" and html_part is None:
                html_part = part
    else:
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            text_part = msg
        elif ctype == "text/html":
            html_part = msg

    has_text = text_part is not None
    has_html = html_part is not None

    def _decode(part) -> str:
        if part is None:
            return ""
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                return ""
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        except Exception:
            return ""

    if text_part is not None:
        return _decode(text_part), has_html, has_text
    if html_part is not None:
        return html_to_text(_decode(html_part)), has_html, has_text
    return "", False, False


def stable_id(msg, fallback_seed: str) -> tuple[str, str]:
    """Return (message_id, message_id_hash). Falls back to deterministic hash if absent."""
    raw_id = msg.get("Message-ID") or msg.get("Message-Id") or ""
    raw_id = decode_str_header(raw_id).strip()
    if raw_id:
        h = hashlib.sha1(raw_id.encode("utf-8", errors="replace")).hexdigest()
        return raw_id, h
    h = hashlib.sha1(fallback_seed.encode("utf-8", errors="replace")).hexdigest()
    return "", h


def parse_date(raw) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None, None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        utc = dt.astimezone(timezone.utc)
        local = dt.astimezone(LOCAL_TZ)
        return utc.isoformat(), local.isoformat()
    except Exception:
        return None, None


def label_path_from_filename(stem: str) -> str:
    """Gmail Takeout encodes label hierarchy in filenames with '-' separators."""
    return stem.replace("-", "/")


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


def process_mbox(conn: sqlite3.Connection, mbox_path: Path) -> dict:
    label_path = label_path_from_filename(mbox_path.stem)
    mbox_file = mbox_path.name
    box = mailbox.mbox(str(mbox_path), factory=None, create=False)

    inserted = 0
    skipped_dup = 0
    skipped_no_date = 0
    errors = 0
    started = time.time()

    cur = conn.cursor()
    parsed_at = datetime.now(timezone.utc).isoformat()

    for i, msg in enumerate(box):
        try:
            subject = decode_str_header(msg.get("Subject", ""))
            from_raw = decode_str_header(msg.get("From", ""))
            sender_name, sender_email = parseaddr(from_raw)
            sender_email = (sender_email or "").lower().strip()
            sender_domain = sender_email.rsplit("@", 1)[-1] if "@" in sender_email else ""
            date_raw = msg.get("Date", "")
            sent_utc, sent_local = parse_date(date_raw)

            if sent_utc is None:
                skipped_no_date += 1
                continue

            body, has_html, has_text = extract_body(msg)
            body_sha1 = hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest() if body else None

            fallback_seed = f"{from_raw}|{date_raw}|{subject}|{body_sha1 or ''}"
            message_id, msg_hash = stable_id(msg, fallback_seed)

            body_path_rel = None
            if body:
                body_file = BODIES_DIR / f"{msg_hash}.txt"
                if not body_file.exists():
                    body_file.write_text(body, encoding="utf-8", errors="replace")
                body_path_rel = f"bodies/{msg_hash}.txt"

            cur.execute(
                """
                INSERT OR IGNORE INTO receipts (
                    message_id_hash, message_id, mbox_file, label_path,
                    sent_date, sent_date_local,
                    sender_email, sender_domain,
                    subject, body_path, body_sha1,
                    has_html, has_text, parsed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_hash, message_id, mbox_file, label_path,
                    sent_utc, sent_local,
                    sender_email, sender_domain,
                    subject, body_path_rel, body_sha1,
                    1 if has_html else 0, 1 if has_text else 0,
                    parsed_at,
                ),
            )
            if cur.rowcount == 0:
                skipped_dup += 1
            else:
                inserted += 1

            if (inserted + skipped_dup) % COMMIT_EVERY == 0:
                conn.commit()

        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  [error] msg #{i} in {mbox_file}: {e}", file=sys.stderr)

    conn.commit()
    elapsed = time.time() - started
    return {
        "mbox": mbox_file,
        "inserted": inserted,
        "duplicates": skipped_dup,
        "no_date": skipped_no_date,
        "errors": errors,
        "elapsed_s": round(elapsed, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="Process only the specified mbox filenames (basenames).")
    args = ap.parse_args()

    if not MBOX_DIR.is_dir():
        print(f"ERROR: mbox dir not found: {MBOX_DIR}", file=sys.stderr)
        return 2

    BODIES_DIR.mkdir(parents=True, exist_ok=True)

    all_mbox = sorted(MBOX_DIR.glob("*.mbox"))
    if args.only:
        wanted = set(args.only)
        all_mbox = [p for p in all_mbox if p.name in wanted]
        if not all_mbox:
            print(f"ERROR: none of --only files matched. Available: "
                  f"{[p.name for p in sorted(MBOX_DIR.glob('*.mbox'))]}", file=sys.stderr)
            return 2

    conn = init_db()
    print(f"DB:     {DB_PATH}")
    print(f"Bodies: {BODIES_DIR}")
    print(f"Files:  {len(all_mbox)}")
    print()

    totals = {"inserted": 0, "duplicates": 0, "no_date": 0, "errors": 0, "elapsed_s": 0.0}
    for path in all_mbox:
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"-> {path.name} ({size_mb:.1f} MB)")
        stats = process_mbox(conn, path)
        print(f"   inserted={stats['inserted']} dup={stats['duplicates']} "
              f"no_date={stats['no_date']} errors={stats['errors']} "
              f"({stats['elapsed_s']}s)")
        for k in ("inserted", "duplicates", "no_date", "errors"):
            totals[k] += stats[k]
        totals["elapsed_s"] += stats["elapsed_s"]

    conn.close()
    print()
    print(f"TOTAL inserted={totals['inserted']} dup={totals['duplicates']} "
          f"no_date={totals['no_date']} errors={totals['errors']} "
          f"({round(totals['elapsed_s'], 1)}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
