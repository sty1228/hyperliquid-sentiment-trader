"""
Explore API — Token Sentiment + Rising Traders
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, desc, case
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta

from backend.deps import get_db
from backend.models.signal import Signal
from backend.models.trader import Trader, TraderStats

router = APIRouter(prefix="/api/explore", tags=["explore"])


class TokenSentimentItem(BaseModel):
    ticker: str
    total_signals: int
    bull_count: int
    bear_count: int
    bull_pct: float
    avg_pnl: float
    latest_price: float | None = None


class RisingTraderItem(BaseModel):
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    profit_grade: str | None = None
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_signals: int = 0
    streak: int = 0
    points_change: float = 0.0  # 7d points vs 30d avg


@router.get("/sentiment", response_model=list[TokenSentimentItem])
def get_token_sentiment(
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(8, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Token sentiment aggregated from KOL signals."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        db.query(
            Signal.ticker,
            func.count().label("total"),
            func.sum(case((Signal.sentiment == "bullish", 1), else_=0)).label("bull"),
            func.sum(case((Signal.sentiment == "bearish", 1), else_=0)).label("bear"),
            func.round(func.avg(Signal.pct_change), 2).label("avg_pnl"),
            func.max(Signal.current_price).label("latest_price"),
        )
        .filter(Signal.created_at >= cutoff)
        .group_by(Signal.ticker)
        .order_by(desc("total"))
        .limit(limit)
        .all()
    )

    result = []
    for r in rows:
        total = r.total or 0
        bull = r.bull or 0
        result.append(
            TokenSentimentItem(
                ticker=r.ticker,
                total_signals=total,
                bull_count=bull,
                bear_count=(r.bear or 0),
                bull_pct=round((bull / max(bull + (r.bear or 0), 1) * 100), 1),
                avg_pnl=float(r.avg_pnl or 0),
                latest_price=float(r.latest_price) if r.latest_price else None,
            )
        )
    return result


@router.get("/rising", response_model=list[RisingTraderItem])
def get_rising_traders(
    limit: int = Query(6, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Traders with biggest improvement: 7d points vs 30d points."""
    stats_7d = {
        s.trader_id: s
        for s in db.query(TraderStats).filter(TraderStats.window == "7d").all()
    }
    stats_30d = {
        s.trader_id: s
        for s in db.query(TraderStats).filter(TraderStats.window == "30d").all()
    }

    scored = []
    for tid, s7 in stats_7d.items():
        s30 = stats_30d.get(tid)
        if not s30 or s30.total_signals < 2:
            continue
        # Rising = 7d avg_return - 30d avg_return (positive = improving)
        change = (s7.avg_return_pct or 0) - (s30.avg_return_pct or 0)
        scored.append((tid, change, s7))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[:limit]

    result = []
    for tid, change, s7 in top:
        trader = db.query(Trader).filter(Trader.id == tid).first()
        if not trader:
            continue
        result.append(
            RisingTraderItem(
                username=trader.username,
                display_name=trader.display_name,
                avatar_url=trader.avatar_url,
                profit_grade=s7.profit_grade,
                win_rate=s7.win_rate,
                avg_return_pct=s7.avg_return_pct or 0,
                total_signals=s7.total_signals,
                streak=s7.streak,
                points_change=round(change, 2),
            )
        )
    return result