"""
Trader API — Profile + Signals + Radar Computation
"""
from __future__ import annotations

import math
import statistics as stats_lib
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.deps import get_db, get_optional_user
from backend.models.follow import Follow
from backend.models.signal import Signal
from backend.models.trader import Trader, TraderStats
from backend.models.user import User

router = APIRouter(prefix="/api", tags=["trader"])


# ── Response Models ──────────────────────────────────────

class RadarData(BaseModel):
    accuracy: int = 0
    winRate: int = 0
    riskReward: int = 0
    consistency: int = 0
    timing: int = 0
    transparency: int = 0
    engagement: int = 0
    trackRecord: int = 0


class BestWorstSignal(BaseModel):
    token: str
    pnl: float
    date: str


class SignalItemResponse(BaseModel):
    x_handle: str
    profit_grade: float | None = None
    signal_id: str
    entry_price: float = 0.0
    win_streak: int = 0
    progress_bar: float = 0.0
    user_week_total_pct: float | None = None
    ticker: str
    bull_or_bear: str
    emotionType: int = 0
    updateTime: str = ""
    content: str = ""
    commentsCount: int = 0
    retweetsCount: int = 0
    likesCount: int = 0
    change_since_tweet: float = 0.0


class UserSignalResponse(BaseModel):
    id: str
    name: str
    tweetsCount: int
    signals: list[SignalItemResponse]


class TraderProfileResponse(BaseModel):
    id: str
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    is_verified: bool = False
    followers_count: int = 0
    following_count: int = 0
    # Stats (for requested window)
    total_signals: int = 0
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_profit_usd: float = 0.0
    streak: int = 0
    points: float = 0.0
    profit_grade: str | None = None
    rank: int | None = None
    copiers_count: int = 0
    signal_to_noise: float = 0.0
    # Radar (8 dimensions, 0-100 each)
    radar: RadarData = RadarData()
    # Follow state (requires auth, else false)
    is_followed: bool = False
    is_copy_trading: bool = False
    # Best / worst signal
    best_signal: BestWorstSignal | None = None
    worst_signal: BestWorstSignal | None = None


# ── Radar Computation ────────────────────────────────────

def _clamp(v: float, lo: float = 0, hi: float = 100) -> int:
    """Clamp value to [lo, hi] and return int."""
    return int(max(lo, min(hi, v)))


def _compute_radar(
    signals: list[Signal],
    all_stats: dict[str, TraderStats | None],
) -> RadarData:
    """
    Compute 8-dimension radar scores (0-100 each).

    Dimensions:
    1. Accuracy   — 30d win_rate (long-term)
    2. Win Rate   — 7d win_rate (current form)
    3. R/R Ratio  — avg win% / avg loss%, capped at 3x = 100
    4. Consistency — std dev of win_rate across 24h/7d/30d windows
    5. Timing     — direction-match ratio, exponential time-decay weighting
    6. Transparency— entry(25) + TP(25) + SL(25) + meaningful tweet text(25)
    7. Engagement  — log-scale (likes + retweets×2 + replies×1.5), normalized
    8. Track Record— min(signals/100,1)×50 + min(active_days/90,1)×50
    """
    s24 = all_stats.get("24h")
    s7 = all_stats.get("7d")
    s30 = all_stats.get("30d")

    if not signals and not any([s24, s7, s30]):
        return RadarData()

    # ── 1. Accuracy (30d > 7d > 24h fallback) ──
    acc_stat = s30 or s7 or s24
    accuracy = _clamp(acc_stat.win_rate * 100) if acc_stat else 0

    # ── 2. Win Rate (7d > 30d > 24h fallback) ──
    wr_stat = s7 or s30 or s24
    win_rate = _clamp(wr_stat.win_rate * 100) if wr_stat else 0

    # ── 3. R/R Ratio ──
    wins = [s for s in signals if s.pct_change is not None and s.pct_change > 0]
    losses = [s for s in signals if s.pct_change is not None and s.pct_change < 0]
    avg_w = (sum(s.pct_change for s in wins) / len(wins)) if wins else 0
    avg_l = abs(sum(s.pct_change for s in losses) / len(losses)) if losses else 1
    raw_rr = avg_w / max(avg_l, 0.01)
    # R/R 1.0 → 33, R/R 2.0 → 67, R/R 3.0+ → 100
    rr_score = _clamp(raw_rr / 3.0 * 100)

    # ── 4. Consistency ──
    rates = [st.win_rate for st in [s24, s7, s30] if st and st.total_signals > 0]
    if len(rates) >= 2:
        std = stats_lib.stdev(rates)
        # std 0 → 100, std ≥ 0.3 → 0
        consistency = _clamp((1 - std / 0.3) * 100)
    elif len(rates) == 1:
        consistency = 50  # insufficient windows
    else:
        consistency = 0

    # ── 5. Timing ──
    now = datetime.now(timezone.utc)
    w_sum = w_tot = 0.0
    for sig in signals:
        if sig.pct_change is None or not sig.direction:
            continue
        correct = (
            (sig.direction == "long" and sig.pct_change > 0)
            or (sig.direction == "short" and sig.pct_change < 0)
        )
        dt = sig.tweet_time or sig.created_at
        age_days = max(0, (now - dt).total_seconds() / 86400) if dt else 7
        w = math.exp(-0.1 * age_days)  # half-life ≈ 7 days
        w_sum += w * (1.0 if correct else 0.0)
        w_tot += w
    timing = _clamp((w_sum / w_tot) * 100) if w_tot > 0 else 0

    # ── 6. Transparency ──
    if signals:
        n = len(signals)
        entry_pts = sum(1 for s in signals if s.entry_price is not None) * 25 / n
        tp_pts = sum(1 for s in signals if s.tp_price is not None) * 25 / n
        sl_pts = sum(1 for s in signals if s.sl_price is not None) * 25 / n
        text_pts = sum(
            1 for s in signals
            if s.tweet_text and len(s.tweet_text.strip()) > len(s.ticker) + 5
        ) * 25 / n
        transparency = _clamp(entry_pts + tp_pts + sl_pts + text_pts)
    else:
        transparency = 0

    # ── 7. Engagement ──
    if signals:
        scores = [
            math.log10(s.likes + 1)
            + math.log10(s.retweets * 2 + 1)
            + math.log10(s.replies * 1.5 + 1)
            for s in signals
        ]
        avg_eng = sum(scores) / len(scores)
        # score 8+ → 100 (big KOL), score 4 → 50 (mid), score 0 → 0
        engagement = _clamp(avg_eng / 8.0 * 100)
    else:
        engagement = 0

    # ── 8. Track Record ──
    total_n = len(signals)
    dates = [
        s.tweet_time or s.created_at
        for s in signals
        if (s.tweet_time or s.created_at)
    ]
    active_days = (max(dates) - min(dates)).days + 1 if len(dates) >= 2 else 0
    track_record = _clamp(
        min(total_n / 100, 1.0) * 50 + min(active_days / 90, 1.0) * 50
    )

    return RadarData(
        accuracy=accuracy,
        winRate=win_rate,
        riskReward=rr_score,
        consistency=consistency,
        timing=timing,
        transparency=transparency,
        engagement=engagement,
        trackRecord=track_record,
    )


# ── Helpers ──────────────────────────────────────────────

def _get_trader_or_404(db: Session, x_handle: str) -> Trader:
    t = db.query(Trader).filter(Trader.username == x_handle).first()
    if not t:
        raise HTTPException(404, f"Trader @{x_handle} not found")
    return t


def _time_ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    delta = datetime.now(timezone.utc) - dt
    h = int(delta.total_seconds() / 3600)
    if h < 1:
        return f"{max(1, int(delta.total_seconds() / 60))}m ago"
    if h < 24:
        return f"{h}h ago"
    return f"{h // 24}d ago"


def _best_worst(
    signals: list[Signal],
) -> tuple[BestWorstSignal | None, BestWorstSignal | None]:
    """Find best and worst signals by pct_change."""
    valid = [s for s in signals if s.pct_change is not None]
    if not valid:
        return None, None

    def fmt(s: Signal) -> BestWorstSignal:
        dt = s.tweet_time or s.created_at
        return BestWorstSignal(
            token=s.ticker,
            pnl=round(s.pct_change, 1),
            date=dt.strftime("%b %d") if dt else "",
        )

    return (
        fmt(max(valid, key=lambda s: s.pct_change)),
        fmt(min(valid, key=lambda s: s.pct_change)),
    )


# ── API Endpoints ────────────────────────────────────────

@router.get("/trader/{x_handle}/profile", response_model=TraderProfileResponse)
def get_trader_profile(
    x_handle: str,
    window: str = Query("7d", regex="^(24h|7d|30d)$"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """
    Trader profile page: basic info + stats + radar + follow state.
    Auth is optional — if JWT present, returns is_followed/is_copy_trading.
    """
    trader = _get_trader_or_404(db, x_handle)

    # All 3 windows for radar consistency calculation
    all_stats: dict[str, TraderStats | None] = {}
    for w in ["24h", "7d", "30d"]:
        all_stats[w] = (
            db.query(TraderStats)
            .filter(TraderStats.trader_id == trader.id, TraderStats.window == w)
            .first()
        )
    stats = all_stats.get(window)

    # Signals for radar + best/worst (cap at 500 for perf)
    signals = (
        db.query(Signal)
        .filter(Signal.trader_id == trader.id)
        .order_by(desc(Signal.created_at))
        .limit(500)
        .all()
    )

    radar = _compute_radar(signals, all_stats)
    best, worst = _best_worst(signals)

    # Follow state
    is_followed = is_copy = False
    if current_user:
        follow = (
            db.query(Follow)
            .filter(
                Follow.user_id == current_user.id,
                Follow.trader_id == trader.id,
            )
            .first()
        )
        if follow:
            is_followed = True
            is_copy = follow.is_copy_trading

    return TraderProfileResponse(
        id=trader.id,
        username=trader.username,
        display_name=trader.display_name,
        avatar_url=trader.avatar_url,
        bio=trader.bio,
        is_verified=trader.is_verified,
        followers_count=trader.followers_count,
        following_count=trader.following_count,
        total_signals=stats.total_signals if stats else 0,
        win_rate=stats.win_rate if stats else 0.0,
        avg_return_pct=stats.avg_return_pct if stats else 0.0,
        total_profit_usd=stats.total_profit_usd if stats else 0.0,
        streak=stats.streak if stats else 0,
        points=stats.points if stats else 0.0,
        profit_grade=stats.profit_grade if stats else None,
        rank=stats.rank if stats else None,
        copiers_count=stats.copiers_count if stats else 0,
        signal_to_noise=stats.signal_to_noise if stats else 0.0,
        radar=radar,
        is_followed=is_followed,
        is_copy_trading=is_copy,
        best_signal=best,
        worst_signal=worst,
    )


@router.get("/user/{x_handle}/signals", response_model=UserSignalResponse)
def get_user_signals(
    x_handle: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Trader signal list (for Signals tab)."""
    trader = _get_trader_or_404(db, x_handle)

    stats_7d = (
        db.query(TraderStats)
        .filter(TraderStats.trader_id == trader.id, TraderStats.window == "7d")
        .first()
    )

    signals = (
        db.query(Signal)
        .filter(Signal.trader_id == trader.id)
        .order_by(desc(Signal.created_at))
        .offset(offset)
        .limit(limit)
        .all()
    )

    total = db.query(Signal).filter(Signal.trader_id == trader.id).count()

    items = []
    for s in signals:
        prog = 0.0
        if s.entry_price and s.current_price and s.tp_price:
            rng = abs(s.tp_price - s.entry_price)
            if rng > 0:
                prog = min(1.0, abs(s.current_price - s.entry_price) / rng)

        items.append(SignalItemResponse(
            x_handle=trader.username,
            profit_grade=s.pct_change,
            signal_id=s.id,
            entry_price=s.entry_price or 0.0,
            win_streak=stats_7d.streak if stats_7d else 0,
            progress_bar=prog,
            user_week_total_pct=stats_7d.avg_return_pct if stats_7d else None,
            ticker=s.ticker,
            bull_or_bear=s.sentiment or "bullish",
            emotionType=1 if s.sentiment == "bullish" else 2 if s.sentiment == "bearish" else 0,
            updateTime=_time_ago(s.tweet_time or s.created_at),
            content=s.tweet_text or "",
            commentsCount=s.replies,
            retweetsCount=s.retweets,
            likesCount=s.likes,
            change_since_tweet=s.pct_change or 0.0,
        ))

    return UserSignalResponse(
        id=trader.id,
        name=trader.display_name or trader.username,
        tweetsCount=total,
        signals=items,
    )