"""
排行榜 API — KOL Leaderboard
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc, exists

from backend.deps import get_db
from backend.models.trader import Trader, TraderStats
from backend.models.user import User

router = APIRouter(prefix="/api", tags=["leaderboard"])


class LeaderboardItemResponse(BaseModel):
    x_handle: str
    display_name: str | None = None
    avatar_url: str | None = None
    is_verified: bool = False
    bull_or_bear: str = "bullish"
    win_rate: float = 0.0
    total_tweets: int = 0
    signal_to_noise: float = 0.0
    results_pct: float = 0.0
    ticker: str = ""
    direction: str = ""
    how_long_ago: str = ""
    tweet_performance: float = 0.0
    copy_button: bool = True
    counter_button: bool = True
    profit_grade: str | None = None
    points: float = 0.0
    streak: int = 0
    rank: int = 0
    total_signals: int = 0
    avg_return: float = 0.0
    copiers: int = 0
    total_profit_usd: float = 0.0


# ── sort_by → column mapping ──
_SORT_COLS = {
    "total_profit_usd": TraderStats.total_profit_usd,
    "copiers_count":    TraderStats.copiers_count,
    "trending_score":   TraderStats.trending_score,
    "points":           TraderStats.points,
    "win_rate":         TraderStats.win_rate,
}


@router.get("/leaderboard", response_model=list[LeaderboardItemResponse])
def get_leaderboard(
    window: str = Query("24h", regex="^(24h|7d|30d)$"),
    sort_by: str = Query(
        "total_profit_usd",
        regex="^(total_profit_usd|copiers_count|trending_score|points|win_rate)$",
    ),
    registered_only: bool = Query(False),
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    KOL 排行榜
    - sort_by: total_profit_usd (earners) | copiers_count (copied) | trending_score (trending)
    - registered_only: true → 只显示在平台注册过的 trader (users.twitter_username 匹配)
    """
    sort_col = _SORT_COLS.get(sort_by, TraderStats.total_profit_usd)

    query = (
        db.query(TraderStats)
        .join(Trader, TraderStats.trader_id == Trader.id)
        .options(joinedload(TraderStats.trader))
        .filter(TraderStats.window == window, TraderStats.total_signals > 0)
    )

    # ★ Verified = 注册过平台的用户 (users 表有匹配的 twitter_username)
    if registered_only:
        query = query.filter(
            exists().where(User.twitter_username == Trader.username)
        )

    rows = (
        query
        .order_by(desc(sort_col))
        .offset(offset)
        .limit(limit)
        .all()
    )

    from backend.models.signal import Signal
    from datetime import datetime, timezone

    result = []
    for idx, stats in enumerate(rows, 1):
        trader = stats.trader

        latest_signal = (
            db.query(Signal)
            .filter(Signal.trader_id == trader.id)
            .order_by(desc(Signal.created_at))
            .first()
        )

        how_long_ago = ""
        ticker = ""
        direction = ""
        bull_or_bear = "bullish"

        if latest_signal:
            delta = datetime.now(timezone.utc) - (latest_signal.tweet_time or latest_signal.created_at)
            hours = int(delta.total_seconds() / 3600)
            if hours < 1:
                how_long_ago = f"{int(delta.total_seconds() / 60)}m ago"
            elif hours < 24:
                how_long_ago = f"{hours}h ago"
            else:
                how_long_ago = f"{hours // 24}d ago"

            ticker = latest_signal.ticker
            direction = latest_signal.direction
            bull_or_bear = latest_signal.sentiment or "bullish"

        if stats.signal_to_noise > 0:
            total_tweets = int(stats.total_signals / stats.signal_to_noise)
        else:
            total_tweets = stats.total_signals

        results_pct = round(stats.total_profit_usd, 2)
        tweet_performance = round(stats.avg_return_pct, 2)

        result.append(
            LeaderboardItemResponse(
                x_handle=trader.username,
                display_name=trader.display_name,
                avatar_url=trader.avatar_url,
                is_verified=trader.is_verified,
                bull_or_bear=bull_or_bear,
                win_rate=stats.win_rate,
                total_tweets=total_tweets,
                signal_to_noise=stats.signal_to_noise,
                results_pct=results_pct,
                ticker=ticker,
                direction=direction,
                how_long_ago=how_long_ago,
                tweet_performance=tweet_performance,
                copy_button=True,
                counter_button=True,
                profit_grade=stats.profit_grade,
                points=stats.points,
                streak=stats.streak,
                rank=idx,
                total_signals=stats.total_signals,
                avg_return=stats.avg_return_pct,
                copiers=stats.copiers_count,
                total_profit_usd=stats.total_profit_usd,
            )
        )

    return result