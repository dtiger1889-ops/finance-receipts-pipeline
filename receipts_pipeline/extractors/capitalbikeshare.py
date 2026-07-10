"""Capital Bikeshare receipts. Top of body has 'Bike ride total: $X.XX Visa *NNNN'."""

from __future__ import annotations
import re

DOMAINS = {"updates.capitalbikeshare.com", "capitalbikeshare.com"}

RE_TOTAL = re.compile(
    r"Bike\s+ride\s+total:\s*\$([\d,]+\.\d{2})(?:\s+(Visa|Mastercard|Amex|Discover)\s*\*?(\d{4}))?",
    re.IGNORECASE,
)
# Free unlock + per-min fallback for older format
RE_TOTAL_ALT = re.compile(r"Total[:\s]*\$([\d,]+\.\d{2})", re.IGNORECASE)


def extract(body: str, subject: str) -> dict | None:
    m = RE_TOTAL.search(body)
    if m:
        amount_cents = int(round(float(m.group(1).replace(",", "")) * 100))
        card_last4 = m.group(3) if m.group(3) else None
    else:
        m = RE_TOTAL_ALT.search(body)
        if not m:
            return None
        amount_cents = int(round(float(m.group(1).replace(",", "")) * 100))
        card_last4 = None

    if amount_cents <= 0:
        return None

    notes_parts = []
    if card_last4:
        notes_parts.append(f"card_last4={card_last4}")
    bike_type = "ebike" if "$0.27 per min" in body or "per min (ebike)" in body.lower() else "bike"
    notes_parts.append(f"type={bike_type}")

    return {
        "amount_cents": amount_cents,
        "merchant_norm": "capital bikeshare",
        "merchant_raw": "Capital Bikeshare",
        "txn_date": None,
        "confidence": 0.95,
        "method": "template:capitalbikeshare",
        "notes": "; ".join(notes_parts),
    }
