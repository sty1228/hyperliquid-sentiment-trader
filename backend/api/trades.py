"""
Trades API — 交易历史
"""
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc

from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.trade import Trade

router = APIRouter(prefix="/api", tags=["trades"])


# ── Response 模型 ────────────────────────────────────────

class TradeResponse(BaseModel):
    id: str
    ticker: str
    direction: str
    entry_price: float
    exit_price: float | None = None
    size_usd: float
    size_qty: float
    leverage: float
    pnl_usd: float | None = None
    pnl_pct: float | None = None
    status: str
    source: str
    trader_username: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None


class TradesSummary(BaseModel):
    total: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0


class TradesPageResponse(BaseModel):
    trades: list[TradeResponse]
    summary: TradesSummary
    total_count: int


# ── API 端点 ─────────────────────────────────────────────

@router.get("/trades", response_model=TradesPageResponse)
def get_trades(
    status: str = Query("all", regex="^(all|open|closed)$"),
    direction: str = Query("all", regex="^(all|long|short)$"),
    source: str = Query("all", regex="^(all|copy|counter|manual)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    交易历史
    - status: all / open / closed
    - direction: all / long / short
    - source: all / copy / counter / manual
    """
    query = db.query(Trade).filter(Trade.user_id == current_user.id)

    if status != "all":
        query = query.filter(Trade.status == status)
    if direction != "all":
        query = query.filter(Trade.direction == direction)
    if source != "all":
        query = query.filter(Trade.source == source)

    total_count = query.count()

    trades = (
        query
        .order_by(desc(Trade.opened_at))
        .offset(offset)
        .limit(limit)
        .all()
    )

    # 计算 summary（基于所有已关闭的交易）
    all_closed = (
        db.query(Trade)
        .filter(Trade.user_id == current_user.id, Trade.status == "closed")
        .all()
    )
    wins = sum(1 for t in all_closed if t.pnl_usd and t.pnl_usd > 0)
    losses = sum(1 for t in all_closed if t.pnl_usd and t.pnl_usd <= 0)
    total_pnl = sum(t.pnl_usd or 0 for t in all_closed)
    pnl_list = [t.pnl_usd for t in all_closed if t.pnl_usd is not None]

    summary = TradesSummary(
        total=len(all_closed),
        wins=wins,
        losses=losses,
        win_rate=(wins / len(all_closed) * 100) if all_closed else 0.0,
        total_pnl=total_pnl,
        best_trade=max(pnl_list) if pnl_list else 0.0,
        worst_trade=min(pnl_list) if pnl_list else 0.0,
    )

    return TradesPageResponse(
        trades=[
            TradeResponse(
                id=t.id,
                ticker=t.ticker,
                direction=t.direction,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                size_usd=t.size_usd,
                size_qty=t.size_qty,
                leverage=t.leverage,
                pnl_usd=t.pnl_usd,
                pnl_pct=t.pnl_pct,
                status=t.status,
                source=t.source,
                trader_username=t.trader_username,
                opened_at=t.opened_at,
                closed_at=t.closed_at,
            )
            for t in trades
        ],
        summary=summary,
        total_count=total_count,
    )