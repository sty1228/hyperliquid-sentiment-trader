"""
排行榜 API — KOL Leaderboard
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc

from backend.deps import get_db
from backend.models.trader import Trader, TraderStats

router = APIRouter(prefix="/api", tags=["leaderboard"])


# ── Response 模型（匹配前端 LeaderboardItem）─────────────

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


# ── API 端点 ─────────────────────────────────────────────

@router.get("/leaderboard", response_model=list[LeaderboardItemResponse])
def get_leaderboard(
    window: str = Query("24h", regex="^(24h|7d|30d)$"),
    sort_by: str = Query("total_profit_usd", regex="^(points|win_rate|total_profit_usd|copiers_count)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    KOL 排行榜
    - window: 24h / 7d / 30d
    - sort_by: points / win_rate / total_profit_usd / copiers_count
    """
    # 查询 TraderStats + 关联 Trader
    sort_col = getattr(TraderStats, sort_by, TraderStats.total_profit_usd)

    rows = (
        db.query(TraderStats)
        .options(joinedload(TraderStats.trader))
        .filter(TraderStats.window == window)
        .order_by(desc(sort_col))
        .offset(offset)
        .limit(limit)
        .all()
    )

    # 查每个 trader 最近一条 signal（用于 how_long_ago / ticker / direction）
    from backend.models.signal import Signal
    from datetime import datetime, timezone

    result = []
    for stats in rows:
        trader = stats.trader

        # 获取最新 signal
        latest_signal = (
            db.query(Signal)
            .filter(Signal.trader_id == trader.id)
            .order_by(desc(Signal.created_at))
            .first()
        )

        # 计算 how_long_ago
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

        result.append(
            LeaderboardItemResponse(
                x_handle=trader.username,
                display_name=trader.display_name,
                avatar_url=trader.avatar_url,
                is_verified=trader.is_verified,
                bull_or_bear=bull_or_bear,
                win_rate=stats.win_rate,
                total_tweets=stats.total_signals,
                signal_to_noise=stats.signal_to_noise,
                results_pct=stats.avg_return_pct,
                ticker=ticker,
                direction=direction,
                how_long_ago=how_long_ago,
                tweet_performance=0.0,
                copy_button=True,
                counter_button=True,
                profit_grade=stats.profit_grade,
                points=stats.points,
                streak=stats.streak,
                rank=stats.rank or 0,
                total_signals=stats.total_signals,
                avg_return=stats.avg_return_pct,
                copiers=stats.copiers_count,
                total_profit_usd=stats.total_profit_usd,
            )
        )

    return result