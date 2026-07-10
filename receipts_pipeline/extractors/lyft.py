"""Lyft receipts (lyftmail.com). Two formats: legacy "Total charged to..." and modern card-line."""

from __future__ import annotations
import re

DOMAINS = {"lyftmail.com", "lyft.com"}

# Legacy: "Total charged to Visa ***2289: $3.77"
RE_TOTAL_CHARGED = re.compile(
    r"Total\s+charged\s+to\s+(?:Visa|Mastercard|American\s+Express|Amex|Discover)\s*[\*x]+(\d{4})\s*:\s*\$([\d,]+\.\d{2})",
    re.IGNORECASE,
)
# Modern: card line followed by amount. "American Express *1005 \n $10.85"
RE_CARD_AMOUNT = re.compile(
    r"(Visa|Mastercard|American\s+Express|Amex|Discover)\s*[\*x]+(\d{4})\s*\n+\s*\$([\d,]+\.\d{2})",
    re.IGNORECASE,
)
# Receipt #1234567890
RE_RECEIPT_NUM = re.compile(r"Receipt\s*#\s*(\d+)")


def extract(body: str, subject: str) -> dict | None:
    amount_cents = None
    card_last4 = None

    m = RE_TOTAL_CHARGED.search(body)
    if m:
        card_last4 = m.group(1)
        amount_cents = int(round(float(m.group(2).replace(",", "")) * 100))
    else:
        m = RE_CARD_AMOUNT.search(body)
        if m:
            card_last4 = m.group(2)
            amount_cents = int(round(float(m.group(3).replace(",", "")) * 100))

    if amount_cents is None or amount_cents <= 0:
        return None

    notes_parts = [f"card_last4={card_last4}"] if card_last4 else []
    rn = RE_RECEIPT_NUM.search(body)
    if rn:
        notes_parts.append(f"receipt={rn.group(1)}")

    return {
        "amount_cents": amount_cents,
        "merchant_norm": "lyft",
        "merchant_raw": "Lyft",
        "txn_date": None,
        "confidence": 0.95,
        "method": "template:lyft",
        "notes": "; ".join(notes_parts) if notes_parts else None,
    }
