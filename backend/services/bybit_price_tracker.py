"""
HyperCopy Price Tracker — HyperLiquid-first with Bybit fallback.

Key changes from Bybit-only version:
  • Default price source is now HyperLiquid (allMids endpoint).
  • Bulk price fetch: one POST to /info gets ALL mid prices at once.
  • Supports HIP-3 tokens (TSLA, CIRCLE, etc.) automatically.
  • Falls back to Bybit source for historical klines if HL doesn't have them.
  • PCT_SANITY_CAP=500 still enforced on all pct_change values.
"""
import os
import re
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import requests as http_requests
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

HL_BASE_URL = env("HL_BASE_URL", "https://api.hyperliquid.xyz")

# ★ Default to HyperLiquid now (was "bybit")
PRICE_SOURCE_ENV = env("PRICE_SOURCE", "hyperliquid")

PCT_SANITY_CAP = 500.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ═══════════════════════════════════════════════════════════════════════
#  HYPERLIQUID NATIVE PRICE CLIENT
# ═══════════════════════════════════════════════════════════════════════

class HLPriceClient:
    """Thin client for HyperLiquid price data.
    
    Uses:
      - POST /info {"type":"allMids"} for bulk current prices (one request!)
      - POST /info {"type":"meta"} + {"type":"spotMeta"} for token lists
      - POST /info {"type":"candleSnapshot"} for historical klines
    """

    def __init__(self, base_url: str = "https://api.hyperliquid.xyz"):
        self.base_url = base_url
        self._token_cache: Dict[str, Any] = {"perp": set(), "spot": set(), "fetched_at": 0.0}
        self._TOKEN_TTL = 3600

    def _post(self, payload: dict, timeout: int = 15) -> Any:
        r = http_requests.post(f"{self.base_url}/info", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # ── Token lists ──────────────────────────────────────────────────

    def get_all_tokens(self) -> Dict[str, set]:
        """Returns {"perp": set(...), "spot": set(...), "all": set(...)}"""
        now = time.monotonic()
        if now - self._token_cache["fetched_at"] < self._TOKEN_TTL and self._token_cache["perp"]:
            return {
                "perp": self._token_cache["perp"],
                "spot": self._token_cache["spot"],
                "all": self._token_cache["perp"] | self._token_cache["spot"],
            }

        perp_tokens = set()
        spot_tokens = set()

        try:
            meta = self._post({"type": "meta"})
            for asset in meta.get("universe", []):
                name = asset.get("name", "").upper().strip()
                if name:
                    perp_tokens.add(name)
        except Exception as e:
            logging.warning(f"HL meta fetch failed: {e}")

        try:
            spot = self._post({"type": "spotMeta"})
            for tok in spot.get("tokens", []):
                name = tok.get("name", "").upper().strip()
                if name:
                    spot_tokens.add(name)
        except Exception as e:
            logging.warning(f"HL spotMeta fetch failed: {e}")

        if perp_tokens or spot_tokens:
            self._token_cache["perp"] = perp_tokens
            self._token_cache["spot"] = spot_tokens
            self._token_cache["fetched_at"] = now

        return {
            "perp": perp_tokens,
            "spot": spot_tokens,
            "all": perp_tokens | spot_tokens,
        }

    # ── Bulk current prices (the big win) ────────────────────────────

    def get_all_mids(self) -> Dict[str, float]:
        """Fetch ALL mid prices in one request. Returns {symbol: price}.
        This is the key efficiency gain — replaces hundreds of individual calls."""
        try:
            data = self._post({"type": "allMids"})
            result = {}
            for symbol, price_str in data.items():
                try:
                    price = float(price_str)
                    if price > 0:
                        result[symbol.upper()] = price
                except (ValueError, TypeError):
                    pass
            return result
        except Exception as e:
            logging.error(f"HL allMids failed: {e}")
            return {}

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current mid price for a single symbol."""
        mids = self.get_all_mids()
        return mids.get(symbol.upper())

    # ── Historical klines ────────────────────────────────────────────

    def get_candle_at(self, symbol: str, timestamp: datetime,
                      interval: str = "1h") -> Optional[float]:
        """Get the open price of the candle containing `timestamp`."""
        ts_ms = int(timestamp.timestamp() * 1000)
        # Request a small window around the timestamp
        try:
            data = self._post({
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol.upper(),
                    "interval": interval,
                    "startTime": ts_ms - 3600_000,  # 1h before
                    "endTime": ts_ms + 3600_000,     # 1h after
                }
            })
            if not data:
                return None
            # Find the candle closest to our timestamp
            best = None
            best_dist = float("inf")
            for candle in data:
                t = candle.get("t", 0)
                dist = abs(t - ts_ms)
                if dist < best_dist:
                    best_dist = dist
                    best = candle
            if best:
                return float(best.get("o", 0))  # open price
            return None
        except Exception as e:
            logging.debug(f"HL candle fetch failed for {symbol}: {e}")
            return None

    def get_klines_range(self, symbol: str, start_ms: int, end_ms: int,
                         interval: str = "1h") -> List[dict]:
        """Get candles in a time range."""
        try:
            data = self._post({
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol.upper(),
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                }
            })
            return data if data else []
        except Exception as e:
            logging.debug(f"HL klines range failed for {symbol}: {e}")
            return []


# ═══════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _map_price_source(name_raw: str) -> str:
    n = (name_raw or "").strip().lower()
    if n in ("bybit",):
        return "bybit"
    if n in ("hyperliquid", "hl", "hyperliquid_sdk"):
        return "hyperliquid"
    return "hyperliquid"  # ★ default changed from bybit


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


# ═══════════════════════════════════════════════════════════════════════
#  MAIN TRACKER CLASS
# ═══════════════════════════════════════════════════════════════════════

class PriceTracker:
    SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}(USDT|USDC|USD)?$")

    def __init__(self, database: Optional[EnhancedPriceDatabase] = None,
                 *, auto_schedule: bool = False):
        # ★ Always create HL client for bulk prices
        self.hl_client = HLPriceClient(base_url=HL_BASE_URL)

        # Legacy source for historical klines (fallback)
        src_name = _map_price_source(PRICE_SOURCE_ENV)
        if src_name == "bybit":
            self.price_source = create_price_source(
                name="bybit",
                api_key=BYBIT_API_KEY,
                secret_key=BYBIT_SECRET,
                testnet=BYBIT_TESTNET,
            )
            self._legacy_source_name = "Bybit"
        else:
            try:
                self.price_source = create_price_source(name="hyperliquid")
                self._legacy_source_name = "HyperliquidSDK"
            except Exception:
                self.price_source = create_price_source(
                    name="bybit",
                    api_key=BYBIT_API_KEY,
                    secret_key=BYBIT_SECRET,
                    testnet=BYBIT_TESTNET,
                )
                self._legacy_source_name = "Bybit(fallback)"

        logging.info(f"Price sources: HL allMids (primary) + {self._legacy_source_name} (klines)")

        # Pre-fetch token list
        tokens = self.hl_client.get_all_tokens()
        logging.info(f"📋 HL tokens: {len(tokens['perp'])} perp, {len(tokens['spot'])} spot")

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

    def _get_entry_price_hl(self, symbol: str, tweet_time: datetime) -> Optional[float]:
        """Try to get entry price from HL candles first, then allMids as fallback."""
        # Try historical candle
        price = self.hl_client.get_candle_at(symbol, tweet_time, interval="1h")
        if price and price > 0:
            return price

        # Try 15m candle for more precision
        price = self.hl_client.get_candle_at(symbol, tweet_time, interval="15m")
        if price and price > 0:
            return price

        return None

    def _get_entry_price_legacy(self, symbol: str, tweet_time: datetime) -> Optional[float]:
        """Legacy source fallback for entry price."""
        try:
            norm = self.price_source.normalize_symbol(symbol)
            if not self.price_source.is_supported_symbol(norm):
                return None
        except Exception:
            return None

        for cat in ("perp", "linear", "spot"):
            try:
                entry = self.price_source.get_price_at(norm, tweet_time, category=cat, use_open=True)
                if entry is not None:
                    return float(entry) if not isinstance(entry, float) else entry
            except Exception:
                pass
        return None

    def _get_entry_price(self, symbol: str, tweet_time: datetime) -> Optional[float]:
        """Get entry price: HL first, legacy fallback, current price last resort."""
        # 1) HL historical
        price = self._get_entry_price_hl(symbol, tweet_time)
        if price:
            return price

        # 2) Legacy source (Bybit klines)
        price = self._get_entry_price_legacy(symbol, tweet_time)
        if price:
            return price

        # 3) Current price fallback (only if tweet is recent)
        if ENTRY_FALLBACK_CURRENT:
            age = datetime.now(timezone.utc) - tweet_time
            if age < timedelta(hours=1):
                cur = self.hl_client.get_current_price(symbol)
                if cur:
                    return cur

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

            t0 = _utc_from_any(row["tweet_time"])

            if (now_utc - t0) > timedelta(days=MAX_BACKFILL_DAYS):
                skipped += 1
                continue

            entry = self._get_entry_price(raw, t0)

            if entry is None:
                logging.warning(f"Could not get entry price for {raw} at {t0}")
                skipped += 1
                continue

            tweet_id = self.database.insert_tweet(
                username=username,
                tweet_text=tweet_text,
                tweet_time=t0,
                ticker=raw,
                sentiment=sentiment,
                entry_price=float(entry),
            )
            if tweet_id:
                processed += 1
                self.processed_tweets.add(key)
                emoji = "🟢" if sentiment == "bullish" else "🔴" if sentiment == "bearish" else "⚪"
                logging.info(f"✓ Added {raw} {emoji} @{username} entry=${float(entry):.6f}")
            else:
                skipped += 1

            time.sleep(SLEEP_S)

        logging.info(f"Processing complete: {processed} added, {skipped} skipped")

    def update_all_prices(self) -> None:
        """★ Core improvement: one HL allMids call replaces hundreds of individual requests."""
        tweets_df = self.database.get_tweets_for_price_update()
        if tweets_df.empty:
            logging.info("No tweets to update")
            return

        logging.info(f"Updating prices for {len(tweets_df)} tweets")

        # ★ ONE request for ALL prices
        all_mids = self.hl_client.get_all_mids()
        if not all_mids:
            logging.warning("HL allMids returned empty — falling back to legacy source")
            self._update_all_prices_legacy(tweets_df)
            return

        logging.info(f"📊 Got {len(all_mids)} prices from HL allMids")

        unique_tickers = sorted(tweets_df["ticker"].dropna().unique())
        price_cache: Dict[str, float] = {}

        for tk in unique_tickers:
            tk_upper = str(tk).upper()
            price = all_mids.get(tk_upper)
            if price is not None:
                price_cache[tk] = price
                self.database.insert_price_data(
                    symbol=tk, price=price, market_type="perp",
                )

        # Tokens not on HL — try legacy source
        missing = [tk for tk in unique_tickers if tk not in price_cache]
        if missing:
            logging.info(f"  {len(missing)} tickers not on HL, trying legacy source: {missing[:10]}")
            for tk in missing:
                try:
                    norm = self.price_source.normalize_symbol(str(tk))
                    if not self.price_source.is_supported_symbol(norm):
                        continue
                    cur = self.price_source.get_current_price(norm)
                    price_val = _get_price_number(cur)
                    if price_val is not None:
                        price_cache[tk] = price_val
                        self.database.insert_price_data(
                            symbol=tk, price=price_val,
                            market_type=(cur.get("market", "spot") if isinstance(cur, dict) else "spot"),
                        )
                except Exception:
                    pass
                time.sleep(SLEEP_S)

        # Update tweet prices
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
                if abs(pct) > PCT_SANITY_CAP:
                    logging.warning(
                        f"Extreme pct_change={pct:.1f}% for {tk} "
                        f"(entry={entry}, current={cur_price}) — discarding"
                    )
                    pct = None
            except Exception:
                pct = None

            if self.database.update_tweet_price(int(tweet["id"]), cur_price, pct):
                updated += 1

        logging.info(f"Updated prices for {updated}/{len(tweets_df)} tweets "
                     f"({len(price_cache)} tickers had prices)")

    def _update_all_prices_legacy(self, tweets_df) -> None:
        """Fallback: update prices one-by-one via legacy source."""
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
                        symbol=tk, price=float(price_val),
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
                if abs(pct) > PCT_SANITY_CAP:
                    pct = None
            except Exception:
                pct = None
            if self.database.update_tweet_price(int(tweet["id"]), cur_price, pct):
                updated += 1

        logging.info(f"(Legacy) Updated prices for {updated} tweets")

    def _choose_interval_fallback(self, horizon_h: int) -> str:
        H = int(horizon_h)
        if H <= 6:   return "1"
        if H <= 24:  return "3"
        if H <= 72:  return "5"
        if H <= 168: return "15"
        return "60"

    def _compute_horizon_metrics_for_tweet(self, tweet_row, horizons=(24,), benchmark="BTC"):
        import math

        symbol = tweet_row["ticker"]
        entry = tweet_row["entry_price"]
        if entry is None or (isinstance(entry, float) and math.isnan(entry)):
            return
        entry = float(entry)

        t0 = _utc_from_any(tweet_row["tweet_time"])

        for H in horizons:
            t1 = t0 + timedelta(hours=int(H))

            # ★ Try HL candles first
            interval = "1h" if H <= 48 else "4h"
            start_ms = int(t0.timestamp() * 1000)
            end_ms = int(t1.timestamp() * 1000)

            candles = self.hl_client.get_klines_range(symbol, start_ms, end_ms, interval=interval)

            if not candles:
                # Fallback to legacy source
                try:
                    if hasattr(self.price_source, "choose_interval_for_horizon"):
                        legacy_interval = self.price_source.choose_interval_for_horizon(int(H))
                    else:
                        legacy_interval = self._choose_interval_fallback(int(H))

                    def pull_range_chunked(sym, cat):
                        return self.price_source.get_klines_range_chunked(
                            sym, category=cat, interval=legacy_interval,
                            start_ms=start_ms, end_ms=end_ms, limit_per_call=2000
                        )

                    rows = pull_range_chunked(symbol, "perp")
                    if not rows:
                        rows = pull_range_chunked(symbol, "linear")
                    if not rows:
                        rows = pull_range_chunked(symbol, "spot")
                    if rows:
                        # Convert legacy format to HL candle format
                        candles = [{"h": r[2], "l": r[3], "c": r[4]} for r in rows]
                except Exception:
                    pass

            if not candles:
                continue

            try:
                highs  = [float(c.get("h", 0)) for c in candles if c.get("h")]
                lows   = [float(c.get("l", 0)) for c in candles if c.get("l")]
                closes = [float(c.get("c", 0)) for c in candles if c.get("c")]
            except Exception:
                continue
            if not closes:
                continue

            close_H   = closes[-1]
            ret_close = (close_H - entry) / entry
            ret_high  = ((max(highs) - entry) / entry) if highs else None
            ret_low   = ((min(lows)  - entry) / entry) if lows  else None

            if abs(ret_close) > PCT_SANITY_CAP / 100.0:
                logging.warning(
                    f"Extreme horizon ret_close={ret_close*100:.1f}% for {symbol} — discarding"
                )
                continue

            ret_close_alpha = None
            try:
                b_candles = self.hl_client.get_klines_range(benchmark, start_ms, end_ms, interval=interval)
                if b_candles and len(b_candles) >= 2:
                    b_entry = float(b_candles[0].get("o", 0))
                    b_close = float(b_candles[-1].get("c", 0))
                    if b_entry > 0:
                        ret_close_alpha = ret_close - ((b_close - b_entry) / b_entry)
            except Exception:
                pass

            self.database.upsert_horizon_perf(
                int(tweet_row["id"]), int(H),
                ret_close, ret_high, ret_low, ret_close_alpha
            )
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
        p25    = rc_pct.quantile(0.25)
        p75    = rc_pct.quantile(0.75)
        mean   = rc_pct.mean()

        print(f"\n{'='*80}")
        print(f"FIXED-HORIZON REPORT  (H = {horizon_h}h)")
        print(f"{'='*80}")
        print(f"Samples: {n}  |  Hit: {pos}  Miss: {neg}  Zero(|Δ|≤{eps*100:.2f}%): {zer}")
        print(f"Median: {median:+.3f}%  Mean: {mean:+.3f}%  P25: {p25:+.3f}%  P75: {p75:+.3f}%")

        top_win  = df.sort_values("ret_close", ascending=False).head(topn)
        top_lose = df.sort_values("ret_close", ascending=True).head(topn)

        def _print_rows(name, rows):
            print(f"\n{name}")
            for _, r in rows.iterrows():
                rc_  = float(r["ret_close"]) * 100.0
                mfe  = float(r["ret_high"])  * 100.0 if pd.notna(r["ret_high"])  else float("nan")
                mae  = float(r["ret_low"])   * 100.0 if pd.notna(r["ret_low"])   else float("nan")
                alpha = r["ret_close_alpha"]
                alpha_s = "" if pd.isna(alpha) else f" | alpha={alpha*100:+.3f}%"
                print(f"@{r['username']} {r['ticker']} {r['sentiment']} | "
                      f"ret={rc_:+.3f}% | MFE={mfe:+.3f}% MAE={mae:+.3f}%{alpha_s}")

        _print_rows(f"TOP {topn} WINNERS", top_win)
        _print_rows(f"TOP {topn} LOSERS",  top_lose)
        print(f"{'='*80}")

    def get_user_leaderboard_df(self, hours: int = 168, eps: float = 0.02, min_calls: int = 3):
        import sqlite3

        hours     = int(hours)
        eps       = float(eps)
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
              AND ABS(price_change_percent) <= {PCT_SANITY_CAP}
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
        df = self.get_user_leaderboard_df(hours=hours, eps=eps, min_calls=min_calls)
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
            uname    = row["username"]
            avg      = float(row["avg_perf"])
            n        = int(row["tweet_count"])
            pos      = int(row["positive"])
            neg      = int(row["negative"])
            zer      = int(row["zero"])
            hit_rate = float(row["hit_rate_%"])
            print(f"{i:2d}. @{uname:20s} | avg={avg:+.3f}% | N={n:3d} | "
                  f"hit={pos:3d} miss={neg:3d} zero={zer:3d} | hit_rate={hit_rate:5.1f}%")
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
                print(f"   @{call['username']} - {call['ticker']} {emoji}: "
                      f"{call['price_change_percent']:+.4f}% ({hours_ago:.1f}h ago)")
                try:
                    print(f"     ${float(call['entry_price']):.6f} → ${float(call['current_price']):.6f}")
                except Exception:
                    pass
                text = str(call["tweet_text"])[:60].replace("\n", " ")
                print(f"     \"{text}...\"")
                print()

        print(f"{'='*80}")

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
            logging.info(
                f"24h Performance: {avg_performance:+.4f}% across {total_tweets} tracked tweets"
            )

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