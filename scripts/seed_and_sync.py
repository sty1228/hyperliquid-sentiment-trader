"""
P0 Pipeline: Seed traders → Sync signals from CSV → Compute trader_stats
Usage:
    cd /opt/hypercopy
    python -m scripts.seed_and_sync
"""
from __future__ import annotations
import os, sys, csv, hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from backend.database import SessionLocal, engine, Base
from backend.models.trader import Trader, TraderStats
from backend.models.signal import Signal
from backend.models.follow import Follow

# ─── KOL list ─────────────────────────────────────────────

DEFAULT_KOLS = [
    "Bluntz_Capital","TheWhiteWhaleHL","pierre_crypt0","Tradermayne","LomahCrypto",
    "Trader_XO","trader1sz","TedPillows","crypto_goos","Crypto_Chase",
    "KeyboardMonkey3","IncomeSharks","trader_koala","galaxyBTC","AltcoinSherpa",
    "CryptoAnup","blknoiz06","lBattleRhino","TheCryptoProfes","izebel_eth",
    "CryptoCaesarTA","Ashcryptoreal","cryptorangutang",
    "Numb3rsguy_","EtherWizz_",
    "CredibleCrypto","Pentosh1","basedkarbon","DJohnson_CPA","fundstrat",
    "CryptoHayes","ThinkingBitmex","TheBootMex","BastilleBtc","JamesWynnReal",
    "JustinCBram","MissionGains","ColeGarnersTake","R89Capital","RookieXBT",
    "ChainLinkGod","not_zkole","TimeFreedomROB","G7_base_eth","defi_mochi",
    "dennis_qian","noBScrypto",
]

CSV_PATH = os.path.join(PROJECT_ROOT, "data", "tweets_processed_complete.csv")
WINDOWS = ["24h", "7d", "30d"]


# ─── Helpers ──────────────────────────────────────────────

def _parse_dt(raw: str) -> Optional[datetime]:
    if not raw or raw == "nan":
        return None
    try:
        from dateutil.parser import parse as dp
        dt = dp(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _sentiment_to_direction(sentiment: str) -> str:
    s = (sentiment or "").lower().strip()
    if s == "bearish":
        return "short"
    return "long"


def _window_hours(w: str) -> int:
    return {"24h": 24, "7d": 168, "30d": 720}.get(w, 168)


def _grade(points: float) -> str:
    if points >= 85:
        return "S+"
    if points >= 70:
        return "S"
    if points >= 55:
        return "A"
    if points >= 35:
        return "B"
    return "C"


# ─── Step 1: Seed traders ────────────────────────────────

def seed_traders(db: Session) -> int:
    created = 0
    for username in DEFAULT_KOLS:
        exists = db.query(Trader.id).filter(Trader.username == username).first()
        if not exists:
            db.add(Trader(username=username, display_name=username))
            created += 1
    db.commit()
    print(f"[seed_traders] {created} new traders inserted, {len(DEFAULT_KOLS) - created} already existed")
    return created


# ─── Step 2: Sync CSV → signals ──────────────────────────

def sync_signals(db: Session, csv_path: str = CSV_PATH) -> int:
    if not os.path.exists(csv_path):
        print(f"[sync_signals] CSV not found at {csv_path} — skipping (run ingestor first)")
        return 0

    traders = {t.username: t.id for t in db.query(Trader).all()}

    existing_hashes: set[str] = set()
    for s in db.query(Signal.tweet_text, Signal.tweet_time).all():
        txt = (s.tweet_text or "")[:500]
        t = str(s.tweet_time or "")
        existing_hashes.add(hashlib.sha256(f"{txt}|{t}".encode()).hexdigest())

    inserted = 0
    skipped = 0

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            username = (row.get("username") or "").strip()
            tweet = (row.get("tweet") or "").strip()
            tweet_time_str = (row.get("tweet_time") or "").strip()
            ticker = (row.get("ticker") or "").upper().strip()
            sentiment = (row.get("sentiment") or "neutral").strip()

            if not username or not tweet or not ticker:
                skipped += 1
                continue
            if ticker in ("NOISE", "MARKET", ""):
                skipped += 1
                continue

            if username not in traders:
                t = Trader(username=username, display_name=username)
                db.add(t)
                db.flush()
                traders[username] = t.id

            trader_id = traders[username]

            dedup_key = hashlib.sha256(f"{tweet[:500]}|{tweet_time_str}".encode()).hexdigest()
            if dedup_key in existing_hashes:
                skipped += 1
                continue
            existing_hashes.add(dedup_key)

            tweet_dt = _parse_dt(tweet_time_str)
            direction = _sentiment_to_direction(sentiment)

            signal = Signal(
                trader_id=trader_id,
                tweet_text=tweet[:5000],
                ticker=ticker,
                direction=direction,
                sentiment=sentiment,
                tweet_time=tweet_dt,
                status="active",
            )
            db.add(signal)
            inserted += 1

            if inserted % 500 == 0:
                db.commit()
                print(f"  ... {inserted} signals inserted so far")

    db.commit()
    print(f"[sync_signals] {inserted} new signals inserted, {skipped} skipped")
    return inserted


# ─── Step 3: Compute trader_stats ────────────────────────

def compute_stats(db: Session) -> int:
    """
    For each trader × window, compute stats from signals table.

    Evaluation model:
    - Each signal = independent $100 trade, 1x leverage
    - Fixed 24h evaluation window: pct_change = price move 24h after signal
    - Direction-aware: long profits when price rises, short profits when price falls
    - Consecutive same-direction signals: each counted independently
    - total_profit_usd: based on $100 per trade (1% = $1)
    """
    now = datetime.now(timezone.utc)
    all_traders = db.query(Trader).all()
    written = 0

    for trader in all_traders:
        for window in WINDOWS:
            hours = _window_hours(window)
            cutoff = now - timedelta(hours=hours)

            # ── FIX: filter by tweet_time, not created_at ──
            sigs = (
                db.query(Signal)
                .filter(
                    Signal.trader_id == trader.id,
                    Signal.tweet_time.isnot(None),
                    Signal.tweet_time >= cutoff,
                )
                .order_by(Signal.tweet_time.asc())
                .all()
            )

            total = len(sigs)
            sigs_with_pnl = [s for s in sigs if s.pct_change is not None]

            # ── FIX: Direction-aware returns ──
            # pct_change = raw 24h price change (positive = price went up)
            # long:  profit = +pct_change  (price up = win)
            # short: profit = -pct_change  (price down = win)
            effective_returns = []
            for s in sigs_with_pnl:
                if s.direction == "short":
                    effective_returns.append(-s.pct_change)
                else:
                    effective_returns.append(s.pct_change)

            wins = sum(1 for r in effective_returns if r > 0)
            losses = sum(1 for r in effective_returns if r <= 0)
            win_rate = (wins / len(effective_returns) * 100) if effective_returns else 0.0
            avg_return = (sum(effective_returns) / len(effective_returns)) if effective_returns else 0.0

            # total_profit_usd: $100 per trade, so 1% change = $1
            total_profit = sum(effective_returns)

            # Streak: consecutive wins from most recent
            streak = 0
            for r in reversed(effective_returns):
                if r > 0:
                    streak += 1
                else:
                    break

            # Signal-to-noise: ratio of signals with price data vs total
            stn = len(sigs_with_pnl) / max(total, 1) if total > 0 else 0.0

            # Copiers count
            copiers = (
                db.query(func.count(Follow.id))
                .filter(Follow.trader_id == trader.id, Follow.is_copy_trading.is_(True))
                .scalar() or 0
            )

            # Points: composite score (max ~100)
            wr_score = min(win_rate, 100) * 0.4       # max 40
            ret_score = min(max(avg_return, -50), 50) * 0.6  # max 30
            vol_score = min(total, 50) * 0.4           # max 20
            streak_score = min(streak, 10) * 1.0       # max 10
            points = max(0, wr_score + ret_score + vol_score + streak_score)

            grade = _grade(points)

            # Upsert
            existing = (
                db.query(TraderStats)
                .filter(TraderStats.trader_id == trader.id, TraderStats.window == window)
                .first()
            )
            vals = dict(
                total_signals=total,
                win_count=wins,
                loss_count=losses,
                win_rate=round(win_rate, 2),
                avg_return_pct=round(avg_return, 4),
                total_profit_usd=round(total_profit, 2),
                streak=streak,
                points=round(points, 2),
                profit_grade=grade,
                copiers_count=copiers,
                signal_to_noise=round(stn, 3),
                computed_at=now,
            )
            if existing:
                for k, v in vals.items():
                    setattr(existing, k, v)
            else:
                db.add(TraderStats(trader_id=trader.id, window=window, **vals))
            written += 1

    # Assign ranks per window by total_profit_usd (Top Earners)
    for window in WINDOWS:
        stats_rows = (
            db.query(TraderStats)
            .filter(TraderStats.window == window)
            .order_by(desc(TraderStats.total_profit_usd))
            .all()
        )
        for rank, s in enumerate(stats_rows, 1):
            s.rank = rank

    db.commit()
    print(f"[compute_stats] {written} stats rows written ({len(all_traders)} traders × {len(WINDOWS)} windows)")
    return written


# ─── Main ─────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("HyperCopy P0: Seed → Sync → Stats")
    print("=" * 60)

    Base.metadata.create_all(bind=engine)
    print("[init] Database tables verified\n")

    db = SessionLocal()
    try:
        seed_traders(db)
        print()
        sync_signals(db)
        print()
        compute_stats(db)
        print()

        trader_count = db.query(func.count(Trader.id)).scalar()
        signal_count = db.query(func.count(Signal.id)).scalar()
        stats_count = db.query(func.count(TraderStats.id)).scalar()
        print("=" * 60)
        print(f"DONE  traders={trader_count}  signals={signal_count}  stats={stats_count}")
        print("=" * 60)
    finally:
        db.close()


if __name__ == "__main__":
    main()