"""Amazon email receipts (order confirmations + ship notifications + refunds).

Order Total only appears on order *confirmations*, not shipment emails.
For non-confirmation emails (shipped/delivered/cancelled), this extractor returns None
so they fall through to generic regex / LLM.
"""

from __future__ import annotations
import re

DOMAINS = {"amazon.com", "amazon.co.uk", "amazon.ca"}

RE_ORDER_TOTAL = re.compile(r"Order\s+Total:\s*\$([\d,]+\.\d{2})", re.IGNORECASE)
RE_REFUND_AMOUNT = re.compile(r"Refund\s+total:\s*\$([\d,]+\.\d{2})", re.IGNORECASE)
RE_ORDER_ID = re.compile(r"Order\s*#\s*([\d\-]+)", re.IGNORECASE)
RE_GRAND_TOTAL = re.compile(r"Grand\s+total[:\s]*\$([\d,]+\.\d{2})", re.IGNORECASE)


def extract(body: str, subject: str) -> dict | None:
    is_refund = False
    m = RE_ORDER_TOTAL.search(body) or RE_GRAND_TOTAL.search(body)
    if not m:
        m = RE_REFUND_AMOUNT.search(body)
        if m:
            is_refund = True
        else:
            return None

    amount_cents = int(round(float(m.group(1).replace(",", "")) * 100))
    if amount_cents <= 0:
        return None
    if is_refund:
        amount_cents = -amount_cents

    notes_parts = []
    oid = RE_ORDER_ID.search(subject or "") or RE_ORDER_ID.search(body)
    if oid:
        notes_parts.append(f"order_id={oid.group(1)}")
    if is_refund:
        notes_parts.append("refund=true")

    return {
        "amount_cents": amount_cents,
        "merchant_norm": "amazon",
        "merchant_raw": "Amazon",
        "txn_date": None,
        "confidence": 0.95,
        "method": "template:amazon_email",
        "notes": "; ".join(notes_parts) if notes_parts else None,
    }
