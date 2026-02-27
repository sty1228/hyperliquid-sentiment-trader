"""
Portfolio API — Dashboard 数据（余额、持仓、盈亏曲线）
匹配前端 ProfileDataResponse / BalanceHistoryResponse

★ P&L 公式改为纯交易盈亏:
  P&L = sum(closed trades PnL) + sum(open trades unrealized PnL)
  与出入金完全无关。没交易 = P&L 为 0。
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
from backend.models.setting import BalanceSnapshot, BalanceEvent

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


# ── ★ P&L History 模型 ──────────────────────────────────

class PnlHistoryItem(BaseModel):
    timestamp: int
    pnl: float


class PnlHistoryResponse(BaseModel):
    data: list[PnlHistoryItem] = []
    range_pnl: float = 0.0
    range_pnl_pct: float = 0.0
    total_pnl: float = 0.0


# ── 交易盈亏计算 helper ──────────────────────────────────

def _calc_trade_pnl(db: Session, user_id: str, since: datetime | None = None) -> float:
    """
    纯交易P&L = 已平仓 realized + 持仓 unrealized
    since: 只计算这个时间之后的交易（用于区间P&L）
    """
    # Realized PnL from closed trades
    closed_q = db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
    )
    if since:
        closed_q = closed_q.filter(Trade.closed_at >= since)
    realized = float(closed_q.scalar())

    # Unrealized PnL from open positions
    unrealized = float(
        db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).filter(
            Trade.user_id == user_id,
            Trade.status == "open",
        ).scalar()
    )

    return round(realized + unrealized, 2)


def _get_realtime_balance(db: Session, user_id: str) -> float:
    """
    取「最新余额」——优先用最新 BalanceEvent.balance_after，
    如果没有事件则用最新 BalanceSnapshot。
    """
    latest_evt = (
        db.query(BalanceEvent)
        .filter(BalanceEvent.user_id == user_id)
        .order_by(desc(BalanceEvent.created_at))
        .first()
    )
    latest_snap = (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.user_id == user_id)
        .order_by(desc(BalanceSnapshot.snapshot_date))
        .first()
    )

    if latest_evt and latest_snap:
        snap_ts = datetime.combine(
            latest_snap.snapshot_date, datetime.min.time()
        ).replace(tzinfo=timezone.utc).timestamp()
        evt_ts = latest_evt.created_at.replace(tzinfo=timezone.utc).timestamp() if not latest_evt.created_at.tzinfo else latest_evt.created_at.timestamp()
        if evt_ts >= snap_ts:
            return float(latest_evt.balance_after)
        return float(latest_snap.balance)
    elif latest_evt:
        return float(latest_evt.balance_after)
    elif latest_snap:
        return float(latest_snap.balance)
    return 0.0


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

    latest_snapshot = (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.user_id == current_user.id)
        .order_by(desc(BalanceSnapshot.snapshot_date))
        .first()
    )
    account_value = latest_snapshot.balance if latest_snapshot else 0.0

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
    """余额曲线（匹配前端 BalanceHistoryResponse）"""
    now = datetime.now(timezone.utc)

    # ── "D" 视图：过去 24 小时，每小时一个点 ──
    if timeRange == "D":
        since_24h = now - timedelta(hours=24)

        latest_before = (
            db.query(BalanceEvent)
            .filter(
                BalanceEvent.user_id == current_user.id,
                BalanceEvent.created_at <= since_24h,
            )
            .order_by(desc(BalanceEvent.created_at))
            .first()
        )
        if latest_before:
            opening_balance = latest_before.balance_after
        else:
            snap_before = (
                db.query(BalanceSnapshot)
                .filter(
                    BalanceSnapshot.user_id == current_user.id,
                    BalanceSnapshot.snapshot_date < since_24h.date(),
                )
                .order_by(desc(BalanceSnapshot.snapshot_date))
                .first()
            )
            opening_balance = snap_before.balance if snap_before else 0.0

        events = (
            db.query(BalanceEvent)
            .filter(
                BalanceEvent.user_id == current_user.id,
                BalanceEvent.created_at > since_24h,
            )
            .order_by(BalanceEvent.created_at)
            .all()
        )

        start_hour = since_24h.replace(minute=0, second=0, microsecond=0)
        evt_idx = 0
        bal = opening_balance
        result: list[BalanceHistoryItem] = []

        for h in range(0, 25, 2):
            hour_time = start_hour + timedelta(hours=h)
            hour_ts = int(hour_time.timestamp())

            while evt_idx < len(events) and int(events[evt_idx].created_at.timestamp()) <= hour_ts:
                bal = events[evt_idx].balance_after
                evt_idx += 1

            result.append(BalanceHistoryItem(
                acconutValue=bal,
                timestamp=hour_ts,
            ))

        while evt_idx < len(events):
            bal = events[evt_idx].balance_after
            evt_idx += 1

        latest_snap = (
            db.query(BalanceSnapshot)
            .filter(BalanceSnapshot.user_id == current_user.id)
            .order_by(desc(BalanceSnapshot.snapshot_date))
            .first()
        )
        final_bal = latest_snap.balance if latest_snap else bal
        if result:
            result[-1] = BalanceHistoryItem(acconutValue=final_bal, timestamp=result[-1].timestamp)

        return result

    # ── 非 D 视图 ──
    if timeRange == "W":
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
    """Dashboard 总结数据 — ★ P&L 纯交易盈亏"""
    latest_snapshot = (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.user_id == current_user.id)
        .order_by(desc(BalanceSnapshot.snapshot_date))
        .first()
    )

    total_trades = db.query(Trade).filter(Trade.user_id == current_user.id).count()
    open_positions = db.query(Trade).filter(Trade.user_id == current_user.id, Trade.status == "open").count()

    # ★ P&L = realized + unrealized from trades only
    total_pnl = _calc_trade_pnl(db, current_user.id)

    closed_trades = (
        db.query(Trade)
        .filter(Trade.user_id == current_user.id, Trade.status == "closed")
        .all()
    )
    wins = sum(1 for t in closed_trades if t.pnl_usd and t.pnl_usd > 0)
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


# ══════════════════════════════════════════════════════════
# ★ P&L History — 纯交易盈亏
#   P&L = sum(closed trade PnL) + sum(open trade unrealized PnL)
#   与出入金完全无关。没交易 = 0。
# ══════════════════════════════════════════════════════════

@router.get("/portfolio/pnl-history", response_model=PnlHistoryResponse)
def get_pnl_history(
    timeRange: str = Query("M", regex="^(D|W|M|YTD|ALL)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    ★ 纯交易 P&L 曲线。
    每个数据点的 pnl = 截至该时间点的累计交易盈亏。
    没有任何交易 → 所有点 pnl = 0。
    """
    now = datetime.now(timezone.utc)
    user_id = current_user.id

    # Total P&L across all time (realized + unrealized)
    all_time_pnl = _calc_trade_pnl(db, user_id)

    # Determine time range
    if timeRange == "D":
        since = now - timedelta(hours=24)
    elif timeRange == "W":
        since = now - timedelta(weeks=1)
    elif timeRange == "M":
        since = now - timedelta(days=30)
    elif timeRange == "YTD":
        since = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    else:  # ALL
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)

    # ── Get all closed trades in range ──
    closed_trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.closed_at >= since,
        )
        .order_by(Trade.closed_at)
        .all()
    )

    # ── PnL before range start (baseline) ──
    pnl_before = float(
        db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.closed_at < since,
        ).scalar()
    )

    # ── Build P&L curve ──
    pnl_points: list[PnlHistoryItem] = []

    if timeRange == "D":
        # 13 points, every 2h
        start_hour = since.replace(minute=0, second=0, microsecond=0)
        trade_idx = 0
        cum_pnl = pnl_before

        for h in range(0, 25, 2):
            hour_time = start_hour + timedelta(hours=h)
            hour_ts = int(hour_time.timestamp())

            # Sum trades closed before this time point
            while trade_idx < len(closed_trades):
                t = closed_trades[trade_idx]
                t_ts = int(t.closed_at.replace(tzinfo=timezone.utc).timestamp()) if not t.closed_at.tzinfo else int(t.closed_at.timestamp())
                if t_ts > hour_ts:
                    break
                cum_pnl += float(t.pnl_usd or 0)
                trade_idx += 1

            pnl_points.append(PnlHistoryItem(
                timestamp=hour_ts,
                pnl=round(cum_pnl, 2),
            ))

        # Last point: add current unrealized PnL
        unrealized = float(
            db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).filter(
                Trade.user_id == user_id,
                Trade.status == "open",
            ).scalar()
        )
        # Process remaining trades
        while trade_idx < len(closed_trades):
            cum_pnl += float(closed_trades[trade_idx].pnl_usd or 0)
            trade_idx += 1

        if pnl_points:
            pnl_points[-1] = PnlHistoryItem(
                timestamp=pnl_points[-1].timestamp,
                pnl=round(cum_pnl + unrealized, 2),
            )
    else:
        # Daily points based on trade close dates
        # Group trades by day
        trade_idx = 0
        cum_pnl = pnl_before

        # Generate daily points
        current_date = since.date()
        today = now.date()

        while current_date <= today:
            day_start = datetime.combine(current_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            day_end_ts = day_start.timestamp() + 86400

            while trade_idx < len(closed_trades):
                t = closed_trades[trade_idx]
                t_ts = t.closed_at.replace(tzinfo=timezone.utc).timestamp() if not t.closed_at.tzinfo else t.closed_at.timestamp()
                if t_ts >= day_end_ts:
                    break
                cum_pnl += float(t.pnl_usd or 0)
                trade_idx += 1

            pnl_points.append(PnlHistoryItem(
                timestamp=int(day_start.timestamp()),
                pnl=round(cum_pnl, 2),
            ))
            current_date += timedelta(days=1)

        # Last point: add unrealized
        unrealized = float(
            db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).filter(
                Trade.user_id == user_id,
                Trade.status == "open",
            ).scalar()
        )
        if pnl_points:
            pnl_points[-1] = PnlHistoryItem(
                timestamp=pnl_points[-1].timestamp,
                pnl=round(pnl_points[-1].pnl + unrealized, 2),
            )

    # ── Thin out points if too many (M/ALL can produce hundreds) ──
    max_points = 60
    if len(pnl_points) > max_points:
        step = len(pnl_points) / max_points
        thinned = [pnl_points[int(i * step)] for i in range(max_points - 1)]
        thinned.append(pnl_points[-1])  # always keep last
        pnl_points = thinned

    # Ensure at least 2 points
    if len(pnl_points) < 2:
        since_ts = int(since.replace(tzinfo=timezone.utc).timestamp()) if not since.tzinfo else int(since.timestamp())
        pnl_points.insert(0, PnlHistoryItem(timestamp=since_ts, pnl=round(pnl_before, 2)))

    # Range P&L
    range_pnl = 0.0
    range_pnl_pct = 0.0
    if len(pnl_points) >= 2:
        range_pnl = round(pnl_points[-1].pnl - pnl_points[0].pnl, 2)
        # Denominator: user's balance at range start
        balance = _get_realtime_balance(db, user_id)
        if balance > 0:
            range_pnl_pct = round(range_pnl / balance * 100, 2)

    return PnlHistoryResponse(
        data=pnl_points,
        range_pnl=range_pnl,
        range_pnl_pct=range_pnl_pct,
        total_pnl=all_time_pnl,
    )