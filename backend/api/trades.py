"""
Trades API — 交易历史 + 手动平仓
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc
import requests as http_requests

from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.trade import Trade
from backend.models.wallet import UserWallet
from backend.services.wallet_manager import decrypt_key, execute_copy_trade

router = APIRouter(prefix="/api", tags=["trades"])
log = logging.getLogger("trades_api")

SLIPPAGE_BPS = 50  # 0.5%
HL_INFO_URL = "https://api.hyperliquid.xyz/info"


# ── Helpers (duplicated from trading_engine to avoid circular import) ──


def _round_price(raw: float) -> float:
    """Tiered price rounding to satisfy HL tick size requirements."""
    if raw >= 10000:
        return round(raw)
    elif raw >= 100:
        return round(raw, 1)
    elif raw >= 1:
        return round(raw, 2)
    else:
        return round(raw, 4)


def _parse_order_result(result: dict) -> tuple[bool, float]:
    """Parse HL order response → (filled, avgPx)."""
    try:
        statuses = (
            result.get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        for st in statuses:
            if "filled" in st:
                return True, float(st["filled"].get("avgPx", 0))
            if "resting" in st:
                return True, 0.0
            if "error" in st:
                log.warning(f"Order error: {st['error']}")
                return False, 0.0
    except Exception:
        pass
    return False, 0.0


def _get_mid_price(ticker: str) -> float | None:
    """Fetch current mid price from HL for a single ticker."""
    try:
        r = http_requests.post(
            HL_INFO_URL,
            json={"type": "allMids"},
            timeout=10,
        )
        r.raise_for_status()
        mids = r.json()
        val = mids.get(ticker)
        return float(val) if val else None
    except Exception as e:
        log.error(f"Failed to fetch mid price for {ticker}: {e}")
        return None


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


class CloseTradeResponse(BaseModel):
    id: str
    ticker: str
    direction: str
    entry_price: float
    exit_price: float
    size_usd: float
    size_qty: float
    leverage: float
    pnl_usd: float
    pnl_pct: float
    status: str
    source: str
    closed_at: datetime


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


# ═══════════════════════════════════════════════════════════
#  ★ CLOSE POSITION — user-initiated manual close
# ═══════════════════════════════════════════════════════════


@router.post("/trades/{trade_id}/close", response_model=CloseTradeResponse)
def close_trade(
    trade_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    手动平仓：用户主动关闭一个 open 仓位。
    1. 查找交易，验证归属
    2. 查找用户 dedicated wallet
    3. 获取当前市场价
    4. 发送 reduce_only IOC 限价单
    5. 更新 DB 中的交易状态
    """
    # ── 1. Find & validate trade ──
    trade = (
        db.query(Trade)
        .filter(Trade.id == trade_id, Trade.user_id == current_user.id)
        .first()
    )
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != "open":
        raise HTTPException(
            status_code=400,
            detail=f"Trade is already {trade.status}, cannot close",
        )

    # ── 2. Find wallet ──
    wallet = (
        db.query(UserWallet)
        .filter(
            UserWallet.user_id == current_user.id,
            UserWallet.is_active.is_(True),
        )
        .first()
    )
    if not wallet:
        raise HTTPException(
            status_code=400,
            detail="No active trading wallet found",
        )

    # ── 3. Get current market price ──
    mid = _get_mid_price(trade.ticker)
    if not mid:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot fetch current price for {trade.ticker}",
        )

    # ── 4. Execute close order on HyperLiquid ──
    pk = decrypt_key(wallet.encrypted_private_key)

    # To close: buy if we're short, sell if we're long
    is_buy = trade.direction == "short"

    slip = SLIPPAGE_BPS / 10_000
    raw_price = mid * (1 + slip) if is_buy else mid * (1 - slip)
    price = _round_price(raw_price)

    try:
        result = execute_copy_trade(
            private_key=pk,
            coin=trade.ticker,
            is_buy=is_buy,
            size=trade.size_qty,
            price=price,
            reduce_only=True,
        )
    except Exception as e:
        log.error(f"Close order failed for trade {trade_id}: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to execute close order on HyperLiquid: {str(e)}",
        )

    filled, avg_px = _parse_order_result(result)

    if not filled:
        # Try to extract error message from result
        error_msg = "Order not filled"
        try:
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            for st in statuses:
                if "error" in st:
                    error_msg = st["error"]
                    break
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=f"Close order not filled: {error_msg}",
        )

    exit_price = avg_px if avg_px > 0 else mid

    # ── 5. Update trade in DB ──
    trade.status = "closed"
    trade.exit_price = exit_price
    trade.closed_at = datetime.now(timezone.utc)

    if trade.direction == "long":
        trade.pnl_pct = round(
            (exit_price - trade.entry_price) / trade.entry_price * 100, 2
        )
    else:
        trade.pnl_pct = round(
            (trade.entry_price - exit_price) / trade.entry_price * 100, 2
        )
    trade.pnl_usd = round(
        trade.pnl_pct / 100 * trade.size_usd * trade.leverage, 2
    )

    db.commit()
    db.refresh(trade)

    log.info(
        f"✅ MANUAL CLOSE {trade.ticker} {trade.direction} "
        f"user {current_user.id[:8]}… "
        f"exit={exit_price:.2f} PnL={trade.pnl_pct:+.1f}% (${trade.pnl_usd:+.2f})"
    )

    return CloseTradeResponse(
        id=trade.id,
        ticker=trade.ticker,
        direction=trade.direction,
        entry_price=trade.entry_price,
        exit_price=exit_price,
        size_usd=trade.size_usd,
        size_qty=trade.size_qty,
        leverage=trade.leverage,
        pnl_usd=trade.pnl_usd,
        pnl_pct=trade.pnl_pct,
        status=trade.status,
        source=trade.source,
        closed_at=trade.closed_at,
    )