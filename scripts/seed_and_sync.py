"""
P0 Pipeline: Seed traders → Sync signals from CSV → Compute trader_stats
Usage:
    cd /opt/hypercopy  (or your project root)
    python -m scripts.seed_and_sync

    # Or directly:
    python scripts/seed_and_sync.py
"""
from __future__ import annotations
import os, sys, csv, hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from backend.database import SessionLocal, engine, Base
from backend.models.trader import Trader, TraderStats
from backend.models.signal import Signal
from backend.models.follow import Follow

# ─── KOL list (same as ingestor/main.py DEFAULT_USERS) ───────────

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

# ─── Helpers ──────────────────────────────────────────────────────

def _parse_dt(raw: str) -> Optional[datetime]:
    """Best-effort ISO parse, returns UTC datetime or None."""
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


def _tweet_hash(username: str, text: str, time_str: str) -> str:
    """Stable dedup key for a signal row."""
    blob = f"{username}|{text[:500]}|{time_str}"
    return hashlib.sha256(blob.encode()).hexdigest()


def _sentiment_to_direction(sentiment: str) -> str:
    s = (sentiment or "").lower().strip()
    if s == "bearish":
        return "short"
    return "long"  # bullish / neutral default


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


# ─── Step 1: Seed traders ────────────────────────────────────────

def seed_traders(db: Session) -> int:
    """Upsert DEFAULT_KOLS into traders table. Returns count of new inserts."""
    created = 0
    for username in DEFAULT_KOLS:
        exists = db.query(Trader.id).filter(Trader.username == username).first()
        if not exists:
            db.add(Trader(username=username, display_name=username))
            created += 1
    db.commit()
    print(f"[seed_traders] {created} new traders inserted, {len(DEFAULT_KOLS) - created} already existed")
    return created


# ─── Step 2: Sync CSV → signals ─────────────────────────────────

def sync_signals(db: Session, csv_path: str = CSV_PATH) -> int:
    """Read processed CSV and insert into signals table. Returns count of new signals."""
    if not os.path.exists(csv_path):
        print(f"[sync_signals] CSV not found at {csv_path} — skipping (run ingestor first)")
        return 0

    # Build trader username → id map
    traders = {t.username: t.id for t in db.query(Trader).all()}

    # Track existing signals to avoid duplicates
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

            # Skip noise
            if not username or not tweet or not ticker:
                skipped += 1
                continue
            if ticker in ("NOISE", "MARKET", ""):
                skipped += 1
                continue

            # Ensure trader exists
            if username not in traders:
                t = Trader(username=username, display_name=username)
                db.add(t)
                db.flush()
                traders[username] = t.id

            trader_id = traders[username]

            # Dedup check
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

            # Batch commit every 500
            if inserted % 500 == 0:
                db.commit()
                print(f"  ... {inserted} signals inserted so far")

    db.commit()
    print(f"[sync_signals] {inserted} new signals inserted, {skipped} skipped")
    return inserted


# ─── Step 3: Compute trader_stats ────────────────────────────────

def compute_stats(db: Session) -> int:
    """
    For each trader × window, compute stats from signals table and upsert into trader_stats.
    Returns total stats rows written.
    """
    now = datetime.now(timezone.utc)
    all_traders = db.query(Trader).all()
    written = 0

    for trader in all_traders:
        for window in WINDOWS:
            hours = _window_hours(window)
            cutoff = now - timedelta(hours=hours)

            # Signals in this window
            sigs = (
                db.query(Signal)
                .filter(
                    Signal.trader_id == trader.id,
                    Signal.created_at >= cutoff,
                )
                .all()
            )

            total = len(sigs)

            # Win/loss based on pct_change (if available)
            sigs_with_pnl = [s for s in sigs if s.pct_change is not None]
            wins = sum(1 for s in sigs_with_pnl if s.pct_change and s.pct_change > 0)
            losses = sum(1 for s in sigs_with_pnl if s.pct_change and s.pct_change <= 0)
            win_rate = (wins / len(sigs_with_pnl) * 100) if sigs_with_pnl else 0.0
            avg_return = (
                sum(s.pct_change for s in sigs_with_pnl) / len(sigs_with_pnl)
                if sigs_with_pnl else 0.0
            )
            total_profit = sum(s.pct_change or 0 for s in sigs_with_pnl)

            # Streak: consecutive wins from most recent
            streak = 0
            recent = sorted(sigs_with_pnl, key=lambda s: s.created_at or now, reverse=True)
            for s in recent:
                if s.pct_change and s.pct_change > 0:
                    streak += 1
                else:
                    break

            # Signal-to-noise: ratio of crypto signals vs total tweets
            # (simplified: we only have crypto signals in the table, so use total as proxy)
            stn = min(total / max(1, total + 2), 1.0)  # simple heuristic

            # Copiers count
            copiers = (
                db.query(func.count(Follow.id))
                .filter(Follow.trader_id == trader.id, Follow.is_copy_trading.is_(True))
                .scalar() or 0
            )

            # Points: composite score
            # Formula: win_rate_component + return_component + volume_component + streak_bonus
            wr_score = min(win_rate, 100) * 0.4  # max 40
            ret_score = min(max(avg_return, -50), 50) * 0.6  # max 30 (scaled)
            vol_score = min(total, 50) * 0.4  # max 20
            streak_score = min(streak, 10) * 1.0  # max 10
            points = max(0, wr_score + ret_score + vol_score + streak_score)

            grade = _grade(points)

            # Upsert
            existing = (
                db.query(TraderStats)
                .filter(TraderStats.trader_id == trader.id, TraderStats.window == window)
                .first()
            )
            if existing:
                existing.total_signals = total
                existing.win_count = wins
                existing.loss_count = losses
                existing.win_rate = round(win_rate, 2)
                existing.avg_return_pct = round(avg_return, 4)
                existing.total_profit_usd = round(total_profit, 2)
                existing.streak = streak
                existing.points = round(points, 2)
                existing.profit_grade = grade
                existing.copiers_count = copiers
                existing.signal_to_noise = round(stn, 3)
                existing.computed_at = now
            else:
                db.add(TraderStats(
                    trader_id=trader.id,
                    window=window,
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
                ))
            written += 1

    # Assign ranks per window (by points descending)
    for window in WINDOWS:
        stats_rows = (
            db.query(TraderStats)
            .filter(TraderStats.window == window)
            .order_by(desc(TraderStats.points))
            .all()
        )
        for rank, s in enumerate(stats_rows, 1):
            s.rank = rank

    db.commit()
    print(f"[compute_stats] {written} stats rows written ({len(all_traders)} traders × {len(WINDOWS)} windows)")
    return written


# ─── Main ─────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("HyperCopy P0: Seed → Sync → Stats")
    print("=" * 60)

    # Ensure tables exist
    Base.metadata.create_all(bind=engine)
    print("[init] Database tables verified\n")

    db = SessionLocal()
    try:
        # Step 1
        seed_traders(db)
        print()

        # Step 2
        sync_signals(db)
        print()

        # Step 3
        compute_stats(db)
        print()

        # Summary
        trader_count = db.query(func.count(Trader.id)).scalar()
        signal_count = db.query(func.count(Signal.id)).scalar()
        stats_count = db.query(func.count(TraderStats.id)).scalar()
        print("=" * 60)
        print(f"DONE  traders={trader_count}  signals={signal_count}  stats={stats_count}")
        print("=" * 60)
        print("\nVerify: curl https://your-domain/api/leaderboard?window=7d")

    finally:
        db.close()


if __name__ == "__main__":
    main()