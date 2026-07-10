import os
import re
import csv
from datetime import datetime

import ollama
from pypdf import PdfReader


# ======================
# CONFIG
# ======================
INPUT_FOLDER = os.path.join(os.path.dirname(__file__), "process_pdf_here")
OUTPUT_CSV = "all_transactions.csv"
MODEL_NAME = "qwen2.5:7b"
SKIP_COVER_PAGES = True  # Skip first and last pages — cover letter / blank back page
BATCH_SIZE = 20          # Merchant enrichment batch size for Ollama


# ======================
# DEBIT / CREDIT CLASSIFICATION
# ======================
DEBIT_LABELS = {
    "Electronic Withdrawal",
    "ATM Withdrawal",
    "Visa Debit Card Point of Sale Purchase",
    "Check Paid",
    "P2P Zelle Debit",
}
CREDIT_LABELS = {
    "Electronic Deposit",
    "Deposit Mobile Banking",
    "ATM Fee Rebate",
    "Interest Paid",
    "Interest Earned",
    "Direct Deposit",
    "Mobile Banking",
}

GENERIC_LABELS = sorted(
    DEBIT_LABELS | CREDIT_LABELS | {"Money Transfer"},
    key=len,
    reverse=True,
)

CREDIT_KEYWORDS = ["payroll", "cashout", "tax ref", "refund", "rebate", "interest", "deposit"]
DEBIT_KEYWORDS  = ["payment", "epay", "ownerdraft", "echeck", "withdrawal", "zelle to"]


def _match_generic_label(text):
    for label in GENERIC_LABELS:
        if text.lower().startswith(label.lower()):
            remainder = text[len(label):].strip()
            return label, remainder
    return None, text


def _classify_sign(raw_description, label, raw_value):
    """Return (debit, credit) strings. raw_value is always a positive float string."""
    if label in DEBIT_LABELS:
        return raw_value, ""
    if label in CREDIT_LABELS:
        return "", raw_value
    desc_lower = raw_description.lower()
    for kw in CREDIT_KEYWORDS:
        if kw in desc_lower:
            return "", raw_value
    for kw in DEBIT_KEYWORDS:
        if kw in desc_lower:
            return raw_value, ""
    return raw_value, ""  # default: debit


# ======================
# DATA MODEL
# ======================
class Transaction:
    def __init__(self, date, raw_description, debit, credit, source_file):
        self.date = date
        self.raw_description = raw_description  # verbatim PDF text → Notes column
        self.merchant = raw_description          # overwritten by LLM enrichment
        self.debit = debit    # positive float string or ""
        self.credit = credit  # positive float string or ""
        self.source_file = source_file

    @property
    def amount(self):
        if self.debit:
            return str(-float(self.debit))
        if self.credit:
            return self.credit
        return ""

    def to_row(self):
        return [
            self.date,
            self.merchant,
            self.debit,
            self.credit,
            self.amount,
            self.raw_description,
        ]


# ======================
# INGESTION
# ======================
def load_pdfs(folder):
    for filename in sorted(os.listdir(folder)):
        if filename.lower().endswith(".pdf"):
            try:
                year = filename.split("_")[1].split("-")[0]
            except (IndexError, ValueError):
                year = None
            yield filename, os.path.join(folder, filename), year


# ======================
# EXTRACTION
# ======================
def extract_pages(file_path):
    reader = PdfReader(file_path)
    pages = reader.pages
    if SKIP_COVER_PAGES and len(pages) > 2:
        pages = pages[1:-1]
    for i, page in enumerate(pages):
        text = page.extract_text(extraction_mode="layout") or ""
        yield i, text


# ======================
# CLASSIFICATION
# ======================
def is_transaction_page(text):
    indicators = [
        "date", "description", "amount", "balance",
        "withdrawal", "deposit", "transaction", "debit", "credit",
    ]
    score = sum(1 for k in indicators if k in text.lower())
    return score >= 2


# ======================
# RULE-BASED PARSER
# ======================
DATE_REGEX = r"\b\d{2}/\d{2}(?:/\d{4})?\b"
AMOUNT_REGEX = r"-?\$?\d{1,3}(?:,\d{3})*\.\d{2}"


def _clean_amount(raw):
    return raw.replace("$", "").replace(",", "").lstrip("-")


def parse_transactions_rule_based(text, source_file, year=None):
    transactions = []
    lines = text.split("\n")

    for idx, line in enumerate(lines):
        date_match = re.search(DATE_REGEX, line)
        if not date_match:
            continue

        date = date_match.group()
        suffix = line[date_match.end():]

        first_amt_match = re.search(AMOUNT_REGEX, suffix)
        if not first_amt_match:
            continue

        raw_description = suffix[: first_amt_match.start()].strip()

        desc_lower = raw_description.lower()
        if "annual percentage yield" in desc_lower or "balance" in desc_lower:
            continue

        amounts = re.findall(AMOUNT_REGEX, suffix)
        if not amounts:
            continue

        # Last amount is typically the running balance — skip when 2+ present
        transaction_amounts = amounts[:-1] if len(amounts) >= 2 else amounts
        if not transaction_amounts:
            continue

        raw_value = _clean_amount(transaction_amounts[0])
        try:
            float(raw_value)
        except ValueError:
            continue

        # Resolve generic label + look ahead for merchant continuation
        label, remainder = _match_generic_label(raw_description)
        if label:
            if remainder:
                raw_description = f"{label} {remainder}"
            else:
                next_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
                if (
                    next_line
                    and not re.search(DATE_REGEX, next_line)
                    and not re.search(AMOUNT_REGEX, next_line)
                ):
                    raw_description = f"{label} {next_line}"

        debit, credit = _classify_sign(raw_description, label or "", raw_value)

        transactions.append(
            Transaction(date, raw_description, debit, credit, source_file)
        )

    return transactions


# ======================
# LLM MERCHANT ENRICHMENT (OLLAMA)
# ======================
def _call_ollama(prompt, retries=3):
    for attempt in range(retries):
        try:
            response = ollama.generate(model=MODEL_NAME, prompt=prompt)
            return response["response"]
        except Exception:
            if attempt == retries - 1:
                raise
            print(f"  Retrying Ollama call ({attempt + 1})...")


def enrich_merchants(transactions):
    """Batch-call Ollama to convert raw descriptions into clean merchant names."""
    print(f"Enriching {len(transactions)} merchant names with {MODEL_NAME}...")
    for i in range(0, len(transactions), BATCH_SIZE):
        batch = transactions[i : i + BATCH_SIZE]
        numbered = "\n".join(
            f"{j + 1}. {t.raw_description}" for j, t in enumerate(batch)
        )
        prompt = f"""Convert each bank transaction description into a short clean merchant name.
Return ONLY a numbered list. One name per line. No explanations.
Examples:
  "VENMO PAYMENT 220414" → "Venmo"
  "EXAMPLECORP MTG MORTG PYMT 040122" → "Examplecorp Mortgage"
  "ACME UNIVERSITY PAYROLL 220412" → "Acme University Payroll"
  "UTILITYCO PAYMENTUS BILLPAY 220901" → "UtilityCo"
  "BANKCO CREDIT CRD EPAY 220420" → "BankCo Credit Card"
  "Electronic Withdrawal EXAMPLE CONDO HOA OWNERDRAFT 220705" → "Example Condo HOA"
  "ATM Fee Rebate" → "ATM Fee Rebate"
  "Interest Paid" → "Interest"

{numbered}

/no_think"""
        try:
            raw = _call_ollama(prompt)
            lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
            for j, line in enumerate(lines[: len(batch)]):
                name = re.sub(r"^\d+[\.\)]\s*", "", line).strip()
                if name:
                    batch[j].merchant = name
        except Exception as e:
            print(f"  Enrichment batch {i // BATCH_SIZE + 1} failed: {e} — using raw description")

        print(f"  Enriched {min(i + BATCH_SIZE, len(transactions))}/{len(transactions)}")


# ======================
# NORMALIZATION
# ======================
def normalize(txn, year=None):
    try:
        txn.date = datetime.strptime(txn.date, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        try:
            if year:
                txn.date = datetime.strptime(f"{txn.date}/{year}", "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    txn.raw_description = txn.raw_description.replace(",", " ").strip()
    txn.merchant = txn.merchant.replace(",", " ").strip()

    return txn


# ======================
# VALIDATION
# ======================
def is_valid(txn):
    if not txn.date or not txn.raw_description:
        return False
    if not txn.debit and not txn.credit:
        return False
    try:
        if txn.debit:
            float(txn.debit)
        if txn.credit:
            float(txn.credit)
    except ValueError:
        return False
    return True


# ======================
# DEDUPLICATION
# ======================
def deduplicate(transactions):
    seen = set()
    unique = []
    for txn in transactions:
        key = (txn.date, txn.raw_description, txn.debit, txn.credit)
        if key not in seen:
            seen.add(key)
            unique.append(txn)
    return unique


# ======================
# OUTPUT
# ======================
def write_csv(transactions, output_file):
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "Merchant Name", "Debit", "Credit", "Amount", "Notes"])
        for txn in transactions:
            writer.writerow(txn.to_row())


# ======================
# MAIN PIPELINE
# ======================
def main():
    all_transactions = []

    for filename, file_path, year in load_pdfs(INPUT_FOLDER):
        print(f"Processing: {filename}")
        file_transactions = []

        try:
            for page_num, text in extract_pages(file_path):
                if not text.strip():
                    continue
                if not is_transaction_page(text):
                    continue

                print(f"  - Page {page_num + 1}")
                txns = parse_transactions_rule_based(text, filename, year)

                for txn in txns:
                    txn = normalize(txn, year)
                    if is_valid(txn):
                        file_transactions.append(txn)

        except Exception as e:
            print(f"Error processing {filename}: {e}")

        print(f"  > {len(file_transactions)} transactions found")
        all_transactions.extend(file_transactions)

    print("Deduplicating...")
    all_transactions = deduplicate(all_transactions)

    enrich_merchants(all_transactions)

    print("Sorting by date...")
    all_transactions.sort(key=lambda t: t.date)

    print(f"Writing {len(all_transactions)} transactions to CSV...")
    write_csv(all_transactions, OUTPUT_CSV)

    print(f"Done! Output: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
