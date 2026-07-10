"""Generic dollar-amount extraction for receipts without a sender-specific template.

Tiered patterns by precision. Returns the highest-confidence match found.
"""

from __future__ import annotations
import re

# Highest-precision: explicit "Total" labels with dollar value adjacent.
# Uses word boundaries to avoid matching 'Subtotal', 'Pre-tax total', etc. where we want the GRAND total.
PATTERNS = [
    # (label, regex, confidence)
    ("grand_total", re.compile(r"(?:Grand\s+Total|Total\s+Charged|Amount\s+(?:Charged|Paid|Due))[:\s]*\$([\d,]+\.\d{2})", re.IGNORECASE), 0.85),
    ("order_total", re.compile(r"Order\s+Total[:\s]*\$([\d,]+\.\d{2})", re.IGNORECASE), 0.85),
    ("payment_total", re.compile(r"Payment\s+(?:Total|Amount)[:\s]*\$([\d,]+\.\d{2})", re.IGNORECASE), 0.80),
    ("total_paid", re.compile(r"Total\s+Paid[:\s]*\$([\d,]+\.\d{2})", re.IGNORECASE), 0.80),
    ("you_paid", re.compile(r"You\s+(?:paid|were\s+charged)[:\s]*\$([\d,]+\.\d{2})", re.IGNORECASE), 0.75),
    # Generic "Total: $X.XX" - watch out for 'Subtotal'
    ("total_labeled", re.compile(r"(?<!Sub)(?<!sub)\bTotal[:\s]*\$([\d,]+\.\d{2})", re.IGNORECASE), 0.70),
    # Last-resort: the largest dollar amount in the body (heuristic).
]


def derive_merchant_from_domain(domain: str) -> str:
    """Strip common email subdomains/suffixes to get a merchant guess."""
    if not domain:
        return ""
    parts = domain.split(".")
    # Drop TLD
    if len(parts) >= 2:
        parts = parts[:-1]
    # Drop common subdomain noise
    NOISE = {"mail", "email", "info", "no-reply", "noreply", "notifications",
             "notification", "updates", "messaging", "welcome", "alerts",
             "receipts", "support", "service"}
    parts = [p for p in parts if p.lower() not in NOISE]
    if not parts:
        return ""
    return parts[-1].lower()


def extract(body: str, subject: str, sender_domain: str = "") -> dict | None:
    best = None
    for label, pat, conf in PATTERNS:
        for m in pat.finditer(body):
            try:
                amount_cents = int(round(float(m.group(1).replace(",", "")) * 100))
            except ValueError:
                continue
            if amount_cents <= 0:
                continue
            # Prefer higher-confidence labels; within same label prefer the LAST occurrence
            # (totals usually appear at the end of receipts).
            if best is None or conf > best["confidence"] or (conf == best["confidence"]):
                best = {
                    "amount_cents": amount_cents,
                    "merchant_norm": derive_merchant_from_domain(sender_domain),
                    "merchant_raw": None,
                    "txn_date": None,
                    "confidence": conf,
                    "method": f"regex:{label}",
                    "notes": None,
                }
        if best and best["confidence"] >= conf:
            # If we found a higher-tier match, no need to fall through
            if best["confidence"] >= 0.80:
                break
    return best
