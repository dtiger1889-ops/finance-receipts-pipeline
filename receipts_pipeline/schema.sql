-- Receipts pipeline schema
-- SQLite source of truth for the Gmail receipts -> Monarch -> Amazon database.
-- Original source files (mbox, Monarch CSVs, Amazon CSV) are read-only and never modified.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS receipts (
  message_id_hash       TEXT PRIMARY KEY,
  message_id            TEXT,
  mbox_file             TEXT NOT NULL,
  label_path            TEXT NOT NULL,
  sent_date             TEXT NOT NULL,
  sent_date_local       TEXT,
  sender_email          TEXT,
  sender_domain         TEXT,
  subject               TEXT,
  body_path             TEXT,
  body_sha1             TEXT,
  has_html              INTEGER,
  has_text              INTEGER,
  amount_cents          INTEGER,
  currency              TEXT DEFAULT 'USD',
  merchant_norm         TEXT,
  merchant_raw          TEXT,
  txn_date              TEXT,
  extraction_method     TEXT,
  extraction_confidence REAL,
  extraction_notes      TEXT,
  parsed_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_receipts_date    ON receipts(sent_date_local);
CREATE INDEX IF NOT EXISTS idx_receipts_amount  ON receipts(amount_cents);
CREATE INDEX IF NOT EXISTS idx_receipts_merch   ON receipts(merchant_norm);
CREATE INDEX IF NOT EXISTS idx_receipts_domain  ON receipts(sender_domain);
CREATE INDEX IF NOT EXISTS idx_receipts_label   ON receipts(label_path);

CREATE TABLE IF NOT EXISTS monarch_transactions (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  account_name        TEXT NOT NULL,
  txn_date            TEXT NOT NULL,
  merchant            TEXT,
  merchant_norm       TEXT,
  category            TEXT,
  original_statement  TEXT,
  notes               TEXT,
  amount_cents        INTEGER NOT NULL,
  tags                TEXT,
  owner               TEXT,
  source_file         TEXT
);
CREATE INDEX IF NOT EXISTS idx_monarch_date   ON monarch_transactions(txn_date);
CREATE INDEX IF NOT EXISTS idx_monarch_amount ON monarch_transactions(amount_cents);
CREATE INDEX IF NOT EXISTS idx_monarch_merch  ON monarch_transactions(merchant_norm);

CREATE TABLE IF NOT EXISTS amazon_orders (
  order_id            TEXT,
  line_no             INTEGER,
  order_date          TEXT,
  ship_date           TEXT,
  product_name        TEXT,
  unit_price_cents    INTEGER,
  total_amount_cents  INTEGER,
  payment_method      TEXT,
  merchant            TEXT,
  source              TEXT NOT NULL,
  raw_row             TEXT,
  PRIMARY KEY(order_id, line_no, source)
);
CREATE INDEX IF NOT EXISTS idx_amazon_date   ON amazon_orders(order_date);
CREATE INDEX IF NOT EXISTS idx_amazon_amount ON amazon_orders(total_amount_cents);

CREATE TABLE IF NOT EXISTS matches (
  match_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_id          TEXT REFERENCES receipts(message_id_hash),
  monarch_id          INTEGER REFERENCES monarch_transactions(id),
  amazon_order_id     TEXT,
  match_type          TEXT,
  date_delta_days     INTEGER,
  amount_delta_cents  INTEGER,
  merchant_similarity REAL,
  confidence          REAL,
  reviewed            INTEGER DEFAULT 0,
  notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_matches_receipt ON matches(receipt_id);
CREATE INDEX IF NOT EXISTS idx_matches_monarch ON matches(monarch_id);
