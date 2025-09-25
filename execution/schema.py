
from __future__ import annotations

import sqlite3
from execution.db import connect, query as db_query

SCHEMA_SCRIPT = """
BEGIN;

CREATE TABLE IF NOT EXISTS order_plans (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  signal_ref TEXT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,            -- 'buy' | 'sell'
  qty REAL NOT NULL,
  limit_px REAL,                 -- NULL for market/IOC
  tif TEXT DEFAULT 'IOC',        -- 'IOC' | 'GTC' | etc.
  reduce_only INTEGER DEFAULT 0, -- 0/1
  source TEXT DEFAULT 'manual',  -- 'manual' | 'auto-follow' | ...
  rule_ref TEXT,                 -- optional rule id
  risk_json TEXT DEFAULT '{}',   -- JSON string of risk config/snapshot
  status TEXT DEFAULT 'created', -- 'created'|'submitted'|'filled'|'canceled'|'error'
  sl_price REAL,                 -- optional stop loss trigger price
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exec_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  type TEXT NOT NULL,            -- 'submit'|'ack'|'fill'|'cancel'|'error'|'sl_trigger'
  ts TEXT NOT NULL,              -- ISO timestamp
  data_json TEXT,                -- optional payload
  FOREIGN KEY(plan_id) REFERENCES order_plans(id)
);

CREATE INDEX IF NOT EXISTS idx_exec_events_plan ON exec_events(plan_id);
CREATE INDEX IF NOT EXISTS idx_order_plans_status ON order_plans(status);
CREATE INDEX IF NOT EXISTS idx_order_plans_created ON order_plans(created_at);

COMMIT;
"""

def _column_exists(con: sqlite3.Connection, table: str, col: str) -> bool:
    rows = db_query(con, f"PRAGMA table_info({table})")
    return any(r.get("name") == col for r in rows)

def ensure_schema() -> None:
    con = connect()
    try:

        con.executescript(SCHEMA_SCRIPT)

        if not _column_exists(con, "order_plans", "idempotency_key"):
            con.execute("ALTER TABLE order_plans ADD COLUMN idempotency_key TEXT")
            con.commit()
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_idempo "
            "ON order_plans(idempotency_key)"
        )
        con.commit()

        con.execute("CREATE INDEX IF NOT EXISTS idx_order_plans_status ON order_plans(status)")
        con.commit()
    finally:
        con.close()

if __name__ == "__main__":
    ensure_schema()
