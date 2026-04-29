"""
Portfolio API — Dashboard 数据（余额、持仓、盈亏曲线）

★ Fix 1: positions current_price 从 pnl_pct 反推，不再用 exit_price
★ Fix 2: 新增 GET /api/portfolio/trader-pnl — 用户实际跟单盈亏 per KOL
★ Fix 3: trader-pnl 新增 pnl_pct 字段（基于 size_usd 计算）
★ Fix 4: _get_realtime_balance 调用 get_hl_balance()，fallback 到 balance_snapshot
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc, func, case
from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.trade import Trade
from backend.models.follow import Follow
from backend.models.trader import Trader
from backend.models.setting import BalanceSnapshot, BalanceEvent
from backend.models.wallet import UserWallet
from backend.models.network_event import NetworkEvent
from backend.services.wallet_manager import get_hl_balance

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
    accountValue: float
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
    # ★ Per-trade TP/SL overrides (manual trades or PATCH /api/trades/{id}/tp-sl).
    # Null = no override, fall back to the user's CopySetting default.
    tp_override_pct: float | None = None
    sl_override_pct: float | None = None
    opened_at: datetime


class DashboardSummary(BaseModel):
    total_balance: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    open_positions: int = 0
    total_trades: int = 0
    win_rate: float = 0.0


class PnlHistoryItem(BaseModel):
    timestamp: int
    pnl: float


class PnlHistoryResponse(BaseModel):
    data: list[PnlHistoryItem] = []
    range_pnl: float = 0.0
    range_pnl_pct: float = 0.0
    total_pnl: float = 0.0


class TraderPnlItem(BaseModel):
    trader_username: str
    pnl_usd: float
    pnl_pct: float = 0.0
    trade_count: int
    open_count: int
    source: str | None = None     # "copy" | "counter" | "mixed"


# ── helpers ──────────────────────────────────────────────

def _calc_trade_pnl(db: Session, user_id: str, since: datetime | None = None) -> float:
    closed_q = db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).filter(
        Trade.user_id == user_id, Trade.status == "closed",
    )
    if since:
        closed_q = closed_q.filter(Trade.closed_at >= since)
    realized = float(closed_q.scalar())
    unrealized = float(
        db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).filter(
            Trade.user_id == user_id, Trade.status == "open",
        ).scalar()
    )
    return round(realized + unrealized, 2)


def _get_realtime_balance(db: Session, user_id: str) -> float:
    """
    Primary: live HL API equity via get_hl_balance() (same as wallet/balance endpoint).
    Fallback: latest balance_snapshot row.
    Returns 0.0 if user has no dedicated wallet yet (new user).
    """
    try:
        wallet = db.query(UserWallet).filter(UserWallet.user_id == user_id).first()
        if wallet:
            hl_state = get_hl_balance(wallet.address)
            equity = float(hl_state.get("equity", 0.0))
            if equity > 0:
                return equity
    except Exception:
        pass

    latest_snap = (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.user_id == user_id)
        .order_by(desc(BalanceSnapshot.snapshot_date))
        .first()
    )
    return float(latest_snap.balance) if latest_snap else 0.0


def _compute_current_price(t: Trade) -> float | None:
    """
    Reverse-compute current price from pnl_pct (updated every 15s by engine).
      long:  mid = entry * (1 + pnl_pct/100)
      short: mid = entry * (1 - pnl_pct/100)
    """
    if t.pnl_pct is None or t.entry_price is None:
        return None
    if t.direction == "long":
        return round(t.entry_price * (1 + t.pnl_pct / 100), 6)
    else:
        return round(t.entry_price * (1 - t.pnl_pct / 100), 6)


# ── API 端点 ─────────────────────────────────────────────

@router.get("/portfolio/profile", response_model=ProfileDataResponse)
def get_profile_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    following_count = db.query(Follow).filter(Follow.user_id == current_user.id).count()
    copy_trading_count = (
        db.query(Follow)
        .filter(Follow.user_id == current_user.id, Follow.is_copy_trading.is_(True))
        .count()
    )
    total_trades = db.query(Trade).filter(Trade.user_id == current_user.id).count()
    account_value = _get_realtime_balance(db, current_user.id)
    copiers_count = 0
    if current_user.twitter_username:
        my_trader = db.query(Trader).filter(Trader.username == current_user.twitter_username).first()
        if my_trader:
            copiers_count = db.query(Follow).filter(
                Follow.trader_id == my_trader.id, Follow.is_copy_trading.is_(True),
            ).count()
    recent_trades = (
        db.query(Trade)
        .filter(Trade.user_id == current_user.id, Trade.status == "closed")
        .order_by(desc(Trade.closed_at))
        .limit(50).all()
    )
    streak, streak_pnl = 0, 0.0
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
        followerCount=copiers_count,
        followerList=[],
        accountValue=account_value,
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
    now = datetime.now(timezone.utc)

    if timeRange == "D":
        since_24h = now - timedelta(hours=24)
        latest_before = (
            db.query(BalanceEvent)
            .filter(BalanceEvent.user_id == current_user.id, BalanceEvent.created_at <= since_24h)
            .order_by(desc(BalanceEvent.created_at)).first()
        )
        opening_balance = latest_before.balance_after if latest_before else 0.0
        if not latest_before:
            snap_before = (
                db.query(BalanceSnapshot)
                .filter(
                    BalanceSnapshot.user_id == current_user.id,
                    BalanceSnapshot.snapshot_date < since_24h.date(),
                )
                .order_by(desc(BalanceSnapshot.snapshot_date)).first()
            )
            opening_balance = snap_before.balance if snap_before else 0.0

        events = (
            db.query(BalanceEvent)
            .filter(BalanceEvent.user_id == current_user.id, BalanceEvent.created_at > since_24h)
            .order_by(BalanceEvent.created_at).all()
        )
        start_hour = since_24h.replace(minute=0, second=0, microsecond=0)
        evt_idx, bal = 0, opening_balance
        result: list[BalanceHistoryItem] = []
        for h in range(0, 25, 2):
            hour_time = start_hour + timedelta(hours=h)
            hour_ts = int(hour_time.timestamp())
            while evt_idx < len(events) and int(events[evt_idx].created_at.timestamp()) <= hour_ts:
                bal = events[evt_idx].balance_after
                evt_idx += 1
            result.append(BalanceHistoryItem(accountValue=bal, timestamp=hour_ts))
        while evt_idx < len(events):
            bal = events[evt_idx].balance_after
            evt_idx += 1
        final_bal = _get_realtime_balance(db, current_user.id)
        if result:
            result[-1] = BalanceHistoryItem(accountValue=final_bal, timestamp=result[-1].timestamp)
        return result

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
        .order_by(BalanceSnapshot.snapshot_date).all()
    )
    result: list[BalanceHistoryItem] = []
    if snapshots and len(snapshots) < 7:
        first_date = snapshots[0].snapshot_date
        days_available = (first_date - since.date()).days
        for i in range(min(7 - len(snapshots), days_available), 0, -1):
            pad_date = first_date - timedelta(days=i)
            result.append(BalanceHistoryItem(
                accountValue=0.0,
                timestamp=int(datetime.combine(pad_date, datetime.min.time()).replace(tzinfo=timezone.utc).timestamp()),
            ))
    result.extend(
        BalanceHistoryItem(
            accountValue=s.balance,
            timestamp=int(datetime.combine(s.snapshot_date, datetime.min.time()).replace(tzinfo=timezone.utc).timestamp()),
        )
        for s in snapshots
    )
    if result:
        final_bal = _get_realtime_balance(db, current_user.id)
        result[-1] = BalanceHistoryItem(accountValue=final_bal, timestamp=result[-1].timestamp)
    return result


@router.get("/portfolio/positions", response_model=list[PositionItem])
def get_open_positions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    positions = (
        db.query(Trade)
        .filter(Trade.user_id == current_user.id, Trade.status == "open")
        .order_by(desc(Trade.opened_at)).all()
    )
    return [
        PositionItem(
            id=t.id,
            ticker=t.ticker,
            direction=t.direction,
            entry_price=t.entry_price,
            current_price=_compute_current_price(t),
            size_usd=t.size_usd,
            size_qty=t.size_qty,
            leverage=t.leverage,
            pnl_usd=t.pnl_usd,
            pnl_pct=t.pnl_pct,
            trader_username=t.trader_username,
            tp_override_pct=t.tp_override_pct,
            sl_override_pct=t.sl_override_pct,
            opened_at=t.opened_at,
        )
        for t in positions
    ]


@router.get("/portfolio/summary", response_model=DashboardSummary)
def get_dashboard_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    balance = _get_realtime_balance(db, current_user.id)
    total_trades = db.query(Trade).filter(Trade.user_id == current_user.id).count()
    open_positions = db.query(Trade).filter(
        Trade.user_id == current_user.id, Trade.status == "open"
    ).count()
    total_pnl = _calc_trade_pnl(db, current_user.id)
    closed_trades = db.query(Trade).filter(
        Trade.user_id == current_user.id, Trade.status == "closed"
    ).all()
    wins = sum(1 for t in closed_trades if t.pnl_usd and t.pnl_usd > 0)
    win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0.0
    pnl_pct = (total_pnl / balance * 100) if balance > 0 else 0.0

    return DashboardSummary(
        total_balance=balance,
        total_pnl=total_pnl,
        total_pnl_pct=pnl_pct,
        open_positions=open_positions,
        total_trades=total_trades,
        win_rate=win_rate,
    )


@router.get("/portfolio/trader-pnl", response_model=list[TraderPnlItem])
def get_trader_pnl(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    User's actual trade PnL grouped by KOL.
    pnl_pct = pnl_usd / total_size_usd * 100
    """
    rows = (
        db.query(
            Trade.trader_username,
            Trade.source,
            func.coalesce(func.sum(Trade.pnl_usd), 0.0).label("pnl_usd"),
            func.coalesce(func.sum(Trade.size_usd), 0.0).label("total_size_usd"),
            func.count(Trade.id).label("trade_count"),
            func.sum(case((Trade.status == "open", 1), else_=0)).label("open_count"),
        )
        .filter(Trade.user_id == current_user.id, Trade.trader_username.isnot(None))
        .group_by(Trade.trader_username, Trade.source)
        .all()
    )

    merged: dict[str, dict] = {}
    for r in rows:
        uname = r.trader_username
        pnl = round(float(r.pnl_usd), 2)
        size = float(r.total_size_usd or 0)
        cnt = int(r.trade_count)
        open_cnt = int(r.open_count or 0)
        src = r.source or "copy"

        if uname in merged:
            m = merged[uname]
            m["pnl_usd"] = round(m["pnl_usd"] + pnl, 2)
            m["total_size_usd"] += size
            m["trade_count"] += cnt
            m["open_count"] += open_cnt
            m["source"] = "mixed" if m["source"] != src else src
        else:
            merged[uname] = {
                "pnl_usd": pnl,
                "total_size_usd": size,
                "trade_count": cnt,
                "open_count": open_cnt,
                "source": src,
            }

    result = []
    for uname, m in merged.items():
        pnl_pct = round(m["pnl_usd"] / m["total_size_usd"] * 100, 2) if m["total_size_usd"] > 0 else 0.0
        result.append(TraderPnlItem(
            trader_username=uname,
            pnl_usd=m["pnl_usd"],
            pnl_pct=pnl_pct,
            trade_count=m["trade_count"],
            open_count=m["open_count"],
            source=m["source"],
        ))
    return result


@router.get("/portfolio/pnl-history", response_model=PnlHistoryResponse)
def get_pnl_history(
    timeRange: str = Query("M", regex="^(D|W|M|YTD|ALL)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    user_id = current_user.id
    all_time_pnl = _calc_trade_pnl(db, user_id)

    if timeRange == "D":
        since = now - timedelta(hours=24)
    elif timeRange == "W":
        since = now - timedelta(weeks=1)
    elif timeRange == "M":
        since = now - timedelta(days=30)
    elif timeRange == "YTD":
        since = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    else:
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)

    closed_trades = (
        db.query(Trade)
        .filter(Trade.user_id == user_id, Trade.status == "closed", Trade.closed_at >= since)
        .order_by(Trade.closed_at).all()
    )
    pnl_before = float(
        db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0)).filter(
            Trade.user_id == user_id, Trade.status == "closed", Trade.closed_at < since,
        ).scalar()
    )
    pnl_points: list[PnlHistoryItem] = []

    if timeRange == "D":
        start_hour = since.replace(minute=0, second=0, microsecond=0)
        trade_idx, cum_pnl = 0, pnl_before
        for h in range(0, 25, 2):
            hour_time = start_hour + timedelta(hours=h)
            hour_ts = int(hour_time.timestamp())
            while trade_idx < len(closed_trades):
                t = closed_trades[trade_idx]
                t_ts = (
                    int(t.closed_at.replace(tzinfo=timezone.utc).timestamp())
                    if not t.closed_at.tzinfo
                    else int(t.closed_at.timestamp())
                )
                if t_ts > hour_ts:
                    break
                cum_pnl += float(t.pnl_usd or 0)
                trade_idx += 1
            pnl_points.append(PnlHistoryItem(timestamp=hour_ts, pnl=round(cum_pnl, 2)))
        unrealized = float(
            db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0))
            .filter(Trade.user_id == user_id, Trade.status == "open")
            .scalar()
        )
        while trade_idx < len(closed_trades):
            cum_pnl += float(closed_trades[trade_idx].pnl_usd or 0)
            trade_idx += 1
        if pnl_points:
            pnl_points[-1] = PnlHistoryItem(
                timestamp=pnl_points[-1].timestamp, pnl=round(cum_pnl + unrealized, 2)
            )
    else:
        trade_idx, cum_pnl = 0, pnl_before
        current_date, today = since.date(), now.date()
        while current_date <= today:
            day_start = datetime.combine(current_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            day_end_ts = day_start.timestamp() + 86400
            while trade_idx < len(closed_trades):
                t = closed_trades[trade_idx]
                t_ts = (
                    t.closed_at.replace(tzinfo=timezone.utc).timestamp()
                    if not t.closed_at.tzinfo
                    else t.closed_at.timestamp()
                )
                if t_ts >= day_end_ts:
                    break
                cum_pnl += float(t.pnl_usd or 0)
                trade_idx += 1
            pnl_points.append(PnlHistoryItem(timestamp=int(day_start.timestamp()), pnl=round(cum_pnl, 2)))
            current_date += timedelta(days=1)
        unrealized = float(
            db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0))
            .filter(Trade.user_id == user_id, Trade.status == "open")
            .scalar()
        )
        if pnl_points:
            pnl_points[-1] = PnlHistoryItem(
                timestamp=pnl_points[-1].timestamp,
                pnl=round(pnl_points[-1].pnl + unrealized, 2),
            )

    max_points = 60
    if len(pnl_points) > max_points:
        step = len(pnl_points) / max_points
        thinned = [pnl_points[int(i * step)] for i in range(max_points - 1)]
        thinned.append(pnl_points[-1])
        pnl_points = thinned

    if len(pnl_points) < 2:
        since_ts = int(since.replace(tzinfo=timezone.utc).timestamp()) if not since.tzinfo else int(since.timestamp())
        pnl_points.insert(0, PnlHistoryItem(timestamp=since_ts, pnl=round(pnl_before, 2)))

    range_pnl, range_pnl_pct = 0.0, 0.0
    if len(pnl_points) >= 2:
        range_pnl = round(pnl_points[-1].pnl - pnl_points[0].pnl, 2)
        balance = _get_realtime_balance(db, user_id)
        if balance > 0:
            range_pnl_pct = round(range_pnl / balance * 100, 2)

    return PnlHistoryResponse(
        data=pnl_points,
        range_pnl=range_pnl,
        range_pnl_pct=range_pnl_pct,
        total_pnl=all_time_pnl,
    )


# ═══════════════════════════════════════════════════════════
#  ★ WELCOME-BACK SUMMARY (2026-04-28)
#  Frontend calls POST /api/portfolio/welcome-back on app boot.
#  If the user was away >= 24h AND has a prior last_seen_at, returns
#  a recap of account performance during the absence. Otherwise
#  returns {"summary": null}. Always updates last_seen_at to now().
# ═══════════════════════════════════════════════════════════

WELCOME_BACK_MIN_GAP_HOURS = 24
WELCOME_BACK_MAX_WINDOW_DAYS = 30


class WelcomeBackTradeRef(BaseModel):
    id: str
    ticker: str
    direction: str
    pnl_usd: float
    pnl_pct: float | None = None
    trader_username: str | None = None


class WelcomeBackTopTrader(BaseModel):
    trader_username: str
    pnl_usd: float
    trade_count: int


class WelcomeBackSummary(BaseModel):
    since: datetime
    until: datetime
    duration_hours: float
    capped_at_30d: bool

    starting_balance_usd: float
    current_balance_usd: float
    balance_delta_usd: float
    balance_delta_pct: float

    realized_pnl_usd: float
    unrealized_pnl_usd_now: float

    trades_opened: int
    trades_closed: int
    wins: int
    losses: int
    win_rate: float

    best_trade: WelcomeBackTradeRef | None = None
    worst_trade: WelcomeBackTradeRef | None = None
    top_trader: WelcomeBackTopTrader | None = None

    events: dict[str, int] = {}


class WelcomeBackResponse(BaseModel):
    summary: WelcomeBackSummary | None = None


def _starting_balance_at(db: Session, user_id: str, since: datetime, fallback: float) -> float:
    """
    Best-effort historical balance just before `since`. Prefers a BalanceEvent
    (intraday granularity) and falls back to the most recent BalanceSnapshot
    on or before `since`. Returns `fallback` (current balance) if neither exists.
    """
    evt = (
        db.query(BalanceEvent)
        .filter(BalanceEvent.user_id == user_id, BalanceEvent.created_at <= since)
        .order_by(desc(BalanceEvent.created_at))
        .first()
    )
    if evt is not None:
        return float(evt.balance_after)
    snap = (
        db.query(BalanceSnapshot)
        .filter(
            BalanceSnapshot.user_id == user_id,
            BalanceSnapshot.snapshot_date <= since.date(),
        )
        .order_by(desc(BalanceSnapshot.snapshot_date))
        .first()
    )
    if snap is not None:
        return float(snap.balance)
    return float(fallback)


@router.post("/portfolio/welcome-back", response_model=WelcomeBackResponse)
def welcome_back(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    prev = current_user.last_seen_at

    # Always touch last_seen_at — even if we won't show the popup.
    current_user.last_seen_at = now
    db.commit()

    # First visit ever, or returning within 24h → no popup.
    if prev is None:
        return WelcomeBackResponse(summary=None)
    # Normalize to UTC if a naive timestamp slipped in (shouldn't, but defensive).
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    gap_hours = (now - prev).total_seconds() / 3600.0
    if gap_hours < WELCOME_BACK_MIN_GAP_HOURS:
        return WelcomeBackResponse(summary=None)

    # Clamp window start at 30 days ago to bound the queries.
    max_window_start = now - timedelta(days=WELCOME_BACK_MAX_WINDOW_DAYS)
    capped = prev < max_window_start
    since = max(prev, max_window_start)

    user_id = current_user.id

    # ── Balances ──
    current_balance = _get_realtime_balance(db, user_id)
    starting_balance = _starting_balance_at(db, user_id, since, fallback=current_balance)
    balance_delta = round(current_balance - starting_balance, 2)
    balance_delta_pct = (
        round(balance_delta / starting_balance * 100, 2) if starting_balance > 0 else 0.0
    )

    # ── Realized PnL during the window ──
    realized = float(
        db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0))
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.closed_at >= since,
        )
        .scalar()
        or 0.0
    )

    # ── Unrealized (current open positions) ──
    unrealized = float(
        db.query(func.coalesce(func.sum(Trade.pnl_usd), 0.0))
        .filter(Trade.user_id == user_id, Trade.status == "open")
        .scalar()
        or 0.0
    )

    # ── Trade counts ──
    trades_opened = (
        db.query(func.count(Trade.id))
        .filter(Trade.user_id == user_id, Trade.opened_at >= since)
        .scalar()
        or 0
    )
    closed_window_q = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == "closed",
        Trade.closed_at >= since,
    )
    trades_closed_rows = closed_window_q.all()
    trades_closed = len(trades_closed_rows)
    wins = sum(1 for t in trades_closed_rows if (t.pnl_usd or 0) > 0)
    losses = sum(1 for t in trades_closed_rows if (t.pnl_usd or 0) <= 0)
    win_rate = round(wins / trades_closed, 3) if trades_closed > 0 else 0.0

    # ── Best / worst trade in the window ──
    def _ref(t: Trade | None) -> WelcomeBackTradeRef | None:
        if t is None or t.pnl_usd is None:
            return None
        return WelcomeBackTradeRef(
            id=t.id,
            ticker=t.ticker,
            direction=t.direction,
            pnl_usd=round(float(t.pnl_usd), 2),
            pnl_pct=round(float(t.pnl_pct), 2) if t.pnl_pct is not None else None,
            trader_username=t.trader_username,
        )

    best_trade = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.closed_at >= since,
            Trade.pnl_usd.isnot(None),
        )
        .order_by(desc(Trade.pnl_usd))
        .first()
    )
    worst_trade = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.closed_at >= since,
            Trade.pnl_usd.isnot(None),
        )
        .order_by(Trade.pnl_usd.asc())
        .first()
    )

    # ── Top performing trader ──
    top_row = (
        db.query(
            Trade.trader_username,
            func.coalesce(func.sum(Trade.pnl_usd), 0.0).label("pnl_usd"),
            func.count(Trade.id).label("trade_count"),
        )
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.closed_at >= since,
            Trade.trader_username.isnot(None),
        )
        .group_by(Trade.trader_username)
        .order_by(desc("pnl_usd"))
        .first()
    )
    top_trader = (
        WelcomeBackTopTrader(
            trader_username=top_row.trader_username,
            pnl_usd=round(float(top_row.pnl_usd), 2),
            trade_count=int(top_row.trade_count),
        )
        if top_row is not None and top_row.trader_username is not None
        else None
    )

    # ── Event counts ──
    event_rows = (
        db.query(NetworkEvent.type, func.count(NetworkEvent.id))
        .filter(
            NetworkEvent.user_id == user_id,
            NetworkEvent.created_at >= since,
        )
        .group_by(NetworkEvent.type)
        .all()
    )
    events: dict[str, int] = {t: int(c) for (t, c) in event_rows}

    summary = WelcomeBackSummary(
        since=since,
        until=now,
        duration_hours=round((now - since).total_seconds() / 3600.0, 1),
        capped_at_30d=capped,
        starting_balance_usd=round(starting_balance, 2),
        current_balance_usd=round(current_balance, 2),
        balance_delta_usd=balance_delta,
        balance_delta_pct=balance_delta_pct,
        realized_pnl_usd=round(realized, 2),
        unrealized_pnl_usd_now=round(unrealized, 2),
        trades_opened=int(trades_opened),
        trades_closed=trades_closed,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        best_trade=_ref(best_trade),
        worst_trade=_ref(worst_trade),
        top_trader=top_trader,
        events=events,
    )
    return WelcomeBackResponse(summary=summary)