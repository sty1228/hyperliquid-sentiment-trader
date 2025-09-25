
import os
import sqlite3
from datetime import datetime

DB_PATH = None
try:
    import sys
    sys.path.append(os.path.abspath("."))
    from backend.config import load_env, env, get_db_path
    load_env()
    DB_PATH = get_db_path(env("DB_PATH", os.path.join(env("DATA_DIR", "data"), "crypto_tracker.db")))
except Exception:
    pass

if not DB_PATH:
    DATA_DIR = os.environ.get("DATA_DIR", "data")
    os.makedirs(DATA_DIR, exist_ok=True)
    DB_PATH = os.path.join(DATA_DIR, "crypto_tracker.db")

print(f"Using database: {DB_PATH}")

def table_has_column(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

def table_exists(cur, table):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table,))
    return cur.fetchone() is not None

def create_index_if_missing(cur, name, table, cols, unique=False):
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?;", (name,))
    if cur.fetchone() is None:
        cur.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {table} ({cols});")

def add_column_if_missing(cur, table, col, col_def):
    if not table_has_column(cur, table, col):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def};")
        print(f"  ✓ {table}.{col} added")

def ensure_tweets_columns(cur):
    if not table_exists(cur, "tweets"):
        raise SystemExit("Table 'tweets' not found. Run your initial DB setup first.")
    print("Ensuring tweets columns...")
    add_column_if_missing(cur, "tweets", "is_call", "INTEGER DEFAULT 0")  # 0/1
    add_column_if_missing(cur, "tweets", "action", "TEXT DEFAULT NULL")   # 'long'|'short'|'none'
    add_column_if_missing(cur, "tweets", "confidence", "REAL DEFAULT NULL")
    add_column_if_missing(cur, "tweets", "signal_id", "TEXT DEFAULT NULL")
    create_index_if_missing(cur, "idx_tweets_signal_id", "tweets", "signal_id", unique=False)
    create_index_if_missing(cur, "idx_tweets_username_time", "tweets", "username, tweet_time", unique=False)
    create_index_if_missing(cur, "idx_tweets_ticker", "tweets", "ticker", unique=False)

def ensure_performance_horizons(cur):
    if not table_exists(cur, "performance_horizons"):
        print("Creating performance_horizons...")
        cur.execute("""
        CREATE TABLE performance_horizons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tweet_id INTEGER NOT NULL,
            horizon_h INTEGER NOT NULL,
            ret_close REAL,
            ret_high REAL,
            ret_low REAL,
            ret_close_alpha REAL,
            computed_at TEXT,
            method_version TEXT,
            UNIQUE(tweet_id, horizon_h)
        );
        """)
        create_index_if_missing(cur, "idx_ph_tweet_h", "performance_horizons", "tweet_id, horizon_h", unique=True)
        return

    print("Ensuring performance_horizons columns...")
    add_column_if_missing(cur, "performance_horizons", "tweet_id", "INTEGER")
    add_column_if_missing(cur, "performance_horizons", "horizon_h", "INTEGER")
    add_column_if_missing(cur, "performance_horizons", "ret_close", "REAL")
    add_column_if_missing(cur, "performance_horizons", "ret_high", "REAL")
    add_column_if_missing(cur, "performance_horizons", "ret_low", "REAL")
    add_column_if_missing(cur, "performance_horizons", "ret_close_alpha", "REAL")
    add_column_if_missing(cur, "performance_horizons", "computed_at", "TEXT")
    add_column_if_missing(cur, "performance_horizons", "method_version", "TEXT")

    create_index_if_missing(cur, "idx_ph_tweet_h", "performance_horizons", "tweet_id, horizon_h", unique=True)

def ensure_user_daily_stats(cur):
    if table_exists(cur, "user_daily_stats"):
        print("Ensuring user_daily_stats columns/indexes...")
        needed = {
            "username": "TEXT NOT NULL",
            "date": "TEXT NOT NULL",
            "calls": "INTEGER DEFAULT 0",
            "wins": "INTEGER DEFAULT 0",
            "losses": "INTEGER DEFAULT 0",
            "median24h": "REAL DEFAULT NULL",
            "q25_24h": "REAL DEFAULT NULL",
            "results_pct_cum": "REAL DEFAULT NULL",
            "streak": "INTEGER DEFAULT 0",
            "updated_at": "TEXT DEFAULT NULL"
        }
        for col, typedef in needed.items():
            add_column_if_missing(cur, "user_daily_stats", col, typedef)
    else:
        print("Creating user_daily_stats...")
        cur.execute("""
        CREATE TABLE user_daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            date TEXT NOT NULL,
            calls INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            median24h REAL DEFAULT NULL,
            q25_24h REAL DEFAULT NULL,
            results_pct_cum REAL DEFAULT NULL,
            streak INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT NULL,
            UNIQUE(username, date)
        );
        """)
    create_index_if_missing(cur, "idx_uds_user_date", "user_daily_stats", "username, date", unique=True)

def ensure_leaderboard_cache(cur):
    if table_exists(cur, "leaderboard_cache"):
        print("Ensuring leaderboard_cache columns/indexes...")
        needed = {
            "username": "TEXT NOT NULL",
            "window_h": "INTEGER NOT NULL", 
            "median24h": "REAL DEFAULT NULL",
            "hit_rate": "REAL DEFAULT NULL",
            "calls": "INTEGER DEFAULT 0",
            "q25_24h": "REAL DEFAULT NULL",
            "signal_noise": "REAL DEFAULT NULL",
            "profit_grade": "TEXT DEFAULT NULL",  
            "points": "REAL DEFAULT 0",
            "score": "REAL DEFAULT NULL",
            "rank": "INTEGER DEFAULT NULL",
            "updated_at": "TEXT DEFAULT NULL"
        }
        for col, typedef in needed.items():
            add_column_if_missing(cur, "leaderboard_cache", col, typedef)
    else:
        print("Creating leaderboard_cache...")
        cur.execute("""
        CREATE TABLE leaderboard_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            window_h INTEGER NOT NULL,
            median24h REAL DEFAULT NULL,
            hit_rate REAL DEFAULT NULL,
            calls INTEGER DEFAULT 0,
            q25_24h REAL DEFAULT NULL,
            signal_noise REAL DEFAULT NULL,
            profit_grade TEXT DEFAULT NULL,
            points REAL DEFAULT 0,
            score REAL DEFAULT NULL,
            rank INTEGER DEFAULT NULL,
            updated_at TEXT DEFAULT NULL,
            UNIQUE(username, window_h)
        );
        """)
    create_index_if_missing(cur, "idx_lb_user_win", "leaderboard_cache", "username, window_h", unique=True)
    create_index_if_missing(cur, "idx_lb_rank", "leaderboard_cache", "window_h, rank", unique=False)
    create_index_if_missing(cur, "idx_lb_score", "leaderboard_cache", "window_h, score", unique=False)

def main():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()

        print("\n=== Migrating schema ===")
        ensure_tweets_columns(cur)
        ensure_performance_horizons(cur)
        ensure_user_daily_stats(cur)
        ensure_leaderboard_cache(cur)

        conn.commit()
        print(f"Migration complete at {datetime.utcnow().isoformat()}Z")

if __name__ == "__main__":
    main()
