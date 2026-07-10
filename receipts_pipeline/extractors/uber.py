"""Uber receipts (uber.com). Covers Uber rides, Lime scooters/bikes routed via Uber."""

from __future__ import annotations
import re

DOMAINS = {"uber.com", "uber.us"}

# "Total$5.22" or "Total\n$5.22" -- HTML->text strips whitespace inconsistently
RE_TOTAL = re.compile(r"\bTotal\s*\$([\d,]+\.\d{2})", re.IGNORECASE)
# "csp ••••3386" or "Visa ••••3386" -- card last-4 hint (• is U+2022)
RE_CARD = re.compile(r"(?:Visa|Mastercard|Amex|Discover|csp)\s*[\*•]+(\d{4})", re.IGNORECASE)
# Subject: "Your Tuesday morning scooter ride with Lime", "Your Monday trip with Uber"
RE_SUBMERCHANT = re.compile(r"(?:trip|ride|delivery)\s+with\s+([A-Za-z]+)", re.IGNORECASE)
# Body date: "Apr 14, 2026" or "November 7, 2024"
RE_DATE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),\s+(20\d{2})\b"
)

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def extract(body: str, subject: str) -> dict | None:
    m = RE_TOTAL.search(body)
    if not m:
        return None
    amount_cents = int(round(float(m.group(1).replace(",", "")) * 100))
    if amount_cents <= 0:
        return None

    sub = RE_SUBMERCHANT.search(subject or "")
    submerchant = sub.group(1).lower() if sub else None
    merchant_raw = f"Uber ({submerchant})" if submerchant and submerchant != "uber" else "Uber"

    card = RE_CARD.search(body)
    notes_parts = []
    if submerchant and submerchant != "uber":
        notes_parts.append(f"submerchant={submerchant}")
    if card:
        notes_parts.append(f"card_last4={card.group(1)}")

    txn_date = None
    d = RE_DATE.search(body)
    if d:
        try:
            mon = MONTHS[d.group(1).lower()]
            day = int(d.group(2))
            year = int(d.group(3))
            txn_date = f"{year:04d}-{mon:02d}-{day:02d}"
        except (KeyError, ValueError):
            pass

    return {
        "amount_cents": amount_cents,
        "merchant_norm": "uber",
        "merchant_raw": merchant_raw,
        "txn_date": txn_date,
        "confidence": 0.95,
        "method": "template:uber",
        "notes": "; ".join(notes_parts) if notes_parts else None,
    }
