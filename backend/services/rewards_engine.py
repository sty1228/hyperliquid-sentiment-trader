"""
KOL Rewards Engine — computes points weekly, distributes fee shares.

Integration: called by trading_engine.py alongside recompute_stats().
  - recompute_kol_points(db)      → every 10 min (updates live points)
  - run_weekly_distribution(db)   → every 10 min (auto-triggers on new week)
"""

import logging
from datetime import datetime, timezone, timedelta, date
from sqlalchemy.orm import Session
from sqlalchemy import func as F

from backend.models.rewards import (
    KOLReward, KOLDistribution, DistributionStatus, ShareEvent,
)
from backend.models.user import User
from backend.models.trade import Trade
from backend.models.signal import Signal
from backend.models.trader import Trader

log = logging.getLogger("rewards_engine")

# ── Config ──────────────────────────────────────────────────
BETA_START = date(2026, 2, 28)
BETA_FEE_SHARE_PCT = 0.60        # 60% of builder fees → KOL pool
BUILDER_FEE_BPS = 10             # 0.1% per trade
MIN_SIGNALS_FOR_QUALITY = 3      # need ≥3 signals to earn quality bonus
SHARE_BOOST_PER_SHARE = 0.1     # +10% per share, capped at +100%
X_LINKED_BOOST = 1.5


# ── Week helpers ────────────────────────────────────────────

def current_week() -> int:
    """Week number since beta start (1-indexed)."""
    delta = (date.today() - BETA_START).days
    return max(delta // 7 + 1, 1)


def week_bounds(week: int) -> tuple[datetime, datetime]:
    """(start_utc, end_utc) for a given week number."""
    s = BETA_START + timedelta(weeks=week - 1)
    e = s + timedelta(weeks=1)
    return (
        datetime(s.year, s.month, s.day, tzinfo=timezone.utc),
        datetime(e.year, e.month, e.day, tzinfo=timezone.utc),
    )


# ── Core: recompute points (every 10 min) ──────────────────

def recompute_kol_points(db: Session):
    """
    Recalculate current_week_points for every KOL.
    A KOL = user whose twitter_username matches a trader.username.
    """
    week = current_week()
    w_start, w_end = week_bounds(week)

    # All KOL users
    kol_users = (
        db.query(User)
        .join(Trader, Trader.username == User.twitter_username)
        .filter(User.twitter_username.isnot(None))
        .all()
    )
    if not kol_users:
        return

    points_map: dict[str, int] = {}

    for u in kol_users:
        uname = u.twitter_username

        # ① Copy volume: $ others traded copying this KOL this week
        copy_vol = (
            db.query(F.coalesce(F.sum(Trade.size_usd), 0))
            .filter(
                Trade.trader_username == uname,
                Trade.source == "copy",
                Trade.opened_at >= w_start,
                Trade.opened_at < w_end,
            )
            .scalar()
        ) or 0.0
        copy_pts = int(copy_vol / 100)  # 1 pt per $100 copied

        # ② Signal quality this week
        sigs = (
            db.query(Signal)
            .join(Trader, Trader.id == Signal.trader_id)
            .filter(
                Trader.username == uname,
                F.coalesce(Signal.tweet_time, Signal.created_at) >= w_start,
                F.coalesce(Signal.tweet_time, Signal.created_at) < w_end,
                Signal.entry_price.isnot(None),
            )
            .all()
        )
        n_sigs = len(sigs)
        n_wins = sum(1 for s in sigs if (s.pct_change or 0) > 0)
        win_rate = n_wins / n_sigs if n_sigs > 0 else 0.0
        quality_pts = int(win_rate * 100) if n_sigs >= MIN_SIGNALS_FOR_QUALITY else 0

        # ③ X-account boost
        x_boost = X_LINKED_BOOST if u.twitter_username else 1.0

        # ④ Share boost (shares this week, capped at 2.0x)
        n_shares = (
            db.query(F.count(ShareEvent.id))
            .filter(
                ShareEvent.user_id == u.id,
                ShareEvent.created_at >= w_start,
                ShareEvent.created_at < w_end,
            )
            .scalar()
        ) or 0
        share_boost = 1.0 + min(n_shares * SHARE_BOOST_PER_SHARE, 1.0)

        # ⑤ Total
        raw = copy_pts + quality_pts
        total = int(raw * x_boost * share_boost)

        # ⑥ Upsert KOLReward
        rw = _get_or_create(db, u.id, uname)
        rw.current_week_points = total
        rw.x_account_linked = True
        rw.x_account_handle = uname
        rw.boost_multiplier = round(x_boost * share_boost, 2)
        rw.smart_follower_count = n_shares

        points_map[u.id] = total

    # ⑦ Rank
    ranked = sorted(points_map.items(), key=lambda x: x[1], reverse=True)
    for rank, (uid, _) in enumerate(ranked, 1):
        rw = db.query(KOLReward).filter(KOLReward.user_id == uid).first()
        if rw:
            rw.rank = rank

    db.commit()
    log.info("KOL points recomputed: %d KOLs, week %d", len(kol_users), week)


# ── Core: weekly distribution (auto-triggers on new week) ───

def run_weekly_distribution(db: Session):
    """
    Distribute fee shares for the previous completed week.
    Safe to call every 10 min — skips if already distributed.
    """
    prev_week = current_week() - 1
    if prev_week < 1:
        return  # still in week 1, nothing to distribute

    # Already distributed?
    exists = (
        db.query(KOLDistribution.id)
        .filter(KOLDistribution.week_number == prev_week)
        .first()
    )
    if exists:
        return

    w_start, w_end = week_bounds(prev_week)
    log.info("Running distribution for week %d", prev_week)

    # Total builder fees collected this week
    total_vol = (
        db.query(F.coalesce(F.sum(Trade.size_usd), 0))
        .filter(Trade.opened_at >= w_start, Trade.opened_at < w_end)
        .scalar()
    ) or 0.0
    total_fees = total_vol * BUILDER_FEE_BPS / 10_000
    kol_pool = total_fees * BETA_FEE_SHARE_PCT

    # KOLs with points > 0
    rewards = db.query(KOLReward).filter(KOLReward.current_week_points > 0).all()
    if not rewards:
        log.info("No KOLs with points for week %d, skipping", prev_week)
        return

    total_pts = sum(r.current_week_points for r in rewards)

    for r in rewards:
        share_usd = round(r.current_week_points / total_pts * kol_pool, 4) if total_pts > 0 else 0.0

        dist = KOLDistribution(
            user_id=r.user_id,
            week_number=prev_week,
            distribution_date=datetime.now(timezone.utc),
            total_points=r.current_week_points,
            copy_volume_points=r.current_week_points,  # detailed breakdown tracked live
            own_trading_points=0,
            signal_quality_bonus=0,
            x_account_boost=X_LINKED_BOOST if r.x_account_linked else 1.0,
            smart_follower_boost=r.boost_multiplier,
            fee_share_usdc=share_usd,
            status=DistributionStatus.paid,
        )
        db.add(dist)

        # Accumulate totals
        r.total_points += r.current_week_points
        r.total_fee_share += share_usd
        r.claimable_fee_share += share_usd
        r.current_week_points = 0  # reset for new week

    db.commit()
    log.info(
        "Week %d distributed: pool=$%.2f across %d KOLs",
        prev_week, kol_pool, len(rewards),
    )


# ── Helper ──────────────────────────────────────────────────

def _get_or_create(db: Session, user_id: str, x_handle: str | None = None) -> KOLReward:
    rw = db.query(KOLReward).filter(KOLReward.user_id == user_id).first()
    if not rw:
        rw = KOLReward(
            user_id=user_id,
            x_account_linked=bool(x_handle),
            x_account_handle=x_handle,
        )
        db.add(rw)
        db.flush()
    return rw