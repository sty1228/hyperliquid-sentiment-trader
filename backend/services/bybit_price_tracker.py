
import os
import re
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

import pandas as pd
import schedule

import sys
_THIS_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


from backend.services.sources import create_price_source
from backend.services.enhanced_price_database import EnhancedPriceDatabase
from backend.config import env, load_env, get_db_path

load_env()

DATA_DIR = env("DATA_DIR", "data")
LOG_DIR = env("LOG_DIR", "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

DB_PATH = get_db_path(env("DB_PATH", os.path.join(DATA_DIR, "crypto_tracker.db")))


MAX_BACKFILL_DAYS = int(env("MAX_BACKFILL_DAYS", "14")) 
ENTRY_FALLBACK_CURRENT = env("ENTRY_FALLBACK_CURRENT", "true").lower() in ("1", "true", "yes", "y")
SLEEP_S = float(env("API_SLEEP_SECONDS", "0.02"))

BYBIT_API_KEY = env("BYBIT_API_KEY", "")
BYBIT_SECRET = env("BYBIT_SECRET", "")
BYBIT_TESTNET = env("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes", "y")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def _map_price_source(name_raw: str) -> str:

    n = (name_raw or "").strip().lower()
    if n in ("bybit",):
        return "bybit"
    if n in ("hyperliquid", "hl", "hyperliquid_sdk"):
        return "hyperliquid"
    # default
    return "bybit"


def _utc_from_any(value) -> datetime:

    try:
        dt = pd.to_datetime(value, utc=True)
        return dt.to_pydatetime()
    except Exception:
        return datetime.now(timezone.utc)


def _get_price_number(payload: Any) -> Optional[float]:

    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, dict):
        for k in ("price", "last", "mark", "index", "close"):
            if k in payload:
                try:
                    return float(payload[k])
                except Exception:
                    pass
    return None


class PriceTracker:
    """

      1) read data/tweets_processed_complete.csv
      2) keep supported symbols only
      3) entry_price from 1m open near tweet_time (fallback current if enabled)
      4) refresh current_price and percent change to now
      5) compute fixed-horizon metrics (e.g., 24h)
      6) print leaderboard
    """

    SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}(USDT|USDC|USD)?$")

    def __init__(self, database: Optional[EnhancedPriceDatabase] = None, *, auto_schedule: bool = False):

        src_env = env("PRICE_SOURCE", "bybit")
        src_name = _map_price_source(src_env)
        if src_name == "bybit":
            self.price_source = create_price_source(
                name="bybit",
                api_key=BYBIT_API_KEY,
                secret_key=BYBIT_SECRET,
                testnet=BYBIT_TESTNET,
            )
            used_name = "BybitPriceSource"
        else:
            self.price_source = create_price_source(name="hyperliquid")
            used_name = "HyperliquidPriceSource"
        logging.info(f"Using price source: {used_name} (env={src_env})")

        db_dir = os.path.dirname(DB_PATH) or "."
        os.makedirs(db_dir, exist_ok=True)
        self.database = database if database else EnhancedPriceDatabase(DB_PATH)

        self.processed_tweets = set()

        try:
            if hasattr(self.price_source, "ensure_instruments_loaded"):
                self.price_source.ensure_instruments_loaded()
        except Exception as e:
            logging.warning(f"Failed to preload instruments: {e}")

        if auto_schedule:
            self.setup_scheduler()


    def setup_scheduler(self) -> None:
        schedule.every(5).minutes.do(self.update_all_prices)
        schedule.every(1).hours.do(self.cleanup_and_analyze)

        def run_scheduler():
            while True:
                try:
                    schedule.run_pending()
                except Exception as e:
                    logging.error(f"Scheduler error: {e}")
                time.sleep(60)

        threading.Thread(target=run_scheduler, daemon=True).start()
        logging.info("Background scheduler started")

 
    def _csv_path(self) -> str:
        return os.path.join(DATA_DIR, "tweets_processed_complete.csv")

    def _is_valid_symbol(self, s: Optional[str]) -> bool:
        if not s:
            return False
        s = str(s).upper().strip()
        if s in {"NOISE", "MARKET"}:
            return False
        return bool(self.SYMBOL_RE.match(s))

    def _supported_and_normalized(self, raw_symbol: str) -> Optional[str]:
        try:
            norm = self.price_source.normalize_symbol(raw_symbol)
            return norm if self.price_source.is_supported_symbol(norm) else None
        except Exception:
            return None

    def process_new_tweets(self) -> None:
        path = self._csv_path()
        if not os.path.exists(path):
            logging.error(f"Could not find processed CSV at {path}")
            return

        df = pd.read_csv(path)
        required = {"username", "tweet", "tweet_time", "ticker", "sentiment"}
        missing = required - set(df.columns)
        if missing:
            logging.error(f"CSV missing columns: {missing}")
            return

        logging.info(f"Processing {len(df)} tweets from CSV")
        processed, skipped = 0, 0

        now_utc = datetime.now(timezone.utc)

        for _, row in df.iterrows():
            username = str(row.get("username", "")).strip()
            tweet_text = str(row.get("tweet", "")).strip()
            raw = str(row.get("ticker", "")).upper().strip()
            sentiment = str(row.get("sentiment", "")).strip()

            if not username or not tweet_text:
                skipped += 1
                continue
            if not self._is_valid_symbol(raw):
                skipped += 1
                continue

            key = (username, tweet_text, str(row.get("tweet_time")))
            if key in self.processed_tweets:
                skipped += 1
                continue

            norm = self._supported_and_normalized(raw)
            if not norm:
                skipped += 1
                continue

            t0 = _utc_from_any(row["tweet_time"])

            if (now_utc - t0) > timedelta(days=MAX_BACKFILL_DAYS):
                skipped += 1
                continue

            entry = None
            for cat in ("perp", "linear", "spot"):
                try:
                    entry = self.price_source.get_price_at(norm, t0, category=cat, use_open=True)
                    if entry is not None:
                        break
                except Exception:
                    pass

            if entry is None and ENTRY_FALLBACK_CURRENT:
                try:
                    cur = self.price_source.get_current_price(norm)
                    entry = _get_price_number(cur)
                except Exception:
                    entry = None

            if entry is None:
                logging.warning(f"Could not get entry price at tweet_time for {norm}")
                skipped += 1
                continue

            tweet_id = self.database.insert_tweet(
                username=username,
                tweet_text=tweet_text,
                tweet_time=t0,
                ticker=norm,
                sentiment=sentiment,
                entry_price=float(entry),
            )
            if tweet_id:
                processed += 1
                self.processed_tweets.add(key)
                emoji = "🟢" if sentiment == "bullish" else "🔴" if sentiment == "bearish" else "⚪"
                logging.info(f"✓ Added {norm} {emoji} @{username} entry=${float(entry):.6f}")
            else:
                skipped += 1

            time.sleep(SLEEP_S)

        logging.info(f"Processing complete: {processed} added, {skipped} skipped")

    # Price updates
    def update_all_prices(self) -> None:
        tweets_df = self.database.get_tweets_for_price_update()
        if tweets_df.empty:
            logging.info("No tweets to update")
            return

        logging.info(f"Updating prices for {len(tweets_df)} tweets")

        unique_tickers = sorted(tweets_df["ticker"].dropna().unique())
        price_cache: Dict[str, float] = {}

        for tk in unique_tickers:
            try:
                norm = self.price_source.normalize_symbol(str(tk))
                if not self.price_source.is_supported_symbol(norm):
                    continue
                cur = self.price_source.get_current_price(norm)
                price_val = _get_price_number(cur)
                if price_val is not None:
                    price_cache[tk] = float(price_val)
                    self.database.insert_price_data(
                        symbol=tk,
                        price=float(price_val),
                        market_type=(cur.get("market", "spot") if isinstance(cur, dict) else "spot"),
                    )
            except Exception:
                pass
            time.sleep(SLEEP_S)

        updated = 0
        for _, tweet in tweets_df.iterrows():
            tk = tweet["ticker"]
            if tk not in price_cache:
                continue
            cur_price = price_cache[tk]
            entry = tweet["entry_price"]
            if entry is None:
                continue
            try:
                pct = ((float(cur_price) - float(entry)) / float(entry)) * 100.0
            except Exception:
                pct = None
            if self.database.update_tweet_price(int(tweet["id"]), cur_price, pct):
                updated += 1

        logging.info(f"Updated prices for {updated} tweets")


    # metrics

    def _choose_interval_fallback(self, horizon_h: int) -> str:

        H = int(horizon_h)
        if H <= 6:
            return "1"    # 1m
        if H <= 24:
            return "3"    # 3m
        if H <= 72:
            return "5"    # 5m
        if H <= 168:
            return "15"   # 15m
        return "60"       # 1h

    def _compute_horizon_metrics_for_tweet(self, tweet_row, horizons=(24,), benchmark="BTCUSDT"):
        import math

        symbol = tweet_row["ticker"]
        entry = tweet_row["entry_price"]
        if entry is None or (isinstance(entry, float) and math.isnan(entry)):
            return
        entry = float(entry)

        t0 = _utc_from_any(tweet_row["tweet_time"])
        t0_ms = int(t0.timestamp() * 1000)

        for H in horizons:
            t1 = t0 + timedelta(hours=int(H))
            t1_ms = int(t1.timestamp() * 1000)

            try:
                if hasattr(self.price_source, "choose_interval_for_horizon"):
                    interval = self.price_source.choose_interval_for_horizon(int(H))
                else:
                    interval = self._choose_interval_fallback(int(H))
            except Exception:
                interval = self._choose_interval_fallback(int(H))

            def pull_range_chunked(sym, cat):
                return self.price_source.get_klines_range_chunked(
                    sym, category=cat, interval=interval,
                    start_ms=t0_ms, end_ms=t1_ms, limit_per_call=2000
                )

            rows = pull_range_chunked(symbol, "perp")
            if not rows:
                rows = pull_range_chunked(symbol, "linear")
            if not rows:
                rows = pull_range_chunked(symbol, "spot")
            if not rows:
                continue

            try:
                highs = [float(r[2]) for r in rows]
                lows  = [float(r[3]) for r in rows]
                closes= [float(r[4]) for r in rows]
            except Exception:
                continue
            if not closes:
                continue

            close_H = closes[-1]
            ret_close = (close_H - entry) / entry
            ret_high  = ((max(highs) - entry) / entry) if highs else None
            ret_low   = ((min(lows)  - entry) / entry) if lows  else None

            ret_close_alpha = None
            try:
                b_entry = self.price_source.get_price_at(benchmark, t0, category="spot", use_open=True)
                b_end   = self.price_source.get_price_at(benchmark, t1, category="spot", use_open=False)
                if b_entry and b_end:
                    ret_close_alpha = ret_close - ((float(b_end) - float(b_entry)) / float(b_entry))
            except Exception:
                pass

            self.database.upsert_horizon_perf(int(tweet_row["id"]), int(H),
                                              ret_close, ret_high, ret_low, ret_close_alpha)
            time.sleep(SLEEP_S)

    def update_horizon_metrics(self, horizons=(24,)):
        df = self.database.get_tweets_for_price_update()
        if df.empty:
            logging.info("No tweets to compute horizon metrics")
            return
        logging.info(f"Computing horizon metrics for {len(df)} tweets, horizons={horizons}")
        for _, row in df.iterrows():
            self._compute_horizon_metrics_for_tweet(row, horizons=horizons)
        logging.info("Horizon metrics update complete")

    def print_horizon_summary(self, horizon_h=24, topn=10, eps=0.0002):
        import sqlite3
        with sqlite3.connect(self.database.db_path) as conn:
            q = """
            SELECT t.id, t.username, t.ticker, t.sentiment, t.tweet_time,
                   h.ret_close, h.ret_high, h.ret_low, h.ret_close_alpha
            FROM performance_horizons h
            JOIN tweets t ON t.id = h.tweet_id
            WHERE h.horizon_h = ?
            """
            df = pd.read_sql_query(q, conn, params=[int(horizon_h)])

        if df.empty:
            print(f"(No data yet for H={horizon_h}h)")
            return

        rc = df["ret_close"].astype(float)
        pos = (rc >  eps).sum()
        neg = (rc < -eps).sum()
        zer = (rc.abs() <= eps).sum()
        n = len(df)

        rc_pct = rc * 100.0
        median = rc_pct.median()
        p25 = rc_pct.quantile(0.25)
        p75 = rc_pct.quantile(0.75)
        mean = rc_pct.mean()

        print(f"\n{'='*80}")
        print(f"FIXED-HORIZON REPORT  (H = {horizon_h}h)")
        print(f"{'='*80}")
        print(f"Samples: {n}  |  Hit: {pos}  Miss: {neg}  Zero(|Δ|≤{eps*100:.2f}%): {zer}")
        print(f"Median: {median:+.3f}%  Mean: {mean:+.3f}%  P25: {p25:+.3f}%  P75: {p75:+.3f}%")

        top_win = df.sort_values("ret_close", ascending=False).head(topn)
        top_lose= df.sort_values("ret_close", ascending=True).head(topn)

        def _print_rows(name, rows):
            print(f"\n{name}")
            for _, r in rows.iterrows():
                rc_ = float(r["ret_close"]) * 100.0
                mfe = float(r["ret_high"])  * 100.0 if pd.notna(r["ret_high"]) else float('nan')
                mae = float(r["ret_low"])   * 100.0 if pd.notna(r["ret_low"])  else float('nan')
                alpha = r["ret_close_alpha"]
                alpha_s = "" if pd.isna(alpha) else f" | alpha={alpha*100:+.3f}%"
                print(f"@{r['username']} {r['ticker']} {r['sentiment']} | "
                      f"ret={rc_:+.3f}% | MFE={mfe:+.3f}% MAE={mae:+.3f}%{alpha_s}")

        _print_rows(f"TOP {topn} WINNERS", top_win)
        _print_rows(f"TOP {topn} LOSERS",  top_lose)
        print(f"{'='*80}")

    # leaderboard

    def get_user_leaderboard_df(self, hours: int = 168, eps: float = 0.02, min_calls: int = 3):
        import sqlite3

        hours = int(hours)
        eps = float(eps)
        min_calls = int(min_calls)

        with sqlite3.connect(self.database.db_path) as conn:
            q = f"""
            SELECT username,
                   COUNT(*) AS tweet_count,
                   AVG(price_change_percent) AS avg_perf,
                   SUM(CASE WHEN price_change_percent >  {eps} THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN price_change_percent < -{eps} THEN 1 ELSE 0 END) AS negative,
                   SUM(CASE WHEN ABS(price_change_percent) <= {eps} THEN 1 ELSE 0 END) AS zero
            FROM tweets
            WHERE price_change_percent IS NOT NULL
              AND username IS NOT NULL AND username != ''
              AND datetime(tweet_time) > datetime('now', '-{hours} hours')
            GROUP BY username
            HAVING tweet_count >= {min_calls}
            """
            df = pd.read_sql_query(q, conn)

        if df.empty:
            return df

        df["hit_rate_%"] = (df["positive"] / df["tweet_count"]) * 100.0
        df = df.sort_values(["avg_perf", "tweet_count"], ascending=[False, False]).reset_index(drop=True)
        df.index = df.index + 1
        return df

    def print_user_leaderboard(self, hours: int = 168, eps: float = 0.02,
                               topn: int = 20, min_calls: int = 3,
                               save_csv: str | None = None) -> None:
        df = self.bbget_user_leaderboard_df(hours=hours, eps=eps, min_calls=min_calls)
        if df.empty:
            print("No user data available for leaderboard")
            return

        if save_csv:
            out_dir = os.path.join(DATA_DIR)
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, save_csv)
            try:
                df.to_csv(out_path, index=True)
                logging.info(f"Leaderboard saved to {out_path}")
            except Exception as e:
                logging.warning(f"Failed to save CSV: {e}")

        print(f"\n{'='*80}")
        print(f"USER LEADERBOARD (last {int(hours)}h, eps={float(eps):.2f}%, min_calls={int(min_calls)})")
        print(f"{'='*80}")
        for i, row in df.head(int(topn)).iterrows():
            uname = row["username"]
            avg   = float(row["avg_perf"])
            n     = int(row["tweet_count"])
            pos   = int(row["positive"])
            neg   = int(row["negative"])
            zer   = int(row["zero"])
            hit_rate = float(row["hit_rate_%"])
            print(f"{i:2d}. @{uname:20s} | avg={avg:+.3f}% | N={n:3d} | hit={pos:3d} miss={neg:3d} zero={zer:3d} | hit_rate={hit_rate:5.1f}%")
        print(f"{'='*80}")


    def print_performance_summary(self, hours: int = 24, eps: float = 0.02) -> None:
        print(f"\n{'='*80}")
        print(f"CRYPTO INFLUENCER PERFORMANCE REPORT (Last {hours} hours)")
        print(f"{'='*80}")

        summary_df = self.database.get_performance_summary(hours_limit=hours, eps=eps)
        if summary_df.empty:
            print("No performance data available")
            return

        total_tweets = int(summary_df["tweet_count"].sum())
        if total_tweets > 0:
            overall_avg = float(
                (summary_df["avg_performance"] * summary_df["tweet_count"]).sum()
                / summary_df["tweet_count"].sum()
            )
        else:
            overall_avg = float("nan")

        print(f"OVERALL STATS")
        print(f"   Total Tracked Tweets: {total_tweets:,}")
        print(f"   Average Performance (weighted): {overall_avg:+.4f}%")
        print(f"   Positive Calls: {int(summary_df['positive_count'].sum())}")
        print(f"   Negative Calls: {int(summary_df['negative_count'].sum())}")
        print(f"   Zeros (|Δ|≤{eps}%): {int(summary_df['zero_count'].sum())}")

        print(f"PERFORMANCE BY SENTIMENT (weighted)")
        for sentiment in ["bullish", "bearish", "neutral"]:
            sd = summary_df[summary_df["sentiment"] == sentiment]
            if not sd.empty:
                sent_count = int(sd["tweet_count"].sum())
                if sent_count > 0:
                    sent_avg = float(
                        (sd["avg_performance"] * sd["tweet_count"]).sum()
                        / sd["tweet_count"].sum()
                    )
                    emoji = "🟢" if sentiment == "bullish" else "🔴" if sentiment == "bearish" else "⚪"
                    print(f"   {emoji} {sentiment.title()}: {sent_avg:+.4f}% avg ({sent_count} tweets)")

        print(f"\n🏆 TOP PERFORMING TICKERS")
        top_tickers = summary_df.nlargest(10, "avg_performance")
        for _, row in top_tickers.iterrows():
            emoji = "🟢" if row["sentiment"] == "bullish" else "🔴" if row["sentiment"] == "bearish" else "⚪"
            print(f"   {row['ticker']} {emoji}: {row['avg_performance']:+.4f}% ({row['tweet_count']} tweets)")

        print(f"\n🎯 BEST INDIVIDUAL CALLS")
        best = self.database.get_best_performers(limit=5)
        if best.empty:
            print("   (No individual calls yet)")
        else:
            for _, call in best.iterrows():
                emoji = "🟢" if call["sentiment"] == "bullish" else "🔴" if call["sentiment"] == "bearish" else "⚪"
                try:
                    dt = pd.to_datetime(call["tweet_time"], utc=True)
                    now_utc = datetime.now(timezone.utc)
                    hours_ago = (now_utc - dt.to_pydatetime()).total_seconds() / 3600.0
                except Exception:
                    hours_ago = float("nan")
                print(f"   @{call['username']} - {call['ticker']} {emoji}: {call['price_change_percent']:+.4f}% ({hours_ago:.1f}h ago)")
                try:
                    print(f"     ${float(call['entry_price']):.6f} → ${float(call['current_price']):.6f}")
                except Exception:
                    pass
                text = str(call["tweet_text"])[:60].replace("\n", " ")
                print(f"     \"{text}...\"")
                print()

        print(f"{'='*80}")

    # -----------------------
    # Maintenance
    # -----------------------
    def cleanup_and_analyze(self) -> None:
        logging.info("Running cleanup and analysis...")
        cleaned = self.database.cleanup_old_data(days_old=7)
        logging.info(f"Cleaned up {cleaned} old price records")

        summary_df = self.database.get_performance_summary(hours_limit=24)
        if not summary_df.empty:
            if (summary_df["tweet_count"] > 0).any():
                avg_performance = (
                    (summary_df["avg_performance"] * summary_df["tweet_count"]).sum()
                    / summary_df["tweet_count"].sum()
                )
            else:
                avg_performance = float("nan")
            total_tweets = int(summary_df["tweet_count"].sum())
            logging.info(f"24h Performance: {avg_performance:+.4f}% across {total_tweets} tracked tweets")

    # -----------------------
    # Entrypoints
    # -----------------------
    def run_once(self) -> None:
        self.process_new_tweets()
        self.update_all_prices()
        self.print_performance_summary()

    def start_live_tracking(self, *, with_scheduler: bool = True) -> None:
        logging.info("🚀 Starting live crypto influencer tracking...")
        try:
            self.run_once()
            if with_scheduler:
                self.setup_scheduler()
                logging.info("Live tracking started! Updates every 5 minutes.")
        except KeyboardInterrupt:
            logging.info("Stopping live tracking...")
        except Exception as e:
            logging.error(f"Error in live tracking: {e}")


if __name__ == "__main__":
    tracker = PriceTracker()
    tracker.start_live_tracking(with_scheduler=True)
