"""
Follow API — follow/unfollow traders + copy/counter trading toggles.
Copy and Counter are mutually exclusive — enforced at every write path.
"""
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session

from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.trader import Trader, TraderStats
from backend.models.follow import Follow

router = APIRouter(prefix="/api", tags=["follow"])


# ── Request / Response models ────────────────────────────────

class FollowRequest(BaseModel):
    trader_username: str
    is_copy_trading: bool = False
    is_counter_trading: bool = False  # ★ NEW

    @model_validator(mode="after")
    def check_mutual_exclusivity(self) -> "FollowRequest":
        if self.is_copy_trading and self.is_counter_trading:
            raise ValueError("is_copy_trading and is_counter_trading are mutually exclusive")
        return self


class FollowResponse(BaseModel):
    id: str
    trader_username: str
    is_copy_trading: bool
    is_counter_trading: bool  # ★ NEW
    created_at: datetime


class FollowListItem(BaseModel):
    id: str
    trader_username: str
    display_name: str | None = None
    avatar_url: str | None = None
    is_copy_trading: bool
    is_counter_trading: bool  # ★ NEW
    created_at: datetime
    win_rate: float = 0.0
    total_profit_usd: float = 0.0
    total_signals: int = 0
    avg_return_pct: float = 0.0
    profit_grade: str | None = None


class FollowStatusResponse(BaseModel):
    is_following: bool
    is_copy_trading: bool
    is_counter_trading: bool  # ★ NEW


# ── Helpers ─────────────────────────────────────────────────

def _get_trader_or_404(db: Session, username: str) -> Trader:
    t = db.query(Trader).filter(Trader.username == username).first()
    if not t:
        raise HTTPException(404, f"Trader @{username} not found")
    return t


def _get_follow(db: Session, user_id: str, trader_id: str) -> Follow | None:
    return (
        db.query(Follow)
        .filter(Follow.user_id == user_id, Follow.trader_id == trader_id)
        .first()
    )


def _to_response(follow: Follow, trader: Trader) -> FollowResponse:
    return FollowResponse(
        id=follow.id,
        trader_username=trader.username,
        is_copy_trading=follow.is_copy_trading,
        is_counter_trading=follow.is_counter_trading,
        created_at=follow.created_at,
    )


# ── Endpoints ────────────────────────────────────────────────

@router.post("/follow", response_model=FollowResponse)
def follow_trader(
    body: FollowRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Follow a trader (idempotent).
    Passing is_copy_trading=True or is_counter_trading=True also activates that mode.
    Copy and counter are mutually exclusive — setting one clears the other.
    """
    trader = _get_trader_or_404(db, body.trader_username)
    existing = _get_follow(db, current_user.id, trader.id)

    if existing:
        # Update trading mode if anything changed
        changed = (
            existing.is_copy_trading != body.is_copy_trading
            or existing.is_counter_trading != body.is_counter_trading
        )
        if changed:
            existing.is_copy_trading = body.is_copy_trading
            existing.is_counter_trading = body.is_counter_trading
            db.commit()
            db.refresh(existing)
        return _to_response(existing, trader)

    # ★ New follow — increment followers_count
    follow = Follow(
        user_id=current_user.id,
        trader_id=trader.id,
        is_copy_trading=body.is_copy_trading,
        is_counter_trading=body.is_counter_trading,
    )
    db.add(follow)
    trader.followers_count = (trader.followers_count or 0) + 1
    db.commit()
    db.refresh(follow)
    return _to_response(follow, trader)


@router.get("/follow/check/{trader_username}", response_model=FollowStatusResponse)
def check_follow_status(
    trader_username: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    trader = db.query(Trader).filter(Trader.username == trader_username).first()
    if not trader:
        return FollowStatusResponse(is_following=False, is_copy_trading=False, is_counter_trading=False)

    follow = _get_follow(db, current_user.id, trader.id)
    return FollowStatusResponse(
        is_following=follow is not None,
        is_copy_trading=follow.is_copy_trading if follow else False,
        is_counter_trading=follow.is_counter_trading if follow else False,
    )


@router.delete("/follow/{trader_username}")
def unfollow_trader(
    trader_username: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    trader = _get_trader_or_404(db, trader_username)
    follow = _get_follow(db, current_user.id, trader.id)
    if not follow:
        raise HTTPException(404, "Not following this trader")
    db.delete(follow)
    # ★ Decrement followers_count (floor at 0)
    trader.followers_count = max((trader.followers_count or 0) - 1, 0)
    db.commit()
    return {"message": f"Unfollowed @{trader_username}"}


@router.get("/follows", response_model=list[FollowListItem])
def get_my_follows(
    window: str = Query("30d", pattern="^(24h|7d|30d)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
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
            is_counter_trading=f.is_counter_trading,
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
    """Toggle copy trading on/off. Turning ON clears counter trading."""
    trader = _get_trader_or_404(db, trader_username)
    follow = _get_follow(db, current_user.id, trader.id)
    if not follow:
        raise HTTPException(404, "Not following this trader")

    follow.is_copy_trading = not follow.is_copy_trading
    if follow.is_copy_trading:
        follow.is_counter_trading = False  # mutual exclusivity
    db.commit()
    db.refresh(follow)
    return {"is_copy_trading": follow.is_copy_trading, "is_counter_trading": follow.is_counter_trading}


@router.patch("/follow/{trader_username}/counter-trading")
def toggle_counter_trading(
    trader_username: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """★ NEW — Toggle counter trading on/off. Turning ON clears copy trading."""
    trader = _get_trader_or_404(db, trader_username)
    follow = _get_follow(db, current_user.id, trader.id)
    if not follow:
        raise HTTPException(404, "Not following this trader")

    follow.is_counter_trading = not follow.is_counter_trading
    if follow.is_counter_trading:
        follow.is_copy_trading = False  # mutual exclusivity
    db.commit()
    db.refresh(follow)
    return {"is_counter_trading": follow.is_counter_trading, "is_copy_trading": follow.is_copy_trading}