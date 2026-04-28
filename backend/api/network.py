"""
Network graph API — feeds the trader-network visualization.

Two endpoints:
  GET /api/network/graph                       — initial draw: one row per (KOL, source) edge.
  GET /api/network/trader/{username}/detail    — click-through: aggregates + open trade list.

Design:
  - LEFT OUTER JOIN Follow → grouped Trades so KOLs the user follows but hasn't
    yet copied/countered still appear (with zeros).
  - Two edges when the user both copies AND counters the same KOL.
  - Backend computes win_rate (single source of truth, divide-by-zero handled here).
"""
from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, case, func, desc
from sqlalchemy.orm import Session

from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.trade import Trade
from backend.models.trader import Trader
from backend.models.follow import Follow

router = APIRouter(prefix="/api/network", tags=["network"])


# ── Response models ───────────────────────────────────────────


class NetworkEdge(BaseModel):
    trader_username: str
    avatar_url: str | None = None
    display_name: str | None = None
    source: str  # "copy" | "counter"
    is_copy_trading: bool = False
    is_counter_trading: bool = False
    copy_mode: str = "all"
    remaining_copies: int | None = None
    open_count: int = 0
    total_exposure_usd: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    pnl_usd: float = 0.0
    trade_count: int = 0


class OpenTradeRow(BaseModel):
    id: str
    ticker: str
    direction: str
    size_usd: float
    size_qty: float
    leverage: float
    entry_price: float
    current_pnl_usd: float | None = None
    current_pnl_pct: float | None = None
    opened_at: datetime


class TraderDetailResponse(BaseModel):
    aggregates: list[NetworkEdge]
    open_trades: list[OpenTradeRow]


# ── Helpers ───────────────────────────────────────────────────


def _aggregate_edges(db: Session, user_id: str, trader_username_filter: str | None = None) -> list[NetworkEdge]:
    """
    Group trades by (trader_username, source) and merge with Follow rows.
    Returns one NetworkEdge per (KOL, source) edge.
    """
    # Trade aggregates per (trader_username, source).
    trade_q = (
        db.query(
            Trade.trader_username.label("trader_username"),
            Trade.source.label("source"),
            func.count(Trade.id).label("trade_count"),
            func.sum(case((Trade.status == "open", 1), else_=0)).label("open_count"),
            func.coalesce(
                func.sum(case((Trade.status == "open", Trade.size_usd), else_=0.0)), 0.0
            ).label("total_exposure_usd"),
            func.sum(
                case((and_(Trade.status == "closed", Trade.pnl_usd > 0), 1), else_=0)
            ).label("win_count"),
            func.sum(
                case((and_(Trade.status == "closed", Trade.pnl_usd <= 0), 1), else_=0)
            ).label("loss_count"),
            func.coalesce(func.sum(Trade.pnl_usd), 0.0).label("pnl_usd"),
        )
        .filter(Trade.user_id == user_id, Trade.trader_username.isnot(None))
        .group_by(Trade.trader_username, Trade.source)
    )
    if trader_username_filter:
        trade_q = trade_q.filter(Trade.trader_username == trader_username_filter)
    trade_rows = {(r.trader_username, r.source or "copy"): r for r in trade_q.all()}

    # Active follows joined to traders for avatar + flags.
    follow_q = (
        db.query(Follow, Trader)
        .join(Trader, Trader.id == Follow.trader_id)
        .filter(Follow.user_id == user_id)
    )
    if trader_username_filter:
        follow_q = follow_q.filter(Trader.username == trader_username_filter)
    follow_rows = follow_q.all()
    follow_by_username = {t.username: (f, t) for f, t in follow_rows}

    # All distinct (trader_username, source) edges to emit:
    # union of trade-aggregate keys and active follow flags.
    edges: dict[tuple[str, str], NetworkEdge] = {}

    for (uname, src), r in trade_rows.items():
        f, t = follow_by_username.get(uname, (None, None))
        win = int(r.win_count or 0)
        loss = int(r.loss_count or 0)
        wr = round(win / (win + loss), 3) if (win + loss) > 0 else 0.0
        edges[(uname, src)] = NetworkEdge(
            trader_username=uname,
            avatar_url=t.avatar_url if t else None,
            display_name=t.display_name if t else None,
            source=src,
            is_copy_trading=bool(f.is_copy_trading) if f else False,
            is_counter_trading=bool(f.is_counter_trading) if f else False,
            copy_mode=(f.copy_mode or "all") if f else "all",
            remaining_copies=f.remaining_copies if f else None,
            open_count=int(r.open_count or 0),
            total_exposure_usd=round(float(r.total_exposure_usd or 0.0), 2),
            win_count=win,
            loss_count=loss,
            win_rate=wr,
            pnl_usd=round(float(r.pnl_usd or 0.0), 2),
            trade_count=int(r.trade_count or 0),
        )

    # Add follow-only edges (user follows + has flag set, but no trades yet on that side).
    for uname, (f, t) in follow_by_username.items():
        if f.is_copy_trading and (uname, "copy") not in edges:
            edges[(uname, "copy")] = NetworkEdge(
                trader_username=uname,
                avatar_url=t.avatar_url,
                display_name=t.display_name,
                source="copy",
                is_copy_trading=True,
                is_counter_trading=bool(f.is_counter_trading),
                copy_mode=f.copy_mode or "all",
                remaining_copies=f.remaining_copies,
            )
        if f.is_counter_trading and (uname, "counter") not in edges:
            edges[(uname, "counter")] = NetworkEdge(
                trader_username=uname,
                avatar_url=t.avatar_url,
                display_name=t.display_name,
                source="counter",
                is_copy_trading=bool(f.is_copy_trading),
                is_counter_trading=True,
                copy_mode=f.copy_mode or "all",
                remaining_copies=f.remaining_copies,
            )

    return sorted(
        edges.values(),
        key=lambda e: (-e.total_exposure_usd, -e.open_count, e.trader_username),
    )


# ── Endpoints ─────────────────────────────────────────────────


@router.get("/graph", response_model=list[NetworkEdge])
def get_network_graph(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One row per (KOL, source) edge for the network graph."""
    return _aggregate_edges(db, current_user.id)


@router.get("/trader/{trader_username}/detail", response_model=TraderDetailResponse)
def get_trader_detail(
    trader_username: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Click-through: edge aggregates plus the open-trade list for one KOL.
    Returns 404 only if the KOL doesn't exist; an empty trade list is normal
    when the user follows but hasn't traded.
    """
    trader = db.query(Trader).filter(Trader.username == trader_username).first()
    if not trader:
        raise HTTPException(404, f"Trader @{trader_username} not found")

    aggregates = _aggregate_edges(db, current_user.id, trader_username_filter=trader_username)

    open_trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == current_user.id,
            Trade.trader_username == trader_username,
            Trade.status == "open",
        )
        .order_by(desc(Trade.opened_at))
        .all()
    )

    return TraderDetailResponse(
        aggregates=aggregates,
        open_trades=[
            OpenTradeRow(
                id=t.id,
                ticker=t.ticker,
                direction=t.direction,
                size_usd=t.size_usd,
                size_qty=t.size_qty,
                leverage=t.leverage,
                entry_price=t.entry_price,
                current_pnl_usd=t.pnl_usd,
                current_pnl_pct=t.pnl_pct,
                opened_at=t.opened_at,
            )
            for t in open_trades
        ],
    )
