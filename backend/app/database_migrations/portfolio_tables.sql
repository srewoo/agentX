-- Portfolio analytics tables.
-- Idempotent: safe to run on every boot. Forward-only; no destructive DDL.
--
-- Owns three tables:
--   holdings      — open + closed equity positions (long-term, audit trail)
--   transactions  — append-only ledger of fills (manual or paper-trade synced)
--   benchmarks    — optional cache for Nifty/Sensex daily closes (beta calc)
--
-- All timestamps are ISO-8601 UTC strings to match the rest of the schema.

CREATE TABLE IF NOT EXISTS holdings (
    id          TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    exchange    TEXT NOT NULL DEFAULT 'NSE',
    qty         REAL NOT NULL,
    avg_price   REAL NOT NULL,
    opened_at   TEXT NOT NULL,
    closed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_holdings_symbol  ON holdings(symbol);
CREATE INDEX IF NOT EXISTS idx_holdings_open    ON holdings(closed_at) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS transactions (
    id      TEXT PRIMARY KEY,
    ts      TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    side    TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty     REAL NOT NULL CHECK (qty > 0),
    price   REAL NOT NULL CHECK (price >= 0),
    fees    REAL NOT NULL DEFAULT 0,
    notes   TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_ts     ON transactions(ts DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS benchmarks (
    date          TEXT PRIMARY KEY,   -- YYYY-MM-DD
    nifty_close   REAL,
    sensex_close  REAL
);
