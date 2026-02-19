"""
Portfolio API — Dashboard 数据（余额、持仓、盈亏曲线）
匹配前端 ProfileDataResponse / BalanceHistoryResponse
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone, date
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.trade import Trade
from backend.models.follow import Follow
from backend.models.setting import BalanceSnapshot

router = APIRouter(prefix="/api", tags=["portfolio"])


# ── Response 模型 ────────────────────────────────────────

class FollowerItem(BaseModel):
    name: str
    twitterId: str


class ProfileDataResponse(BaseModel):
    name: str
    twitterId: str
    followingCount: int = 0
    followerCount: int = 0
    accountValue: float = 0.0
    followerList: list[FollowerItem] = []
    traderCopyingCount: int = 0
    signalCount: int = 0
    noiseCount: int = 0
    streakCount: int = 0
    streakCumulativePnLRate: float = 0.0
    tradeTicks: int = 0
    collectedPoints: float = 0.0


class BalanceHistoryItem(BaseModel):
    acconutValue: float   # 注意：前端拼写是 acconutValue
    timestamp: int


class PositionItem(BaseModel):
    id: str
    ticker: str
    direction: str
    entry_price: float
    current_price: float | None = None
    size_usd: float
    size_qty: float
    leverage: float
    pnl_usd: float | None = None
    pnl_pct: float | None = None
    trader_username: str | None = None
    opened_at: datetime


class DashboardSummary(BaseModel):
    total_balance: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    open_positions: int = 0
    total_trades: int = 0
    win_rate: float = 0.0


# ── API 端点 ─────────────────────────────────────────────

@router.get("/portfolio/profile", response_model=ProfileDataResponse)
def get_profile_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dashboard 用户概览"""
    following_count = db.query(Follow).filter(Follow.user_id == current_user.id).count()
    copy_trading_count = (
        db.query(Follow)
        .filter(Follow.user_id == current_user.id, Follow.is_copy_trading.is_(True))
        .count()
    )
    total_trades = db.query(Trade).filter(Trade.user_id == current_user.id).count()

    # 最新余额
    latest_snapshot = (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.user_id == current_user.id)
        .order_by(desc(BalanceSnapshot.snapshot_date))
        .first()
    )
    account_value = latest_snapshot.balance if latest_snapshot else 0.0

    # 计算连胜
    recent_trades = (
        db.query(Trade)
        .filter(Trade.user_id == current_user.id, Trade.status == "closed")
        .order_by(desc(Trade.closed_at))
        .limit(50)
        .all()
    )
    streak = 0
    streak_pnl = 0.0
    for t in recent_trades:
        if t.pnl_usd and t.pnl_usd > 0:
            streak += 1
            streak_pnl += t.pnl_usd
        else:
            break

    return ProfileDataResponse(
        name=current_user.display_name or current_user.wallet_address[:10],
        twitterId=current_user.wallet_address,
        followingCount=following_count,
        followerCount=0,
        accountValue=account_value,
        followerList=[],
        traderCopyingCount=copy_trading_count,
        signalCount=total_trades,
        noiseCount=0,
        streakCount=streak,
        streakCumulativePnLRate=streak_pnl,
        tradeTicks=total_trades,
        collectedPoints=0.0,
    )


@router.get("/portfolio/balance-history", response_model=list[BalanceHistoryItem])
def get_balance_history(
    timeRange: str = Query("W", regex="^(D|W|M|YTD|ALL)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """盈亏曲线（匹配前端 BalanceHistoryResponse）"""
    now = datetime.now(timezone.utc)
    if timeRange == "D":
        since = now - timedelta(days=1)
    elif timeRange == "W":
        since = now - timedelta(weeks=1)
    elif timeRange == "M":
        since = now - timedelta(days=30)
    elif timeRange == "YTD":
        since = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    else:
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)

    snapshots = (
        db.query(BalanceSnapshot)
        .filter(
            BalanceSnapshot.user_id == current_user.id,
            BalanceSnapshot.snapshot_date >= since.date(),
        )
        .order_by(BalanceSnapshot.snapshot_date)
        .all()
    )

    result: list[BalanceHistoryItem] = []

    # ── 补零余额点：数据点不足 7 个时，在第一个快照前补 $0 ──
    if snapshots and len(snapshots) < 7:
        first_date = snapshots[0].snapshot_date
        days_available = (first_date - since.date()).days
        points_needed = 7 - len(snapshots)
        pad_count = min(points_needed, days_available)

        for i in range(pad_count, 0, -1):
            pad_date = first_date - timedelta(days=i)
            result.append(
                BalanceHistoryItem(
                    acconutValue=0.0,
                    timestamp=int(
                        datetime.combine(pad_date, datetime.min.time())
                        .replace(tzinfo=timezone.utc)
                        .timestamp()
                    ),
                )
            )

    # ── 真实数据点 ──
    result.extend(
        BalanceHistoryItem(
            acconutValue=s.balance,
            timestamp=int(
                datetime.combine(s.snapshot_date, datetime.min.time())
                .replace(tzinfo=timezone.utc)
                .timestamp()
            ),
        )
        for s in snapshots
    )

    return result


@router.get("/portfolio/positions", response_model=list[PositionItem])
def get_open_positions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """当前持仓"""
    positions = (
        db.query(Trade)
        .filter(Trade.user_id == current_user.id, Trade.status == "open")
        .order_by(desc(Trade.opened_at))
        .all()
    )

    return [
        PositionItem(
            id=t.id,
            ticker=t.ticker,
            direction=t.direction,
            entry_price=t.entry_price,
            current_price=t.exit_price,
            size_usd=t.size_usd,
            size_qty=t.size_qty,
            leverage=t.leverage,
            pnl_usd=t.pnl_usd,
            pnl_pct=t.pnl_pct,
            trader_username=t.trader_username,
            opened_at=t.opened_at,
        )
        for t in positions
    ]


@router.get("/portfolio/summary", response_model=DashboardSummary)
def get_dashboard_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dashboard 总结数据"""
    latest_snapshot = (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.user_id == current_user.id)
        .order_by(desc(BalanceSnapshot.snapshot_date))
        .first()
    )

    total_trades = db.query(Trade).filter(Trade.user_id == current_user.id).count()
    open_positions = db.query(Trade).filter(Trade.user_id == current_user.id, Trade.status == "open").count()

    closed_trades = (
        db.query(Trade)
        .filter(Trade.user_id == current_user.id, Trade.status == "closed")
        .all()
    )
    wins = sum(1 for t in closed_trades if t.pnl_usd and t.pnl_usd > 0)
    total_pnl = sum(t.pnl_usd or 0 for t in closed_trades)
    win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0.0
    balance = latest_snapshot.balance if latest_snapshot else 0.0
    pnl_pct = (total_pnl / balance * 100) if balance > 0 else 0.0

    return DashboardSummary(
        total_balance=balance,
        total_pnl=total_pnl,
        total_pnl_pct=pnl_pct,
        open_positions=open_positions,
        total_trades=total_trades,
        win_rate=win_rate,
    )