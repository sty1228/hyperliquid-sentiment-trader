"""Legacy deposit/withdraw endpoints — DISABLED.

These manually modified BalanceSnapshot/BalanceEvent and conflicted with
the trading engine's sync_balances(). All deposit/withdraw operations now
go through the dedicated wallet system (/api/wallet/*).

Kept as 410 Gone so any residual frontend calls fail loudly.
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/portfolio", tags=["deposit"])

_MSG = (
    "This endpoint is deprecated. "
    "Use /api/wallet/withdraw for withdrawals and "
    "deposit USDC directly to your dedicated wallet address."
)


@router.post("/record-deposit")
def record_deposit():
    raise HTTPException(status_code=410, detail=_MSG)


@router.post("/record-withdraw")
def record_withdraw():
    raise HTTPException(status_code=410, detail=_MSG)