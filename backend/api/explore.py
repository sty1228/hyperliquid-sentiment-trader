"""
Explore API — Token Sentiment · Token Detail · Rising Traders · Search · Styles
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


class TokenSignalRow(BaseModel):
    signal_id: str
    trader_username: str
    trader_display_name: str | None = None
    trader_avatar_url: str | None = None
    direction: str
    sentiment: str
    entry_price: float | None = None
    current_price: float | None = None
    pct_change: float | None = None
    max_gain_pct: float | None = None
    max_gain_at: str | None = None
    tweet_text: str | None = None
    tweet_image_url: str | None = None
    likes: int | None = None
    retweets: int | None = None
    replies: int | None = None
    created_at: str


class TokenTopTrader(BaseModel):
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    profit_grade: str | None = None
    signal_count: int = 0
    avg_pnl: float = 0.0
    win_count: int = 0
    win_rate: float = 0.0


class TokenDetailResponse(BaseModel):
    ticker: str
    total_signals: int
    bull_count: int
    bear_count: int
    bull_pct: float
    avg_pnl: float
    latest_price: float | None = None
    recent_signals: list[TokenSignalRow] = []
    top_traders: list[TokenTopTrader] = []


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


# ── Token Detail ─────────────────────────────────────────

@router.get("/token/{ticker}", response_model=TokenDetailResponse)
def get_token_detail(
    ticker: str = Path(..., min_length=1, max_length=20),
    days: int = Query(30, ge=1, le=90),
    signal_limit: int = Query(20, ge=1, le=50),
    trader_limit: int = Query(10, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Detailed analysis for a single token: sentiment, recent signals, top traders."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        tk = ticker.upper().strip()

        # ── aggregate sentiment ──
        agg = (
            db.query(
                func.count().label("total"),
                func.sum(case((Signal.sentiment == "bullish", 1), else_=0)).label("bull"),
                func.sum(case((Signal.sentiment == "bearish", 1), else_=0)).label("bear"),
                func.coalesce(func.avg(Signal.pct_change), 0).label("avg_pnl"),
                func.max(Signal.current_price).label("latest_price"),
            )
            .filter(Signal.created_at >= cutoff, Signal.ticker == tk)
            .first()
        )
        total = (agg.total or 0) if agg else 0
        bull = (agg.bull or 0) if agg else 0
        bear = (agg.bear or 0) if agg else 0
        denom = bull + bear

        # ── recent signals ──
        sig_rows = (
            db.query(Signal, Trader)
            .join(Trader, Trader.id == Signal.trader_id)
            .filter(Signal.ticker == tk, Signal.created_at >= cutoff)
            .order_by(desc(Signal.created_at))
            .limit(signal_limit)
            .all()
        )
        recent_signals = []
        for sig, trader in sig_rows:
            recent_signals.append(TokenSignalRow(
                signal_id=sig.id,
                trader_username=trader.username,
                trader_display_name=trader.display_name,
                trader_avatar_url=trader.avatar_url,
                direction=sig.direction,
                sentiment=sig.sentiment,
                entry_price=sig.entry_price,
                current_price=sig.current_price,
                pct_change=round(float(sig.pct_change), 2) if sig.pct_change is not None else None,
                max_gain_pct=round(float(sig.max_gain_pct), 2) if sig.max_gain_pct is not None else None,
                max_gain_at=sig.max_gain_at.isoformat() if sig.max_gain_at else None,
                tweet_text=sig.tweet_text or None,
                tweet_image_url=sig.tweet_image_url or None,
                likes=sig.likes,
                retweets=sig.retweets,
                replies=sig.replies,
                created_at=sig.created_at.isoformat() if sig.created_at else "",
            ))

        # ── top traders for this token ──
        trader_agg = (
            db.query(
                Signal.trader_id,
                func.count().label("cnt"),
                func.coalesce(func.avg(Signal.pct_change), 0).label("avg"),
                func.sum(case((Signal.pct_change > 0, 1), else_=0)).label("wins"),
            )
            .filter(Signal.ticker == tk, Signal.created_at >= cutoff)
            .group_by(Signal.trader_id)
            .order_by(desc("cnt"))
            .limit(trader_limit)
            .all()
        )
        top_traders = []
        if trader_agg:
            tids = [r.trader_id for r in trader_agg]
            traders_map = {t.id: t for t in db.query(Trader).filter(Trader.id.in_(tids)).all()}
            stats_map = {
                s.trader_id: s
                for s in db.query(TraderStats)
                .filter(TraderStats.trader_id.in_(tids), TraderStats.window == "30d")
                .all()
            }
            for r in trader_agg:
                t = traders_map.get(r.trader_id)
                if not t:
                    continue
                s = stats_map.get(r.trader_id)
                cnt = r.cnt or 0
                wins = r.wins or 0
                top_traders.append(TokenTopTrader(
                    username=t.username,
                    display_name=t.display_name,
                    avatar_url=t.avatar_url,
                    profit_grade=s.profit_grade if s else None,
                    signal_count=cnt,
                    avg_pnl=round(float(r.avg or 0), 2),
                    win_count=wins,
                    win_rate=round(wins / max(cnt, 1), 2),
                ))

        return TokenDetailResponse(
            ticker=tk,
            total_signals=total,
            bull_count=bull,
            bear_count=bear,
            bull_pct=round(bull / max(denom, 1) * 100, 1),
            avg_pnl=round(float(agg.avg_pnl or 0), 2) if agg else 0,
            latest_price=round(float(agg.latest_price), 6) if agg and agg.latest_price else None,
            recent_signals=recent_signals,
            top_traders=top_traders,
        )
    except Exception as e:
        logger.exception("token detail %s failed: %s", ticker, e)
        return TokenDetailResponse(
            ticker=ticker.upper(),
            total_signals=0, bull_count=0, bear_count=0, bull_pct=0, avg_pnl=0,
        )


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
            .filter(or_(Trader.username.ilike(pattern), Trader.display_name.ilike(pattern)))
            .order_by(Trader.followers_count.desc())
            .limit(limit)
            .all()
        )
        if not traders:
            return []
        trader_ids = [t.id for t in traders]
        stats_map = {
            s.trader_id: s
            for s in db.query(TraderStats)
            .filter(TraderStats.trader_id.in_(trader_ids), TraderStats.window == "30d")
            .all()
        }
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
            base = base.filter(TraderStats.win_rate >= 0.75, TraderStats.total_signals >= 3).order_by(desc(TraderStats.win_rate), desc(TraderStats.total_signals))
        elif style == "whales":
            base = base.order_by(desc(TraderStats.total_profit_usd))
        elif style == "scalpers":
            base = base.filter(TraderStats.total_signals >= 8).order_by(desc(TraderStats.total_signals))
        elif style == "holders":
            base = base.filter(TraderStats.total_signals.between(1, 5), TraderStats.avg_return_pct > 0).order_by(desc(TraderStats.avg_return_pct))
        elif style == "macro":
            macro_ids = [
                r[0] for r in db.query(Signal.trader_id)
                .filter(Signal.ticker.in_(["BTC", "ETH", "SOL"]))
                .group_by(Signal.trader_id).having(func.count() >= 2).all()
            ]
            if not macro_ids:
                return []
            base = base.filter(Trader.id.in_(macro_ids)).order_by(desc(TraderStats.total_profit_usd))
        elif style == "new":
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            base = base.filter(Trader.created_at >= cutoff).order_by(desc(Trader.created_at))

        rows = base.limit(limit).all()
        return [
            StyleTraderItem(
                username=t.username, display_name=t.display_name, avatar_url=t.avatar_url,
                profit_grade=s.profit_grade, win_rate=s.win_rate, avg_return_pct=s.avg_return_pct or 0,
                total_profit_usd=s.total_profit_usd or 0, total_signals=s.total_signals,
                copiers_count=s.copiers_count, streak=s.streak,
            )
            for t, s in rows
        ]
    except Exception as e:
        logger.exception("styles/%s query failed: %s", style, e)
        return []