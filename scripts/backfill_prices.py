"""
Backfill prices for existing signals using Hyperliquid public API.
Then re-compute trader_stats.

Usage:
    cd /opt/hypercopy
    ./venv/bin/python3 scripts/backfill_prices.py
"""
from __future__ import annotations
import os, sys, time, requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import func
from sqlalchemy.orm import Session
from backend.database import SessionLocal, engine, Base
from backend.models.signal import Signal
from backend.models.trader import Trader, TraderStats
from backend.models.follow import Follow

# ─── Hyperliquid public API ──────────────────────────────────────

HL_INFO = "https://api.hyperliquid.xyz/info"
SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

SKIP_TICKERS = {
    "NOISE", "MARKET", "USDT", "USDC", "USD", "PUMP", "WIFE", "CARDS",
    "SBET", "AVNT", "JOJO", "CRYPTO", "DEFI", "NFT", "WEB3", "MEME",
    "TOKEN", "COIN", "BULL", "BEAR", "LONG", "SHORT",
}

# Symbols that need "k" prefix on Hyperliquid (price in thousands)
K_PREFIX = {"PEPE", "BONK", "SHIB", "FLOKI", "LUNC", "DOGS", "NEIRO"}


def _fetch_all_mids() -> Dict[str, float]:
    """One call to get all mid prices."""
    r = SESSION.post(HL_INFO, json={"type": "allMids"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    out = {}
    for k, v in data.items():
        try:
            out[k.upper()] = float(v)
        except (ValueError, TypeError):
            continue
    return out


def _resolve_symbol(ticker: str, mids: Dict[str, float]) -> Optional[tuple[str, float]]:
    """Find ticker in mids dict. Returns (hl_symbol, price) or None."""
    t = ticker.upper().strip()

    # Strip common suffixes
    for suf in ("USDT", "USDC", "USD", "-PERP"):
        if t.endswith(suf):
            t = t[:-len(suf)]

    # Direct match
    if t in mids:
        return t, mids[t]

    # Try with k prefix (PEPE -> kPEPE)
    kt = "k" + t
    if kt in mids:
        return kt, mids[kt]

    return None


def _get_entry_price(hl_symbol: str, at_dt: datetime) -> Optional[float]:
    """Get historical price via candleSnapshot."""
    target_ms = int(at_dt.timestamp() * 1000)
    start_ms = target_ms - 30 * 60_000
    end_ms = target_ms + 30 * 60_000

    try:
        body = {
            "type": "candleSnapshot",
            "req": {
                "coin": hl_symbol,
                "interval": 60,  # 1min in seconds
                "startTime": start_ms,
                "endTime": end_ms,
            },
        }
        r = SESSION.post(HL_INFO, json=body, timeout=10)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, list) and data:
            # Find closest candle
            closest = min(data, key=lambda c: abs(int(c.get("t", 0)) - target_ms))
            return float(closest.get("o", 0))  # open price
        if isinstance(data, dict):
            # Sometimes nested
            candles = data.get("candles", data.get("data", []))
            if candles:
                closest = min(candles, key=lambda c: abs(int(c[0]) - target_ms))
                return float(closest[1])  # open price
    except Exception as e:
        pass

    return None


def _calc_pct(entry: float, current: float, direction: str) -> float:
    if entry <= 0:
        return 0.0
    if direction == "short":
        return ((entry - current) / entry) * 100.0
    return ((current - entry) / entry) * 100.0


# ─── Backfill ─────────────────────────────────────────────────────

def backfill_prices(db: Session) -> int:
    print("[backfill] Fetching all mid prices from Hyperliquid...")
    mids = _fetch_all_mids()
    print(f"[backfill] Got {len(mids)} symbols")

    signals = (
        db.query(Signal)
        .filter(Signal.ticker.isnot(None))
        .order_by(Signal.created_at.desc())
        .all()
    )
    print(f"[backfill] Total signals: {len(signals)}")

    updated = 0
    skipped = 0
    no_match = set()
    entry_cache: Dict[str, Optional[float]] = {}

    for i, sig in enumerate(signals):
        ticker = (sig.ticker or "").upper().strip()
        if ticker in SKIP_TICKERS or len(ticker) < 2:
            skipped += 1
            continue

        resolved = _resolve_symbol(ticker, mids)
        if resolved is None:
            if ticker not in no_match:
                no_match.add(ticker)
            skipped += 1
            continue

        hl_sym, cur_price = resolved

        # Get entry price at tweet_time
        entry_price = sig.entry_price
        if entry_price is None and sig.tweet_time:
            cache_key = f"{hl_sym}|{int(sig.tweet_time.timestamp() // 300)}"
            if cache_key not in entry_cache:
                entry_cache[cache_key] = _get_entry_price(hl_sym, sig.tweet_time)
                time.sleep(0.05)
            entry_price = entry_cache[cache_key]

        # Fallback: use current price (for very old tweets with no kline data)
        if entry_price is None:
            entry_price = cur_price

        # Handle kPEPE etc: HL returns price in thousands, so kPEPE=0.003854 means PEPE=0.000003854
        # But since both entry and current use the same scale, pct_change is still correct

        pct = _calc_pct(entry_price, cur_price, sig.direction or "long")

        sig.entry_price = round(entry_price, 8)
        sig.current_price = round(cur_price, 8)
        sig.pct_change = round(pct, 4)
        sig.status = "closed_win" if pct > 0 else "closed_loss" if pct < 0 else "active"
        updated += 1

        if updated % 100 == 0:
            db.commit()
            print(f"  ... {updated} signals updated ({i+1}/{len(signals)})")

    db.commit()

    if no_match:
        print(f"[backfill] Tickers not found on Hyperliquid ({len(no_match)}): {sorted(no_match)}")
    print(f"[backfill] Done: {updated} updated, {skipped} skipped")
    return updated


# ─── Re-compute stats ────────────────────────────────────────────

WINDOWS = ["24h", "7d", "30d"]

def _window_hours(w: str) -> int:
    return {"24h": 24, "7d": 168, "30d": 720}.get(w, 168)

def _grade(points: float) -> str:
    if points >= 85: return "S+"
    if points >= 70: return "S"
    if points >= 55: return "A"
    if points >= 35: return "B"
    return "C"

def compute_stats(db: Session) -> int:
    from sqlalchemy import desc as sa_desc
    now = datetime.now(timezone.utc)
    all_traders = db.query(Trader).all()
    written = 0

    for trader in all_traders:
        for window in WINDOWS:
            hours = _window_hours(window)
            cutoff = now - timedelta(hours=hours)

            sigs = (
                db.query(Signal)
                .filter(Signal.trader_id == trader.id, Signal.created_at >= cutoff)
                .all()
            )
            total = len(sigs)

            sigs_pnl = [s for s in sigs if s.pct_change is not None]
            wins = sum(1 for s in sigs_pnl if s.pct_change and s.pct_change > 0)
            losses = sum(1 for s in sigs_pnl if s.pct_change is not None and s.pct_change <= 0)
            win_rate = (wins / len(sigs_pnl) * 100) if sigs_pnl else 0.0
            avg_ret = (sum(s.pct_change for s in sigs_pnl) / len(sigs_pnl)) if sigs_pnl else 0.0
            total_profit = sum(s.pct_change or 0 for s in sigs_pnl)

            streak = 0
            recent = sorted(sigs_pnl, key=lambda s: s.created_at or now, reverse=True)
            for s in recent:
                if s.pct_change and s.pct_change > 0:
                    streak += 1
                else:
                    break

            stn = min(total / max(1, total + 2), 1.0)
            copiers = db.query(func.count(Follow.id)).filter(
                Follow.trader_id == trader.id, Follow.is_copy_trading.is_(True)
            ).scalar() or 0

            wr_score = min(win_rate, 100) * 0.4
            ret_score = min(max(avg_ret, -50), 50) * 0.6
            vol_score = min(total, 50) * 0.4
            streak_score = min(streak, 10) * 1.0
            points = max(0, wr_score + ret_score + vol_score + streak_score)
            grade = _grade(points)

            existing = (
                db.query(TraderStats)
                .filter(TraderStats.trader_id == trader.id, TraderStats.window == window)
                .first()
            )
            vals = dict(
                total_signals=total, win_count=wins, loss_count=losses,
                win_rate=round(win_rate, 2), avg_return_pct=round(avg_ret, 4),
                total_profit_usd=round(total_profit, 2), streak=streak,
                points=round(points, 2), profit_grade=grade,
                copiers_count=copiers, signal_to_noise=round(stn, 3),
                computed_at=now,
            )
            if existing:
                for k, v in vals.items():
                    setattr(existing, k, v)
            else:
                db.add(TraderStats(trader_id=trader.id, window=window, **vals))
            written += 1

    for window in WINDOWS:
        rows = (
            db.query(TraderStats)
            .filter(TraderStats.window == window)
            .order_by(sa_desc(TraderStats.points))
            .all()
        )
        for rank, s in enumerate(rows, 1):
            s.rank = rank

    db.commit()
    print(f"[compute_stats] {written} stats rows updated")
    return written


# ─── Main ─────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("HyperCopy: Backfill Prices (Hyperliquid) + Recompute Stats")
    print("=" * 60)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        backfill_prices(db)
        print()
        compute_stats(db)
        print()

        total_sigs = db.query(func.count(Signal.id)).scalar()
        with_pnl = db.query(func.count(Signal.id)).filter(Signal.pct_change.isnot(None)).scalar()
        print("=" * 60)
        print(f"DONE  signals={total_sigs}  with_pnl={with_pnl}")
        print("=" * 60)
    finally:
        db.close()


if __name__ == "__main__":
    main()