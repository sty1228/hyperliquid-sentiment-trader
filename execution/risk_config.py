from __future__ import annotations

import sqlite3
from execution.db import connect, query as db_query

SCHEMA_SCRIPT = """
BEGIN;

DROP TABLE IF EXISTS risk_config;

CREATE TABLE risk_config (
    user_id TEXT NOT NULL,
    leader_username TEXT NOT NULL,
    symbol TEXT NOT NULL,
    tp_decimal_long REAL,
    sl_decimal_long REAL,
    tp_decimal_short REAL,
    sl_decimal_short REAL,
    PRIMARY KEY (user_id, leader_username, symbol)
);

CREATE INDEX IF NOT EXISTS idx_risk_user_symbol ON risk_config(user_id, leader_username, symbol);

COMMIT;
"""

def _column_exists(con: sqlite3.Connection, table: str, col: str) -> bool:
    rows = db_query(con, f"PRAGMA table_info({table})")
    return any(r.get("name") == col for r in rows)

def ensure_schema() -> None:
    con = connect()
    try:
        con.executescript(SCHEMA_SCRIPT)
        con.commit()
    finally:
        con.close()

if __name__ == "__main__":
    ensure_schema()
