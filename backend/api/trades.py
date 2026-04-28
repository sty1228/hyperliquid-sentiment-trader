"""
Trades API — 交易历史 + 手动平仓 + 手动下单 (2026-04-28)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from sqlalchemy import desc
import requests as http_requests
import math
from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.trade import Trade
from backend.models.trader import Trader
from backend.models.wallet import UserWallet
from backend.services.wallet_manager import decrypt_key, execute_copy_trade
from backend.services.events import publish as _publish_event

router = APIRouter(prefix="/api", tags=["trades"])
log = logging.getLogger("trades_api")

SLIPPAGE_BPS = 50  # 0.5%
MIN_TRADE_USD = 10.0
HL_INFO_URL = "https://api.hyperliquid.xyz/info"


# ── Helpers (duplicated from trading_engine to avoid circular import) ──



def _round_price(raw: float) -> float:
    """
    Round price to 5 significant figures — HyperLiquid's rule.
    
    HL rejects orders where the price has more than 5 significant figures.
    Examples:
      87432.1  → 87432.0  (5 sig figs)
      1923.456 → 1923.5   (5 sig figs)
      0.04312  → 0.043120 (5 sig figs)
      65.432   → 65.432   (5 sig figs, already ok)
      0.00789  → 0.007890 (already ok)
    """
    if raw <= 0:
        return 0.0
    # Number of digits before decimal point
    magnitude = math.floor(math.log10(raw)) + 1
    # We want 5 significant figures total
    decimal_places = max(0, 5 - magnitude)
    return round(raw, decimal_places)


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


def _get_sz_decimals(ticker: str) -> int | None:
    """Fetch HL `meta` and return szDecimals for the given ticker. None if absent."""
    try:
        r = http_requests.post(HL_INFO_URL, json={"type": "meta"}, timeout=10)
        r.raise_for_status()
        for a in r.json().get("universe", []):
            if a.get("name") == ticker:
                return int(a.get("szDecimals", 2))
    except Exception as e:
        log.error(f"Failed to fetch HL meta for {ticker}: {e}")
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

    try:
        _publish_event(
            db, current_user.id, "trade_closed",
            {
                "trader_username": trade.trader_username,
                "ticker": trade.ticker,
                "direction": trade.direction,
                "source": trade.source,
                "size_usd": round(trade.size_usd, 2),
                "pnl_usd": trade.pnl_usd,
                "reason": "manual",
            },
        )
    except Exception as e:
        log.warning(f"publish manual close event failed: {e}")

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


# ═══════════════════════════════════════════════════════════
#  ★ MANUAL OPEN — industry-standard interface (2026-04-28)
# ═══════════════════════════════════════════════════════════


class ManualOpenRequest(BaseModel):
    ticker: str
    direction: Literal["long", "short"]
    size_usd: float = Field(..., gt=0)
    leverage: float = Field(5.0, ge=1.0, le=50.0)
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    tp_pct: float | None = Field(None, ge=0)
    sl_pct: float | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _check_limit_price(self) -> "ManualOpenRequest":
        if self.order_type == "limit" and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("limit_price > 0 is required when order_type='limit'")
        return self


class TpSlPatchRequest(BaseModel):
    """Null clears the override and reverts to the user's CopySetting defaults."""
    tp_pct: float | None = Field(None, ge=0)
    sl_pct: float | None = Field(None, ge=0)


@router.post("/trades/manual", response_model=TradeResponse)
def open_manual_trade(
    body: ManualOpenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Place a manual market or limit order. Persists a Trade row with source='manual'
    and signal_id=NULL. Enforces the same invariants as the copy path:
      - same-ticker conflict guard
      - withdrawable / equity floor
      - HL meta whitelist + szDecimals rounding
      - HL 5-sig-fig price rounding
      - ghost-position recovery on DB write failure
    Manual trades do NOT consume the referral free-trade quota and pay full builder fees.
    """
    coin = body.ticker.upper().strip()

    # ── 1. Same-ticker conflict guard (engine enforces the same rule on copy path) ──
    existing = (
        db.query(Trade)
        .filter(
            Trade.user_id == current_user.id,
            Trade.ticker == coin,
            Trade.status == "open",
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Already have an open {existing.direction} position on {coin}",
        )

    # ── 2. Wallet ──
    wallet = (
        db.query(UserWallet)
        .filter(UserWallet.user_id == current_user.id, UserWallet.is_active.is_(True))
        .first()
    )
    if not wallet:
        raise HTTPException(400, "No active trading wallet found")

    # ── 3. HL meta + mid price ──
    sz_decimals = _get_sz_decimals(coin)
    if sz_decimals is None:
        raise HTTPException(400, f"{coin} not listed on HyperLiquid")
    mid = _get_mid_price(coin)
    if not mid:
        raise HTTPException(502, f"Cannot fetch mid price for {coin}")

    # ── 4. Equity / margin check ──
    from backend.services.wallet_manager import get_hl_balance
    bal = get_hl_balance(wallet.address)
    equity = bal.get("equity", 0.0)
    withdrawable = bal.get("withdrawable", 0.0)
    if withdrawable < MIN_TRADE_USD:
        raise HTTPException(400, f"Withdrawable margin ${withdrawable:.2f} < ${MIN_TRADE_USD}")

    # Cap to 90% of equity AND 90% of withdrawable, same as engine.
    max_alloc = min(equity * 0.9, withdrawable * 0.9)
    usd_alloc = min(body.size_usd, max_alloc)
    if usd_alloc < MIN_TRADE_USD:
        raise HTTPException(
            400, f"Allocation ${usd_alloc:.2f} below minimum ${MIN_TRADE_USD} after margin cap"
        )

    # ── 5. Direction + sizing ──
    is_buy = body.direction == "long"
    notional = usd_alloc * body.leverage

    if body.order_type == "limit":
        ref_price = float(body.limit_price)
        price = _round_price(ref_price)
    else:
        slip = SLIPPAGE_BPS / 10_000
        ref_price = mid * (1 + slip) if is_buy else mid * (1 - slip)
        price = _round_price(ref_price)

    qty = round(notional / mid, sz_decimals)
    if qty <= 0:
        raise HTTPException(400, "Order qty rounds to zero — increase size_usd or leverage")

    # ── 6. Submit on HL (with builder-fee safety net) ──
    pk = decrypt_key(wallet.encrypted_private_key)
    # Set leverage (best-effort; engine uses same helper).
    try:
        from backend.services.trading_engine import _hl_set_leverage, _ensure_builder_approved
        _hl_set_leverage(pk, coin, int(body.leverage), cross=True)
        _ensure_builder_approved(pk, wallet.address)
    except Exception as e:
        log.warning(f"leverage/builder-fee setup failed (continuing): {e}")

    try:
        result = execute_copy_trade(
            private_key=pk, coin=coin, is_buy=is_buy,
            size=qty, price=price, reduce_only=False,
        )
    except Exception as e:
        log.error(f"Manual order submit failed for user {current_user.id[:8]}…: {e}")
        raise HTTPException(502, f"Order submission failed: {e}")

    filled, avg_px = _parse_order_result(result)
    if not filled:
        err = "Order not filled"
        try:
            for st in result.get("response", {}).get("data", {}).get("statuses", []):
                if "error" in st:
                    err = st["error"]
                    break
        except Exception:
            pass
        raise HTTPException(502, f"Order not filled: {err}")

    fill_price = avg_px if avg_px > 0 else mid

    # ── 7. Persist trade with ghost-position recovery ──
    try:
        trade = Trade(
            user_id=current_user.id,
            signal_id=None,
            trader_username=None,
            ticker=coin,
            direction=body.direction,
            entry_price=fill_price,
            size_usd=usd_alloc,
            size_qty=qty,
            leverage=body.leverage,
            status="open",
            source="manual",
            fee_usd=0.0,
            is_fee_free=False,
            tp_override_pct=body.tp_pct,
            sl_override_pct=body.sl_pct,
        )
        db.add(trade)
        db.flush()
    except Exception as db_err:
        log.error(f"  🚨 DB write failed after manual fill — closing ghost: {db_err}")
        db.rollback()
        try:
            from backend.services.trading_engine import _emergency_close_position
            _emergency_close_position(pk, coin, is_buy, qty, mid)
        except Exception as e2:
            log.error(f"  🚨 ghost close also failed: {e2}")
        raise HTTPException(500, "Trade fill succeeded but DB persistence failed; position auto-closed")

    # ── 8. Emit network event ──
    try:
        _publish_event(
            db, current_user.id, "trade_opened",
            {
                "trader_username": None,
                "ticker": coin,
                "direction": body.direction,
                "source": "manual",
                "size_usd": round(usd_alloc, 2),
                "pnl_usd": None,
                "reason": None,
            },
        )
    except Exception as e:
        log.warning(f"publish manual trade_opened failed: {e}")

    db.commit()
    db.refresh(trade)

    log.info(
        f"✅ MANUAL OPEN {body.direction.upper()} {coin} user {current_user.id[:8]}… "
        f"qty={qty} @ {fill_price:.4f}"
    )

    return TradeResponse(
        id=trade.id,
        ticker=trade.ticker,
        direction=trade.direction,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        size_usd=trade.size_usd,
        size_qty=trade.size_qty,
        leverage=trade.leverage,
        pnl_usd=trade.pnl_usd,
        pnl_pct=trade.pnl_pct,
        status=trade.status,
        source=trade.source,
        trader_username=trade.trader_username,
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
    )


# ═══════════════════════════════════════════════════════════
#  ★ MODIFY TP/SL on an open trade (DB-only — TP/SL is engine-side)
# ═══════════════════════════════════════════════════════════


@router.patch("/trades/{trade_id}/tp-sl")
def patch_trade_tp_sl(
    trade_id: str,
    body: TpSlPatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update per-trade TP/SL overrides. Pure DB write — HL has no trigger primitives
    in this codebase; the engine's update_positions loop polls pnl_pct against the
    override (or CopySetting default) and fires reduce-only closes when threshold hits.
    Pass null to clear the override and revert to the user's CopySetting defaults.
    """
    trade = (
        db.query(Trade)
        .filter(Trade.id == trade_id, Trade.user_id == current_user.id)
        .first()
    )
    if not trade:
        raise HTTPException(404, "Trade not found")
    if trade.status != "open":
        raise HTTPException(400, f"Trade is {trade.status}, cannot modify TP/SL")

    trade.tp_override_pct = body.tp_pct
    trade.sl_override_pct = body.sl_pct
    db.commit()
    db.refresh(trade)
    return {
        "trade_id": trade.id,
        "tp_override_pct": trade.tp_override_pct,
        "sl_override_pct": trade.sl_override_pct,
    }


# ═══════════════════════════════════════════════════════════
#  ★ PARTIAL CLOSE — close N% of an open position
# ═══════════════════════════════════════════════════════════


@router.post("/trades/{trade_id}/partial-close", response_model=TradeResponse)
def partial_close_trade(
    trade_id: str,
    pct: float = Query(..., gt=0, le=100, description="Percent of position to close (1-100)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Close `pct`% of an open position. The remaining qty/size_usd is decremented
    in place; realized PnL accumulates into `realized_pnl_usd`; the row stays
    status='open' until pct=100 (which routes to a full close).
    """
    trade = (
        db.query(Trade)
        .filter(Trade.id == trade_id, Trade.user_id == current_user.id)
        .first()
    )
    if not trade:
        raise HTTPException(404, "Trade not found")
    if trade.status != "open":
        raise HTTPException(400, f"Trade is {trade.status}, cannot partial-close")

    # 100% partial close → full close: route to existing path for consistency.
    if pct >= 99.999:
        return close_trade(trade_id=trade_id, db=db, current_user=current_user)

    sz_decimals = _get_sz_decimals(trade.ticker)
    if sz_decimals is None:
        raise HTTPException(502, f"Cannot resolve HL meta for {trade.ticker}")
    mid = _get_mid_price(trade.ticker)
    if not mid:
        raise HTTPException(502, f"Cannot fetch current price for {trade.ticker}")

    close_qty = round(trade.size_qty * pct / 100.0, sz_decimals)
    if close_qty <= 0:
        raise HTTPException(400, f"pct={pct} rounds to zero qty — try a larger percent")
    if close_qty >= trade.size_qty:
        # Rounding pushed us to full close.
        return close_trade(trade_id=trade_id, db=db, current_user=current_user)

    wallet = (
        db.query(UserWallet)
        .filter(UserWallet.user_id == current_user.id, UserWallet.is_active.is_(True))
        .first()
    )
    if not wallet:
        raise HTTPException(400, "No active trading wallet found")

    pk = decrypt_key(wallet.encrypted_private_key)
    is_buy = trade.direction == "short"  # close is opposite side
    slip = SLIPPAGE_BPS / 10_000
    raw_price = mid * (1 + slip) if is_buy else mid * (1 - slip)
    price = _round_price(raw_price)

    try:
        result = execute_copy_trade(
            private_key=pk, coin=trade.ticker, is_buy=is_buy,
            size=close_qty, price=price, reduce_only=True,
        )
    except Exception as e:
        log.error(f"Partial-close submit failed for trade {trade_id}: {e}")
        raise HTTPException(502, f"Partial-close failed: {e}")

    filled, avg_px = _parse_order_result(result)
    if not filled:
        err = "Order not filled"
        try:
            for st in result.get("response", {}).get("data", {}).get("statuses", []):
                if "error" in st:
                    err = st["error"]
                    break
        except Exception:
            pass
        raise HTTPException(502, f"Partial-close not filled: {err}")

    exit_px = avg_px if avg_px > 0 else mid

    # Realized PnL for the closed slice
    if trade.direction == "long":
        slice_pct = (exit_px - trade.entry_price) / trade.entry_price * 100
    else:
        slice_pct = (trade.entry_price - exit_px) / trade.entry_price * 100
    slice_size_usd = trade.size_usd * pct / 100.0
    slice_pnl_usd = round(slice_pct / 100 * slice_size_usd * trade.leverage, 2)

    trade.size_qty = round(trade.size_qty - close_qty, sz_decimals)
    trade.size_usd = round(trade.size_usd - slice_size_usd, 6)
    trade.realized_pnl_usd = round((trade.realized_pnl_usd or 0.0) + slice_pnl_usd, 2)

    try:
        _publish_event(
            db, current_user.id, "trade_closed",
            {
                "trader_username": trade.trader_username,
                "ticker": trade.ticker,
                "direction": trade.direction,
                "source": trade.source,
                "size_usd": round(slice_size_usd, 2),
                "pnl_usd": slice_pnl_usd,
                "reason": "manual",
                "partial": True,
            },
        )
    except Exception as e:
        log.warning(f"publish partial-close event failed: {e}")

    db.commit()
    db.refresh(trade)

    log.info(
        f"✅ PARTIAL CLOSE {pct:.0f}% {trade.ticker} {trade.direction} "
        f"user {current_user.id[:8]}… slice PnL=${slice_pnl_usd:+.2f} "
        f"remaining qty={trade.size_qty}"
    )

    return TradeResponse(
        id=trade.id,
        ticker=trade.ticker,
        direction=trade.direction,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        size_usd=trade.size_usd,
        size_qty=trade.size_qty,
        leverage=trade.leverage,
        pnl_usd=trade.pnl_usd,
        pnl_pct=trade.pnl_pct,
        status=trade.status,
        source=trade.source,
        trader_username=trade.trader_username,
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
    )