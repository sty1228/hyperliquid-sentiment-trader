
from __future__ import annotations
import os, sqlite3
from typing import Any, Iterable, Optional
from backend.config import load_env, env

load_env()
DATA_DIR = env("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "execution.sqlite")
# DB_PATH = os.path.join(DATA_DIR, "crypto_tracker.db")

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def exec(con: sqlite3.Connection, sql: str, params: Iterable[Any] = ()):
    cur = con.cursor()
    cur.execute(sql, params)
    con.commit()
    return cur

def query(con: sqlite3.Connection, sql: str, params: Iterable[Any] = ()):
    cur = con.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    return [dict(r) for r in rows]

def scalar(con: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Optional[Any]:
    cur = con.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None
