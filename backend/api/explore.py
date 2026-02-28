"""
Explore API — Token Sentiment · Rising Traders · Search · Browse by Style
"""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, Query, Path
from pydantic import BaseModel
from sqlalchemy import func, desc, case, or_
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta

from backend.deps import get_db
from backend.models.signal import Signal
from backend.models.trader import Trader, TraderStats

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/explore", tags=["explore"])


# ── Response Models ──────────────────────────────────────

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
    points_change: float = 0.0


class SearchTraderItem(BaseModel):
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    profit_grade: str | None = None
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_signals: int = 0
    copiers_count: int = 0


class StyleTraderItem(BaseModel):
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    profit_grade: str | None = None
    win_rate: float = 0.0
    avg_return_pct: float = 0.0
    total_profit_usd: float = 0.0
    total_signals: int = 0
    copiers_count: int = 0
    streak: int = 0


# ── Token Sentiment ──────────────────────────────────────

@router.get("/sentiment", response_model=list[TokenSentimentItem])
def get_token_sentiment(
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(8, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Token sentiment aggregated from KOL signals."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = (
            db.query(
                Signal.ticker,
                func.count().label("total"),
                func.sum(case((Signal.sentiment == "bullish", 1), else_=0)).label("bull"),
                func.sum(case((Signal.sentiment == "bearish", 1), else_=0)).label("bear"),
                func.coalesce(func.avg(Signal.pct_change), 0).label("avg_pnl"),
                func.max(Signal.current_price).label("latest_price"),
            )
            .filter(Signal.created_at >= cutoff, Signal.ticker.isnot(None), Signal.ticker != "")
            .group_by(Signal.ticker)
            .order_by(desc("total"))
            .limit(limit)
            .all()
        )
        result = []
        for r in rows:
            total = r.total or 0
            bull = r.bull or 0
            bear = r.bear or 0
            denom = bull + bear
            result.append(TokenSentimentItem(
                ticker=r.ticker,
                total_signals=total,
                bull_count=bull,
                bear_count=bear,
                bull_pct=round(bull / max(denom, 1) * 100, 1),
                avg_pnl=round(float(r.avg_pnl or 0), 2),
                latest_price=round(float(r.latest_price), 6) if r.latest_price else None,
            ))
        return result
    except Exception as e:
        logger.exception("sentiment query failed: %s", e)
        return []


# ── Rising Traders ───────────────────────────────────────

@router.get("/rising", response_model=list[RisingTraderItem])
def get_rising_traders(
    limit: int = Query(6, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Traders with biggest improvement: 7d avg_return vs 30d avg_return."""
    try:
        stats_7d = {s.trader_id: s for s in db.query(TraderStats).filter(TraderStats.window == "7d").all()}
        stats_30d = {s.trader_id: s for s in db.query(TraderStats).filter(TraderStats.window == "30d").all()}
        scored = []
        for tid, s7 in stats_7d.items():
            s30 = stats_30d.get(tid)
            if not s30 or s30.total_signals < 2:
                continue
            change = (s7.avg_return_pct or 0) - (s30.avg_return_pct or 0)
            scored.append((tid, change, s7))
        scored.sort(key=lambda x: x[1], reverse=True)

        result = []
        for tid, change, s7 in scored[:limit]:
            trader = db.query(Trader).filter(Trader.id == tid).first()
            if not trader:
                continue
            result.append(RisingTraderItem(
                username=trader.username,
                display_name=trader.display_name,
                avatar_url=trader.avatar_url,
                profit_grade=s7.profit_grade,
                win_rate=s7.win_rate,
                avg_return_pct=s7.avg_return_pct or 0,
                total_signals=s7.total_signals,
                streak=s7.streak,
                points_change=round(change, 2),
            ))
        return result
    except Exception as e:
        logger.exception("rising traders query failed: %s", e)
        return []


# ── Search ───────────────────────────────────────────────

@router.get("/search", response_model=list[SearchTraderItem])
def search_traders(
    q: str = Query(..., min_length=1, max_length=50),
    limit: int = Query(10, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Search traders by username or display_name (case-insensitive)."""
    try:
        pattern = f"%{q.strip()}%"
        traders = (
            db.query(Trader)
            .filter(or_(
                Trader.username.ilike(pattern),
                Trader.display_name.ilike(pattern),
            ))
            .order_by(Trader.followers_count.desc())
            .limit(limit)
            .all()
        )
        if not traders:
            return []

        trader_ids = [t.id for t in traders]
        stats_rows = (
            db.query(TraderStats)
            .filter(TraderStats.trader_id.in_(trader_ids), TraderStats.window == "30d")
            .all()
        )
        stats_map = {s.trader_id: s for s in stats_rows}

        result = []
        for t in traders:
            s = stats_map.get(t.id)
            result.append(SearchTraderItem(
                username=t.username,
                display_name=t.display_name,
                avatar_url=t.avatar_url,
                profit_grade=s.profit_grade if s else None,
                win_rate=s.win_rate if s else 0.0,
                avg_return_pct=s.avg_return_pct if s else 0.0,
                total_signals=s.total_signals if s else 0,
                copiers_count=s.copiers_count if s else 0,
            ))
        return result
    except Exception as e:
        logger.exception("search failed: %s", e)
        return []


# ── Browse by Style ──────────────────────────────────────

VALID_STYLES = {"high_wr", "holders", "scalpers", "whales", "macro", "new"}


@router.get("/styles/{style}", response_model=list[StyleTraderItem])
def get_traders_by_style(
    style: str = Path(...),
    limit: int = Query(10, ge=1, le=30),
    window: str = Query("30d"),
    db: Session = Depends(get_db),
):
    """Filter traders by trading style category."""
    if style not in VALID_STYLES:
        return []
    try:
        base = (
            db.query(Trader, TraderStats)
            .join(TraderStats, TraderStats.trader_id == Trader.id)
            .filter(TraderStats.window == window, TraderStats.total_signals >= 1)
        )

        if style == "high_wr":
            # Win rate ≥ 75%, minimum 3 signals for significance
            base = (
                base
                .filter(TraderStats.win_rate >= 0.75, TraderStats.total_signals >= 3)
                .order_by(desc(TraderStats.win_rate), desc(TraderStats.total_signals))
            )

        elif style == "whales":
            # Highest absolute profit
            base = base.order_by(desc(TraderStats.total_profit_usd))

        elif style == "scalpers":
            # High frequency traders (≥8 signals in window)
            base = (
                base
                .filter(TraderStats.total_signals >= 8)
                .order_by(desc(TraderStats.total_signals))
            )

        elif style == "holders":
            # Low frequency but positive returns (quality over quantity)
            base = (
                base
                .filter(
                    TraderStats.total_signals.between(1, 5),
                    TraderStats.avg_return_pct > 0,
                )
                .order_by(desc(TraderStats.avg_return_pct))
            )

        elif style == "macro":
            # Traders who primarily trade BTC/ETH/SOL
            major = ["BTC", "ETH", "SOL"]
            macro_ids = [
                r[0]
                for r in db.query(Signal.trader_id)
                .filter(Signal.ticker.in_(major))
                .group_by(Signal.trader_id)
                .having(func.count() >= 2)
                .all()
            ]
            if not macro_ids:
                return []
            base = base.filter(Trader.id.in_(macro_ids)).order_by(desc(TraderStats.total_profit_usd))

        elif style == "new":
            # Joined within last 30 days
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            base = base.filter(Trader.created_at >= cutoff).order_by(desc(Trader.created_at))

        rows = base.limit(limit).all()
        return [
            StyleTraderItem(
                username=t.username,
                display_name=t.display_name,
                avatar_url=t.avatar_url,
                profit_grade=s.profit_grade,
                win_rate=s.win_rate,
                avg_return_pct=s.avg_return_pct or 0,
                total_profit_usd=s.total_profit_usd or 0,
                total_signals=s.total_signals,
                copiers_count=s.copiers_count,
                streak=s.streak,
            )
            for t, s in rows
        ]
    except Exception as e:
        logger.exception("styles/%s query failed: %s", style, e)
        return []