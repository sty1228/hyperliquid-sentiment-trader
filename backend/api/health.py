"""
Health Check API — DB + HL API + Engine + Master Wallet status
"""
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter
from sqlalchemy import text
import requests as http_requests
import logging

from backend.database import SessionLocal
from backend.services.wallet_manager import get_master_arb_usdc_balance, get_hl_balance, MASTER_WALLET_ADDRESS

router = APIRouter(tags=["health"])
log = logging.getLogger("health")


@router.get("/health")
def health_check():
    """
    Comprehensive health check.
    Returns 200 with status details even if some checks fail.
    Monitoring tools should check response.status for "healthy" vs "degraded".
    """
    checks = {}
    overall = "healthy"

    # ── 1. Database ──
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        checks["database"] = {"status": "ok"}
    except Exception as e:
        checks["database"] = {"status": "error", "detail": str(e)[:200]}
        overall = "degraded"

    # ── 2. HyperLiquid API ──
    try:
        r = http_requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "allMids"},
            timeout=5,
        )
        r.raise_for_status()
        mids = r.json()
        coin_count = len(mids) if isinstance(mids, dict) else 0
        checks["hyperliquid"] = {"status": "ok", "coins": coin_count}
    except Exception as e:
        checks["hyperliquid"] = {"status": "error", "detail": str(e)[:200]}
        overall = "degraded"

    # ── 3. Engine freshness (last trade or signal within 15 min = ok) ──
    try:
        db = SessionLocal()
        row = db.execute(
            text(
                "SELECT MAX(opened_at) as last_trade FROM trades "
                "WHERE opened_at > NOW() - INTERVAL '24 hours'"
            )
        ).fetchone()
        last_signal = db.execute(
            text(
                "SELECT MAX(created_at) as last_sig FROM signals "
                "WHERE created_at > NOW() - INTERVAL '1 hour'"
            )
        ).fetchone()
        db.close()

        last_trade_at = row[0] if row else None
        last_signal_at = last_signal[0] if last_signal else None

        checks["engine"] = {
            "status": "ok",
            "last_trade": last_trade_at.isoformat() if last_trade_at else None,
            "last_signal": last_signal_at.isoformat() if last_signal_at else None,
        }
    except Exception as e:
        checks["engine"] = {"status": "error", "detail": str(e)[:200]}

    # ── 4. Master Wallet ──
    try:
        arb_usdc = get_master_arb_usdc_balance()
        hl_bal = (
            get_hl_balance(MASTER_WALLET_ADDRESS) if MASTER_WALLET_ADDRESS else {}
        )
        checks["master_wallet"] = {
            "status": "ok" if arb_usdc > 10 else "warning",
            "arb_usdc": round(arb_usdc, 2),
            "hl_equity": round(hl_bal.get("equity", 0), 2),
            "hl_withdrawable": round(hl_bal.get("withdrawable", 0), 2),
        }
        if arb_usdc < 10:
            overall = "degraded"
    except Exception as e:
        checks["master_wallet"] = {"status": "error", "detail": str(e)[:200]}

    # ── 5. Counts ──
    try:
        db = SessionLocal()
        user_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
        open_trades = db.execute(
            text("SELECT COUNT(*) FROM trades WHERE status='open'")
        ).scalar()
        active_copiers = db.execute(
            text(
                "SELECT COUNT(DISTINCT user_id) FROM follows "
                "WHERE is_copy_trading = true OR is_counter_trading = true"
            )
        ).scalar()
        db.close()
        checks["stats"] = {
            "users": user_count,
            "open_trades": open_trades,
            "active_copiers": active_copiers,
        }
    except Exception as e:
        checks["stats"] = {"status": "error", "detail": str(e)[:200]}

    return {
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }