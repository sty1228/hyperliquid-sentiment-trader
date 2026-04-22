"""
Max Gain Updater (2026-04-23) — Standalone Version
====================================================
Computes the maximum favorable excursion (peak gain) for each signal
from tweet_time to now, using HyperLiquid historical klines.

For bullish/long: max_gain = (max_high - entry) / entry * 100  (always positive)
For bearish/short: max_gain = (entry - min_low) / entry * 100  (always positive)

Monotonic — only updated when the new value exceeds the stored one.
Batches by ticker to minimize API calls (one klines request per unique coin).

This is SELF-CONTAINED — does not import from bybit_price_tracker.

Usage:
    python -m backend.services.max_gain_updater          # loop mode (every 5 min)
    python -m backend.services.max_gain_updater --once   # single run (cron mode)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

import requests as http_requests
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models.signal import Signal

log = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────
HL_BASE_URL = os.getenv("HL_BASE_URL", "https://api.hyperliquid.xyz")
PCT_SANITY_CAP = 500.0      # max reasonable % gain; beyond this = bad entry price
MAX_LOOKBACK_DAYS = 14      # Only process signals from last N days
MIN_AGE_MINUTES = 3         # Skip signals < 3 min old (no data yet)
SLEEP_BETWEEN_TICKERS = 0.05
LOOP_INTERVAL_SECONDS = 300 # 5 min


# ═══════════════════════════════════════════════════════════════
# Self-contained HyperLiquid price client
# ═══════════════════════════════════════════════════════════════

class HLClient:
    """Minimal HL price client for candle fetching."""

    def __init__(self, base_url: str = HL_BASE_URL):
        self.base_url = base_url

    def _post(self, payload: dict, timeout: int = 15) -> Any:
        r = http_requests.post(f"{self.base_url}/info", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def get_klines_range(self, symbol: str, start_ms: int, end_ms: int,
                         interval: str = "1h") -> list[dict]:
        """Get candles in a time range. Returns list of {t, o, h, l, c, v}."""
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
            log.debug(f"HL klines fetch failed for {symbol}: {e}")
            return []


# ═══════════════════════════════════════════════════════════════
# Core logic
# ═══════════════════════════════════════════════════════════════

def _pick_interval(hours_elapsed: float) -> str:
    """Pick HL candle interval based on signal age."""
    if hours_elapsed <= 24:
        return "15m"
    if hours_elapsed <= 168:   # 7 days
        return "1h"
    return "4h"


def _is_bullish(signal: Signal) -> bool:
    if signal.direction == "long":
        return True
    if signal.direction == "short":
        return False
    return signal.sentiment == "bullish"


def _compute_from_candles(
    signal: Signal,
    candles: list[dict],
) -> tuple[float | None, datetime | None]:
    """Given pre-fetched candles, compute max_gain for this signal."""
    if not signal.entry_price or signal.entry_price <= 0:
        return None, None

    t0 = signal.tweet_time or signal.created_at
    if not t0:
        return None, None
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    t0_ms = int(t0.timestamp() * 1000)

    entry = float(signal.entry_price)
    is_bull = _is_bullish(signal)

    best_price: Optional[float] = None
    best_ts_ms: Optional[int] = None

    for c in candles:
        try:
            t_ms = int(c.get("t", 0))
            if t_ms < t0_ms:
                continue  # before tweet
            if is_bull:
                price = float(c.get("h", 0))
                if price > 0 and (best_price is None or price > best_price):
                    best_price = price
                    best_ts_ms = t_ms
            else:
                price = float(c.get("l", 0))
                if price > 0 and (best_price is None or price < best_price):
                    best_price = price
                    best_ts_ms = t_ms
        except (ValueError, TypeError):
            continue

    if best_price is None or best_price <= 0:
        return None, None

    if is_bull:
        gain = (best_price - entry) / entry * 100.0
    else:
        gain = (entry - best_price) / entry * 100.0

    if gain < 0:
        gain = 0.0  # Peak never went favorable
    if gain > PCT_SANITY_CAP:
        log.warning(
            f"Extreme max_gain={gain:.1f}% for {signal.ticker} sig={signal.id[:8]} "
            f"(entry={entry}, peak={best_price}) — discarding"
        )
        return None, None

    peak_at = datetime.fromtimestamp(best_ts_ms / 1000, tz=timezone.utc) if best_ts_ms else None
    return gain, peak_at


def update_max_gains(db: Optional[Session] = None, max_signals: int = 2000) -> dict:
    """Run one update cycle."""
    owned_db = db is None
    if owned_db:
        db = SessionLocal()

    stats = {"processed": 0, "updated": 0, "skipped_no_data": 0, "skipped_too_new": 0, "errors": 0}

    try:
        hl = HLClient()
        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc - timedelta(days=MAX_LOOKBACK_DAYS)
        min_age = now_utc - timedelta(minutes=MIN_AGE_MINUTES)

        signals: list[Signal] = (
            db.query(Signal)
            .filter(
                Signal.entry_price.isnot(None),
                Signal.ticker.isnot(None),
                or_(
                    Signal.tweet_time >= cutoff,
                    and_(Signal.tweet_time.is_(None), Signal.created_at >= cutoff),
                ),
            )
            .limit(max_signals)
            .all()
        )

        log.info(f"max_gain: scanning {len(signals)} signals from last {MAX_LOOKBACK_DAYS} days")

        # Group by ticker, track oldest tweet_time per ticker
        by_ticker: dict[str, list[Signal]] = defaultdict(list)
        oldest_ts: dict[str, datetime] = {}
        for s in signals:
            t0 = s.tweet_time or s.created_at
            if not t0:
                continue
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=timezone.utc)
            if t0 > min_age:
                stats["skipped_too_new"] += 1
                continue
            tk = s.ticker.upper()
            by_ticker[tk].append(s)
            if tk not in oldest_ts or t0 < oldest_ts[tk]:
                oldest_ts[tk] = t0

        log.info(f"max_gain: {len(by_ticker)} unique tickers to fetch")

        for ticker, sigs in by_ticker.items():
            t_start = oldest_ts[ticker]
            hours_elapsed = (now_utc - t_start).total_seconds() / 3600.0
            interval = _pick_interval(hours_elapsed)
            start_ms = int(t_start.timestamp() * 1000)
            end_ms = int(now_utc.timestamp() * 1000)

            try:
                candles = hl.get_klines_range(ticker, start_ms, end_ms, interval=interval)
            except Exception as e:
                log.warning(f"max_gain: HL klines failed for {ticker}: {e}")
                stats["errors"] += len(sigs)
                continue

            if not candles:
                stats["skipped_no_data"] += len(sigs)
                continue

            for sig in sigs:
                stats["processed"] += 1
                try:
                    new_gain, peak_at = _compute_from_candles(sig, candles)
                    if new_gain is None:
                        stats["skipped_no_data"] += 1
                        continue

                    current = sig.max_gain_pct or 0.0
                    if new_gain > current + 0.01:
                        sig.max_gain_pct = new_gain
                        sig.max_gain_at = peak_at
                        stats["updated"] += 1
                except Exception as e:
                    log.warning(f"max_gain: compute error for sig {sig.id[:8]}: {e}")
                    stats["errors"] += 1

            time.sleep(SLEEP_BETWEEN_TICKERS)

        if owned_db:
            db.commit()

        log.info(
            f"max_gain done: processed={stats['processed']} updated={stats['updated']} "
            f"skipped_new={stats['skipped_too_new']} no_data={stats['skipped_no_data']} errors={stats['errors']}"
        )

    except Exception as e:
        log.error(f"max_gain updater FAILED: {e}", exc_info=True)
        if owned_db:
            db.rollback()
        raise
    finally:
        if owned_db:
            db.close()

    return stats


def run_loop(interval_seconds: int = LOOP_INTERVAL_SECONDS):
    log.info(f"max_gain_updater loop starting, interval={interval_seconds}s")
    while True:
        try:
            update_max_gains()
        except Exception as e:
            log.error(f"max_gain cycle error: {e}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    if "--once" in sys.argv:
        update_max_gains()
    else:
        run_loop()