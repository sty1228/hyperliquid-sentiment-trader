"""
Trader API — Profile + Signals
"""
from __future__ import annotations
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc

from backend.deps import get_db
from backend.models.trader import Trader, TraderStats
from backend.models.signal import Signal

router = APIRouter(prefix="/api", tags=["trader"])


# ── Response 模型（匹配前端 UserSignalItem / UserSignalResponse）───

class SignalItemResponse(BaseModel):
    x_handle: str
    profit_grade: float | None = None
    signal_id: str
    entry_price: float = 0.0
    win_streak: int = 0
    progress_bar: float = 0.0
    user_week_total_pct: float | None = None
    ticker: str
    bull_or_bear: str  # "bullish" | "bearish"
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


# ── 工具函数 ─────────────────────────────────────────────

def _get_trader_or_404(db: Session, x_handle: str) -> Trader:
    trader = db.query(Trader).filter(Trader.username == x_handle).first()
    if not trader:
        raise HTTPException(404, f"Trader @{x_handle} not found")
    return trader


def _time_ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    delta = datetime.now(timezone.utc) - dt
    hours = int(delta.total_seconds() / 3600)
    if hours < 1:
        return f"{max(1, int(delta.total_seconds() / 60))}m ago"
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


# ── API 端点 ─────────────────────────────────────────────

@router.get("/user/{x_handle}/signals", response_model=UserSignalResponse)
def get_user_signals(
    x_handle: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    获取 Trader 的信号列表（前端 Signals tab）
    """
    trader = _get_trader_or_404(db, x_handle)

    # 获取 7d stats 中的 streak
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

    total_count = (
        db.query(Signal)
        .filter(Signal.trader_id == trader.id)
        .count()
    )

    signal_items = []
    for s in signals:
        # 计算 progress_bar（基于 entry → tp/sl 的进度）
        progress = 0.0
        if s.entry_price and s.current_price and s.tp_price:
            total_range = abs(s.tp_price - s.entry_price)
            if total_range > 0:
                progress = min(1.0, abs(s.current_price - s.entry_price) / total_range)

        signal_items.append(
            SignalItemResponse(
                x_handle=trader.username,
                profit_grade=s.pct_change,
                signal_id=s.id,
                entry_price=s.entry_price or 0.0,
                win_streak=stats_7d.streak if stats_7d else 0,
                progress_bar=progress,
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
            )
        )

    return UserSignalResponse(
        id=trader.id,
        name=trader.display_name or trader.username,
        tweetsCount=total_count,
        signals=signal_items,
    )


@router.get("/trader/{x_handle}/profile", response_model=TraderProfileResponse)
def get_trader_profile(
    x_handle: str,
    window: str = Query("7d", regex="^(24h|7d|30d)$"),
    db: Session = Depends(get_db),
):
    """
    获取 Trader 详情页数据
    """
    trader = _get_trader_or_404(db, x_handle)

    stats = (
        db.query(TraderStats)
        .filter(TraderStats.trader_id == trader.id, TraderStats.window == window)
        .first()
    )

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
    )