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
    """盈亏曲线（匹配前端 BalanceHistoryResponse）"""
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


# ══════════════════════════════════════════════════════════
# ★ P&L History — 剥离出入金的真实损益曲线
# ══════════════════════════════════════════════════════════

def _get_cumulative_net_deposits(
    events: list[BalanceEvent],
    up_to_ts: float,
) -> float:
    """
    给定已按 created_at 排序的 events 列表，
    返回截至 up_to_ts（unix timestamp）的累计净入金。
    """
    total = 0.0
    for e in events:
        evt_ts = (
            e.created_at.timestamp()
            if e.created_at.tzinfo
            else e.created_at.replace(tzinfo=timezone.utc).timestamp()
        )
        if evt_ts > up_to_ts:
            break
        amt = float(e.amount) if e.amount else 0.0
        if e.event_type == "deposit":
            total += amt
        elif e.event_type == "withdraw":
            total -= amt
    return total


@router.get("/portfolio/pnl-history", response_model=PnlHistoryResponse)
def get_pnl_history(
    timeRange: str = Query("M", regex="^(D|W|M|YTD|ALL)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    P&L 曲线（Polymarket 风格）。
    P&L = account_value - cumulative_net_deposits
    出入金完全剥离，只反映交易表现。
    """
    now = datetime.now(timezone.utc)
    user_id = current_user.id

    # ── 所有出入金事件（计算累计净入金需要完整历史） ──
    all_events = (
        db.query(BalanceEvent)
        .filter(BalanceEvent.user_id == user_id)
        .order_by(BalanceEvent.created_at)
        .all()
    )

    # 总净入金（all-time）
    total_net_deposits = 0.0
    for e in all_events:
        amt = float(e.amount) if e.amount else 0.0
        if e.event_type == "deposit":
            total_net_deposits += amt
        elif e.event_type == "withdraw":
            total_net_deposits -= amt

    # 当前余额
    latest_snap = (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.user_id == user_id)
        .order_by(desc(BalanceSnapshot.snapshot_date))
        .first()
    )
    current_balance = float(latest_snap.balance) if latest_snap else 0.0
    all_time_pnl = round(current_balance - total_net_deposits, 2)

    # ────────────────────────────────────────────────
    # "D" 视图：用 BalanceEvent 构建小时级 P&L 曲线
    # ────────────────────────────────────────────────
    if timeRange == "D":
        since_24h = now - timedelta(hours=24)

        # 起始余额
        latest_before = (
            db.query(BalanceEvent)
            .filter(
                BalanceEvent.user_id == user_id,
                BalanceEvent.created_at <= since_24h,
            )
            .order_by(desc(BalanceEvent.created_at))
            .first()
        )
        if latest_before:
            opening_balance = float(latest_before.balance_after)
        else:
            snap_before = (
                db.query(BalanceSnapshot)
                .filter(
                    BalanceSnapshot.user_id == user_id,
                    BalanceSnapshot.snapshot_date < since_24h.date(),
                )
                .order_by(desc(BalanceSnapshot.snapshot_date))
                .first()
            )
            opening_balance = float(snap_before.balance) if snap_before else 0.0

        # 24h 内事件
        recent_events = (
            db.query(BalanceEvent)
            .filter(
                BalanceEvent.user_id == user_id,
                BalanceEvent.created_at > since_24h,
            )
            .order_by(BalanceEvent.created_at)
            .all()
        )

        # 24h 前的累计净入金
        since_24h_ts = since_24h.timestamp()
        cum_net_before = _get_cumulative_net_deposits(all_events, since_24h_ts)

        # 构建 13 个点（每 2 小时）
        start_hour = since_24h.replace(minute=0, second=0, microsecond=0)
        evt_idx = 0
        bal = opening_balance
        cum_net = cum_net_before
        pnl_points: list[PnlHistoryItem] = []

        for h in range(0, 25, 2):
            hour_time = start_hour + timedelta(hours=h)
            hour_ts = int(hour_time.timestamp())

            while evt_idx < len(recent_events):
                e = recent_events[evt_idx]
                e_ts = (
                    int(e.created_at.timestamp())
                    if e.created_at.tzinfo
                    else int(e.created_at.replace(tzinfo=timezone.utc).timestamp())
                )
                if e_ts > hour_ts:
                    break
                bal = float(e.balance_after)
                amt = float(e.amount) if e.amount else 0.0
                if e.event_type == "deposit":
                    cum_net += amt
                elif e.event_type == "withdraw":
                    cum_net -= amt
                evt_idx += 1

            pnl_points.append(PnlHistoryItem(
                timestamp=hour_ts,
                pnl=round(bal - cum_net, 2),
            ))

        # 处理剩余事件
        while evt_idx < len(recent_events):
            e = recent_events[evt_idx]
            bal = float(e.balance_after)
            amt = float(e.amount) if e.amount else 0.0
            if e.event_type == "deposit":
                cum_net += amt
            elif e.event_type == "withdraw":
                cum_net -= amt
            evt_idx += 1

        # 最后一个点用最新余额
        if pnl_points:
            pnl_points[-1] = PnlHistoryItem(
                timestamp=pnl_points[-1].timestamp,
                pnl=round(current_balance - total_net_deposits, 2),
            )

        # 计算区间 P&L
        range_pnl = 0.0
        range_pnl_pct = 0.0
        if len(pnl_points) >= 2:
            range_pnl = round(pnl_points[-1].pnl - pnl_points[0].pnl, 2)
            if opening_balance > 0:
                range_pnl_pct = round(range_pnl / opening_balance * 100, 2)

        return PnlHistoryResponse(
            data=pnl_points,
            range_pnl=range_pnl,
            range_pnl_pct=range_pnl_pct,
            total_pnl=all_time_pnl,
        )

    # ────────────────────────────────────────────────
    # 非 D 视图：用 BalanceSnapshot（日级快照）
    # ────────────────────────────────────────────────
    if timeRange == "W":
        since = now - timedelta(weeks=1)
    elif timeRange == "M":
        since = now - timedelta(days=30)
    elif timeRange == "YTD":
        since = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    else:  # ALL
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)

    snapshots = (
        db.query(BalanceSnapshot)
        .filter(
            BalanceSnapshot.user_id == user_id,
            BalanceSnapshot.snapshot_date >= since.date(),
        )
        .order_by(BalanceSnapshot.snapshot_date)
        .all()
    )

    if not snapshots:
        return PnlHistoryResponse(
            data=[],
            range_pnl=0.0,
            range_pnl_pct=0.0,
            total_pnl=all_time_pnl,
        )

    # 构建 P&L 数据点：双指针遍历 snapshots + all_events
    evt_idx = 0
    cum_net = 0.0
    pnl_points: list[PnlHistoryItem] = []

    for snap in snapshots:
        snap_dt = datetime.combine(snap.snapshot_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        snap_ts = snap_dt.timestamp()

        # 推进事件指针到该快照日期
        while evt_idx < len(all_events):
            e = all_events[evt_idx]
            e_ts = (
                e.created_at.timestamp()
                if e.created_at.tzinfo
                else e.created_at.replace(tzinfo=timezone.utc).timestamp()
            )
            if e_ts > snap_ts + 86400:
                break
            amt = float(e.amount) if e.amount else 0.0
            if e.event_type == "deposit":
                cum_net += amt
            elif e.event_type == "withdraw":
                cum_net -= amt
            evt_idx += 1

        acct_val = float(snap.balance)
        pnl = round(acct_val - cum_net, 2)
        pnl_points.append(PnlHistoryItem(
            timestamp=int(snap_ts),
            pnl=pnl,
        ))

    # 计算区间 P&L
    range_pnl = 0.0
    range_pnl_pct = 0.0
    if len(pnl_points) >= 2:
        range_pnl = round(pnl_points[-1].pnl - pnl_points[0].pnl, 2)
        start_equity = float(snapshots[0].balance)
        if start_equity > 0:
            range_pnl_pct = round(range_pnl / start_equity * 100, 2)
    elif len(pnl_points) == 1:
        range_pnl = pnl_points[0].pnl
        if total_net_deposits > 0:
            range_pnl_pct = round(range_pnl / total_net_deposits * 100, 2)

    return PnlHistoryResponse(
        data=pnl_points,
        range_pnl=range_pnl,
        range_pnl_pct=range_pnl_pct,
        total_pnl=all_time_pnl,
    )