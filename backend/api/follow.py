"""
Follow API — 关注/取关 Trader，含 stats
"""
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.trader import Trader, TraderStats
from backend.models.follow import Follow

router = APIRouter(prefix="/api", tags=["follow"])


# ── Request / Response 模型 ──────────────────────────────

class FollowRequest(BaseModel):
    trader_username: str
    is_copy_trading: bool = False


class FollowResponse(BaseModel):
    id: str
    trader_username: str
    is_copy_trading: bool
    created_at: datetime


class FollowListItem(BaseModel):
    id: str
    trader_username: str
    display_name: str | None = None
    avatar_url: str | None = None
    is_copy_trading: bool
    created_at: datetime
    # Stats
    win_rate: float = 0.0
    total_profit_usd: float = 0.0
    total_signals: int = 0
    avg_return_pct: float = 0.0
    profit_grade: str | None = None


# ── API 端点 ─────────────────────────────────────────────

@router.post("/follow", response_model=FollowResponse)
def follow_trader(
    body: FollowRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """关注一个 Trader"""
    trader = db.query(Trader).filter(Trader.username == body.trader_username).first()
    if not trader:
        raise HTTPException(404, f"Trader @{body.trader_username} not found")

    existing = (
        db.query(Follow)
        .filter(Follow.user_id == current_user.id, Follow.trader_id == trader.id)
        .first()
    )
    if existing:
        raise HTTPException(400, "Already following this trader")

    follow = Follow(
        user_id=current_user.id,
        trader_id=trader.id,
        is_copy_trading=body.is_copy_trading,
    )
    db.add(follow)
    db.commit()
    db.refresh(follow)

    return FollowResponse(
        id=follow.id,
        trader_username=trader.username,
        is_copy_trading=follow.is_copy_trading,
        created_at=follow.created_at,
    )


@router.delete("/follow/{trader_username}")
def unfollow_trader(
    trader_username: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取关一个 Trader"""
    trader = db.query(Trader).filter(Trader.username == trader_username).first()
    if not trader:
        raise HTTPException(404, f"Trader @{trader_username} not found")

    follow = (
        db.query(Follow)
        .filter(Follow.user_id == current_user.id, Follow.trader_id == trader.id)
        .first()
    )
    if not follow:
        raise HTTPException(404, "Not following this trader")

    db.delete(follow)
    db.commit()
    return {"message": f"Unfollowed @{trader_username}"}


@router.get("/follows", response_model=list[FollowListItem])
def get_my_follows(
    window: str = Query("30d", regex="^(24h|7d|30d)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取我关注的所有 Trader（含 stats）"""
    follows = (
        db.query(Follow)
        .filter(Follow.user_id == current_user.id)
        .order_by(Follow.created_at.desc())
        .all()
    )

    result = []
    for f in follows:
        trader = db.query(Trader).filter(Trader.id == f.trader_id).first()
        if not trader:
            continue

        # 取对应窗口的 stats
        stats = (
            db.query(TraderStats)
            .filter(TraderStats.trader_id == trader.id, TraderStats.window == window)
            .first()
        )

        result.append(FollowListItem(
            id=f.id,
            trader_username=trader.username,
            display_name=trader.display_name,
            avatar_url=trader.avatar_url,
            is_copy_trading=f.is_copy_trading,
            created_at=f.created_at,
            win_rate=stats.win_rate if stats else 0.0,
            total_profit_usd=stats.total_profit_usd if stats else 0.0,
            total_signals=stats.total_signals if stats else 0,
            avg_return_pct=stats.avg_return_pct if stats else 0.0,
            profit_grade=stats.profit_grade if stats else None,
        ))
    return result


@router.patch("/follow/{trader_username}/copy-trading")
def toggle_copy_trading(
    trader_username: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """开启/关闭跟单"""
    trader = db.query(Trader).filter(Trader.username == trader_username).first()
    if not trader:
        raise HTTPException(404, f"Trader @{trader_username} not found")

    follow = (
        db.query(Follow)
        .filter(Follow.user_id == current_user.id, Follow.trader_id == trader.id)
        .first()
    )
    if not follow:
        raise HTTPException(404, "Not following this trader")

    follow.is_copy_trading = not follow.is_copy_trading
    db.commit()
    db.refresh(follow)
    return {"is_copy_trading": follow.is_copy_trading}