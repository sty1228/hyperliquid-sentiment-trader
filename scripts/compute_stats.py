"""
Compute trader_stats from signals data.
Run after ingestor to populate the leaderboard.

Usage:
    python -m scripts.compute_stats
    python -m scripts.compute_stats --window 7d
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, case, desc
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models.trader import Trader, TraderStats
from backend.models.signal import Signal


WINDOWS = {
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
}

GRADE_THRESHOLDS = [
    (50.0,  "S+"),
    (30.0,  "S"),
    (15.0,  "A"),
    (5.0,   "B"),
    (0.0,   "C"),
    (-999,  "D"),
]


def _calc_grade(avg_return: float) -> str:
    for threshold, grade in GRADE_THRESHOLDS:
        if avg_return >= threshold:
            return grade
    return "D"


def _calc_streak(session: Session, trader_id: str) -> int:
    """Calculate current win/loss streak from most recent signals."""
    signals = (
        session.query(Signal)
        .filter(Signal.trader_id == trader_id, Signal.status != "active")
        .order_by(desc(Signal.created_at))
        .limit(50)
        .all()
    )
    if not signals:
        return 0

    streak = 0
    first_direction = None
    for s in signals:
        is_win = s.status == "closed_win" or (s.pct_change and s.pct_change > 0)
        if first_direction is None:
            first_direction = is_win
        if is_win == first_direction:
            streak += 1
        else:
            break

    return streak if first_direction else -streak


def _calc_signal_to_noise(session: Session, trader_id: str, since: datetime) -> float:
    """Ratio of actionable signals to total tweets (higher = better)."""
    total = (
        session.query(func.count(Signal.id))
        .filter(Signal.trader_id == trader_id, Signal.created_at >= since)
        .scalar() or 0
    )
    if total == 0:
        return 0.0
    # In our DB, all signals are already filtered (NOISE removed by ingestor)
    # So signal_to_noise is based on how many have price movement
    with_movement = (
        session.query(func.count(Signal.id))
        .filter(
            Signal.trader_id == trader_id,
            Signal.created_at >= since,
            Signal.pct_change.isnot(None),
        )
        .scalar() or 0
    )
    return round(with_movement / total * 100, 1) if total > 0 else 0.0


def compute_window(session: Session, window: str, delta: timedelta):
    """Compute stats for all traders in a given time window."""
    since = datetime.now(timezone.utc) - delta
    print(f"\n{'='*50}")
    print(f"Computing stats for window: {window} (since {since.strftime('%Y-%m-%d %H:%M')})")
    print(f"{'='*50}")

    traders = session.query(Trader).all()
    stats_list = []

    for trader in traders:
        # Count signals in window
        signals = (
            session.query(Signal)
            .filter(Signal.trader_id == trader.id, Signal.created_at >= since)
            .all()
        )
        total = len(signals)
        if total == 0:
            continue

        # Win/loss (based on pct_change or status)
        wins = 0
        losses = 0
        total_return = 0.0
        returns = []

        for s in signals:
            pct = s.pct_change
            if pct is not None:
                returns.append(pct)
                total_return += pct
                if pct > 0:
                    wins += 1
                elif pct < 0:
                    losses += 1
            elif s.status == "closed_win":
                wins += 1
            elif s.status == "closed_loss":
                losses += 1

        decided = wins + losses
        win_rate = round(wins / decided * 100, 1) if decided > 0 else 0.0
        avg_return = round(total_return / len(returns), 2) if returns else 0.0

        # Points scoring system
        #   base = total_signals * 10
        #   win_rate bonus = win_rate * 2
        #   return bonus = avg_return * 5
        #   streak bonus
        streak = _calc_streak(session, trader.id)
        s2n = _calc_signal_to_noise(session, trader.id, since)

        points = (
            total * 10
            + win_rate * 2
            + max(avg_return, 0) * 5
            + max(streak, 0) * 15
            + s2n * 0.5
        )
        points = round(points, 1)

        grade = _calc_grade(avg_return)

        stats_list.append({
            "trader_id": trader.id,
            "window": window,
            "total_signals": total,
            "win_count": wins,
            "loss_count": losses,
            "win_rate": win_rate,
            "avg_return_pct": avg_return,
            "total_profit_usd": round(total_return, 2),
            "streak": streak,
            "points": points,
            "profit_grade": grade,
            "copiers_count": 0,
            "signal_to_noise": s2n,
        })

    # Rank by points
    stats_list.sort(key=lambda x: x["points"], reverse=True)
    for i, s in enumerate(stats_list):
        s["rank"] = i + 1

    # Upsert into DB
    for s in stats_list:
        existing = (
            session.query(TraderStats)
            .filter(TraderStats.trader_id == s["trader_id"], TraderStats.window == window)
            .first()
        )
        if existing:
            for k, v in s.items():
                if k != "trader_id" and k != "window":
                    setattr(existing, k, v)
            existing.computed_at = datetime.now(timezone.utc)
        else:
            existing = TraderStats(**s)
            session.add(existing)

    session.commit()
    print(f"  ✅ {len(stats_list)} traders computed for {window}")

    # Print top 10
    if stats_list:
        print(f"\n  Top 10 ({window}):")
        for s in stats_list[:10]:
            trader = session.query(Trader).filter(Trader.id == s["trader_id"]).first()
            name = trader.username if trader else "?"
            print(f"    #{s['rank']} @{name} — {s['total_signals']} signals, "
                  f"WR {s['win_rate']}%, avg {s['avg_return_pct']}%, "
                  f"grade {s['profit_grade']}, pts {s['points']}")


def run(windows: list[str] | None = None):
    session = SessionLocal()
    try:
        target_windows = windows or list(WINDOWS.keys())
        for w in target_windows:
            if w not in WINDOWS:
                print(f"  ⚠ Unknown window: {w}, skipping")
                continue
            compute_window(session, w, WINDOWS[w])

        # Summary
        total = session.query(TraderStats).count()
        print(f"\n{'='*50}")
        print(f"Done. Total trader_stats rows: {total}")
        print(f"{'='*50}")
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=str, default=None,
                        help="Specific window to compute (24h, 7d, 30d). Default: all")
    args = parser.parse_args()

    if args.window:
        run(windows=[args.window])
    else:
        run()