
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

try:
    from backend.config import get_db_path, load_env
except Exception: 
    get_db_path = lambda *_args, **_kwargs: str(Path(__file__).resolve().parents[2] / "data" / "crypto_tracker.db")
    def load_env():
        return None

load_env()

ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class EnhancedPriceDatabase:


    def __init__(self, db_path: Optional[str] = None):

        path_str = db_path or get_db_path()
        self.db_path = str(Path(path_str).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.init_database()


    def _connect(self) -> sqlite3.Connection:

        db_uri = f"file:{Path(self.db_path).as_posix()}?cache=shared"
        conn = sqlite3.connect(
            db_uri,
            uri=True,
            timeout=30,            
            check_same_thread=False, 
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")  
        return conn

    def _convert_timestamp_to_string(self, timestamp: Any) -> Optional[str]:
        if timestamp is None:
            return None
        try:
            if isinstance(timestamp, str):
                try:
                    pd.to_datetime(timestamp)
                    return timestamp
                except Exception:
                    return datetime.now(timezone.utc).isoformat()
            if hasattr(timestamp, "to_pydatetime"):
                return timestamp.to_pydatetime().isoformat()
            if isinstance(timestamp, datetime):
                return timestamp.isoformat()
            try:
                parsed_dt = pd.to_datetime(timestamp)
                if hasattr(parsed_dt, "to_pydatetime"):
                    return parsed_dt.to_pydatetime().isoformat()
                return str(parsed_dt)
            except Exception:
                return datetime.now(timezone.utc).isoformat()
        except Exception as e:
            logging.error(f"Error converting timestamp {timestamp} (type: {type(timestamp)}): {e}")
            return datetime.now(timezone.utc).isoformat()

    def _clean_text(self, text: Any) -> str:
        if text is None:
            return ""
        s = str(text)
        s = s.replace("\x00", "").replace("\0", "")
        max_len = 10000
        if len(s) > max_len:
            s = s[:max_len] + "..."
        return s.strip()


    # Schema
    def init_database(self) -> None:
        with self._connect() as conn:
            cur = conn.cursor()

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tweets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    tweet_text TEXT NOT NULL,
                    tweet_time TEXT NOT NULL,
                    ticker TEXT,
                    sentiment TEXT,
                    entry_price REAL,
                    current_price REAL,
                    price_change_percent REAL,
                    last_updated TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(username, tweet_text, tweet_time)
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    market_type TEXT DEFAULT 'spot',
                    volume REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS performance_tracking (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id INTEGER,
                    ticker TEXT NOT NULL,
                    username TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    current_price REAL,
                    max_price REAL,
                    min_price REAL,
                    price_change_percent REAL,
                    hours_since_tweet INTEGER,
                    tweet_time TEXT NOT NULL,
                    last_updated TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tweet_id) REFERENCES tweets (id)
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS performance_horizons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id INTEGER NOT NULL,
                    horizon_h INTEGER NOT NULL,
                    ret_close REAL,
                    ret_high REAL,
                    ret_low REAL,
                    ret_close_alpha REAL,
                    computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tweet_id, horizon_h),
                    FOREIGN KEY (tweet_id) REFERENCES tweets(id)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_perf_horizon_tid ON performance_horizons(tweet_id)")

            # Indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tweets_ticker ON tweets (ticker)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tweets_sentiment ON tweets (sentiment)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tweets_time ON tweets (tweet_time)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_price_symbol ON price_history (symbol)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_price_timestamp ON price_history (timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_performance_ticker ON performance_tracking (ticker)")

            conn.commit()


    def _select_tweet_id(self, username: str, tweet_text: str, tweet_time: str) -> Optional[int]:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id FROM tweets
                WHERE username = ? AND tweet_text = ? AND tweet_time = ?
                """,
                (username, tweet_text, tweet_time),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None

    def insert_tweet(
        self,
        username: str,
        tweet_text: str,
        tweet_time: Any,
        ticker: Optional[str],
        sentiment: Optional[str],
        entry_price: Optional[float] = None,
    ) -> Optional[int]:
        """
        Safe upsert strategy:
          1) INSERT OR IGNORE to keep the original PK
          2) If existed, UPDATE only NULL/empty fields (don't overwrite non-null values)
        Returns row id (new or existing)
        """
        try:
            clean_username = self._clean_text(username)
            clean_tweet_text = self._clean_text(tweet_text)
            clean_ticker = self._clean_text(ticker) if ticker else None
            clean_sentiment = self._clean_text(sentiment) if sentiment else None
            converted_tweet_time = self._convert_timestamp_to_string(tweet_time)
            converted_last_updated = datetime.now(timezone.utc).isoformat()

            if not clean_username or not clean_tweet_text:
                logging.error("Missing required fields: username or tweet_text")
                return None

            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT OR IGNORE INTO tweets
                    (username, tweet_text, tweet_time, ticker, sentiment, entry_price, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_username,
                        clean_tweet_text,
                        converted_tweet_time,
                        clean_ticker,
                        clean_sentiment,
                        None if entry_price is None else float(entry_price),
                        converted_last_updated,
                    ),
                )
                conn.commit()

                if cur.rowcount > 0:
                    return int(cur.lastrowid)

                cur.execute(
                    """
                    UPDATE tweets
                    SET
                        ticker = CASE WHEN (ticker IS NULL OR ticker = '') AND ? IS NOT NULL THEN ? ELSE ticker END,
                        sentiment = CASE WHEN (sentiment IS NULL OR sentiment = '') AND ? IS NOT NULL THEN ? ELSE sentiment END,
                        entry_price = CASE WHEN entry_price IS NULL AND ? IS NOT NULL THEN ? ELSE entry_price END,
                        last_updated = ?
                    WHERE username = ? AND tweet_text = ? AND tweet_time = ?
                    """,
                    (
                        clean_ticker, clean_ticker,
                        clean_sentiment, clean_sentiment,
                        None if entry_price is None else float(entry_price),
                        None if entry_price is None else float(entry_price),
                        converted_last_updated,
                        clean_username, clean_tweet_text, converted_tweet_time,
                    ),
                )
                conn.commit()

                return self._select_tweet_id(clean_username, clean_tweet_text, converted_tweet_time)

        except Exception as e:
            logging.error(f"Error inserting tweet: {e}")
            return None

    def update_tweet_price(
        self,
        tweet_id: int,
        current_price: Optional[float],
        price_change_percent: Optional[float] = None,
    ) -> bool:
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                last_updated = datetime.now(timezone.utc).isoformat()
                cur.execute(
                    """
                    UPDATE tweets
                    SET current_price = ?, price_change_percent = ?, last_updated = ?
                    WHERE id = ?
                    """,
                    (
                        None if current_price is None else float(current_price),
                        None if price_change_percent is None else float(price_change_percent),
                        last_updated,
                        int(tweet_id),
                    ),
                )
                conn.commit()
                return cur.rowcount > 0
            except Exception as e:
                logging.error(f"Error updating tweet price: {e}")
                return False

    def insert_price_data(
        self,
        symbol: str,
        price: float,
        timestamp: Optional[Any] = None,
        market_type: str = "spot",
        volume: Optional[float] = None,
    ) -> bool:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        timestamp_str = self._convert_timestamp_to_string(timestamp)

        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO price_history (symbol, price, timestamp, market_type, volume)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (symbol, float(price), timestamp_str, market_type, volume),
                )
                conn.commit()
                return True
            except Exception as e:
                logging.error(f"Error inserting price data: {e}")
                return False


    def upsert_horizon_perf(
        self,
        tweet_id: int,
        horizon_h: int,
        ret_close: Optional[float],
        ret_high: Optional[float],
        ret_low: Optional[float],
        ret_close_alpha: Optional[float] = None,
    ) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO performance_horizons (tweet_id, horizon_h, ret_close, ret_high, ret_low, ret_close_alpha)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id, horizon_h) DO UPDATE SET
                  ret_close=excluded.ret_close,
                  ret_high=excluded.ret_high,
                  ret_low=excluded.ret_low,
                  ret_close_alpha=excluded.ret_close_alpha,
                  computed_at=CURRENT_TIMESTAMP
                """,
                (
                    int(tweet_id), int(horizon_h),
                    None if ret_close is None else float(ret_close),
                    None if ret_high  is None else float(ret_high),
                    None if ret_low   is None else float(ret_low),
                    None if ret_close_alpha is None else float(ret_close_alpha),
                ),
            )
            conn.commit()

    # -----------------------
    # Queries / reports
    # -----------------------
    def get_tweets_for_price_update(self) -> pd.DataFrame:
        with self._connect() as conn:
            return pd.read_sql_query(
                """
                SELECT id, username, ticker, sentiment, entry_price, tweet_time
                FROM tweets
                WHERE ticker IS NOT NULL
                  AND ticker NOT IN ('NOISE','MARKET')
                  AND entry_price IS NOT NULL
                ORDER BY tweet_time DESC
                """,
                conn,
            )

    def get_performance_summary(self, hours_limit: int = 24, eps: float = 0.02) -> pd.DataFrame:
        with self._connect() as conn:
            return pd.read_sql_query(
                f"""
                SELECT
                    ticker,
                    sentiment,
                    COUNT(*) AS tweet_count,
                    AVG(price_change_percent) AS avg_performance,
                    MIN(price_change_percent) AS min_performance,
                    MAX(price_change_percent) AS max_performance,
                    COUNT(CASE WHEN price_change_percent >  {eps} THEN 1 END) AS positive_count,
                    COUNT(CASE WHEN price_change_percent < -{eps} THEN 1 END) AS negative_count,
                    COUNT(CASE WHEN ABS(price_change_percent) <= {eps} THEN 1 END) AS zero_count
                FROM tweets
                WHERE ticker IS NOT NULL
                  AND ticker NOT IN ('NOISE','MARKET')
                  AND price_change_percent IS NOT NULL
                  AND datetime(tweet_time) > datetime('now', '-{hours_limit} hours')
                GROUP BY ticker, sentiment
                ORDER BY avg_performance DESC
                """,
                conn,
            )

    def get_best_performers(self, sentiment: Optional[str] = None, limit: int = 10) -> pd.DataFrame:
        sentiment_filter = f"AND sentiment = '{sentiment}'" if sentiment else ""
        with self._connect() as conn:
            return pd.read_sql_query(
                f"""
                SELECT username, ticker, sentiment, tweet_text,
                       entry_price, current_price, price_change_percent, tweet_time
                FROM tweets
                WHERE price_change_percent IS NOT NULL
                {sentiment_filter}
                ORDER BY price_change_percent DESC
                LIMIT {int(limit)}
                """,
                conn,
            )

    def get_ticker_stats(self, ticker: str) -> pd.DataFrame:
        with self._connect() as conn:
            return pd.read_sql_query(
                """
                SELECT username, sentiment, tweet_text, entry_price,
                       current_price, price_change_percent, tweet_time
                FROM tweets
                WHERE ticker = ?
                  AND price_change_percent IS NOT NULL
                ORDER BY tweet_time DESC
                """,
                conn,
                params=[ticker],
            )

    def cleanup_old_data(self, days_old: int = 30) -> int:
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    f"""
                    DELETE FROM price_history
                    WHERE datetime(timestamp) < datetime('now', '-{int(days_old)} days')
                    """
                )
                deleted = cur.rowcount
                conn.commit()
                logging.info(f"Cleaned up {deleted} old price records")
                return int(deleted)
            except Exception as e:
                logging.error(f"Error cleaning up old data: {e}")
                return 0

    def get_database_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            cur = conn.cursor()
            stats: dict[str, int] = {}

            cur.execute("SELECT COUNT(*) FROM tweets")
            stats["total_tweets"] = int(cur.fetchone()[0])

            cur.execute("SELECT COUNT(*) FROM tweets WHERE current_price IS NOT NULL")
            stats["tweets_with_prices"] = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT COUNT(DISTINCT ticker)
                FROM tweets
                WHERE ticker IS NOT NULL AND ticker NOT IN ('NOISE','MARKET')
                """
            )
            stats["unique_tickers"] = int(cur.fetchone()[0])

            cur.execute("SELECT COUNT(*) FROM price_history")
            stats["price_history_records"] = int(cur.fetchone()[0])

            return stats
