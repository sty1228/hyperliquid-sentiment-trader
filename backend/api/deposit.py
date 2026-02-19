"""Record deposits and withdrawals made through HyperCopy frontend."""

from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.deps import get_db
from backend.api.auth import get_current_user
from backend.models.user import User
from backend.models.setting import BalanceSnapshot

import uuid

router = APIRouter(prefix="/api/portfolio", tags=["deposit"])


# ── Request / Response 模型 ──────────────────────────────

class DepositRequest(BaseModel):
    amount: float
    tx_hash: str | None = None


class DepositResponse(BaseModel):
    success: bool
    new_balance: float
    message: str


class WithdrawRequest(BaseModel):
    amount: float


class WithdrawResponse(BaseModel):
    success: bool
    new_balance: float
    message: str


# ── Deposit ──────────────────────────────────────────────

@router.post("/record-deposit", response_model=DepositResponse)
def record_deposit(
    req: DepositRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if req.amount < 5:
        return DepositResponse(success=False, new_balance=0, message="Minimum deposit is 5 USDC")

    today = date.today()

    snapshot = (
        db.query(BalanceSnapshot)
        .filter(
            BalanceSnapshot.user_id == current_user.id,
            BalanceSnapshot.snapshot_date == today,
        )
        .first()
    )

    if snapshot:
        snapshot.balance += req.amount
        snapshot.available += req.amount
    else:
        prev = (
            db.query(BalanceSnapshot)
            .filter(BalanceSnapshot.user_id == current_user.id)
            .order_by(BalanceSnapshot.snapshot_date.desc())
            .first()
        )
        prev_balance = prev.balance if prev else 0.0

        snapshot = BalanceSnapshot(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            balance=prev_balance + req.amount,
            available=prev_balance + req.amount,
            used=0.0,
            pnl_daily=0.0,
            snapshot_date=today,
        )
        db.add(snapshot)

    db.commit()
    db.refresh(snapshot)

    return DepositResponse(
        success=True,
        new_balance=snapshot.balance,
        message=f"Recorded ${req.amount:.2f} deposit",
    )


# ── Withdraw ─────────────────────────────────────────────

@router.post("/record-withdraw", response_model=WithdrawResponse)
def record_withdraw(
    req: WithdrawRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")

    # 查最新余额
    latest = (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.user_id == current_user.id)
        .order_by(BalanceSnapshot.snapshot_date.desc())
        .first()
    )

    if not latest or latest.balance < req.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    today = date.today()

    snapshot = (
        db.query(BalanceSnapshot)
        .filter(
            BalanceSnapshot.user_id == current_user.id,
            BalanceSnapshot.snapshot_date == today,
        )
        .first()
    )

    if snapshot:
        # 今天已有快照，扣减
        if snapshot.balance < req.amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")
        snapshot.balance = round(snapshot.balance - req.amount, 2)
        snapshot.available = round(snapshot.available - req.amount, 2)
    else:
        # 新建今天的快照，基于上一条扣减
        snapshot = BalanceSnapshot(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            balance=round(latest.balance - req.amount, 2),
            available=round((latest.available or latest.balance) - req.amount, 2),
            used=latest.used or 0.0,
            pnl_daily=latest.pnl_daily or 0.0,
            snapshot_date=today,
        )
        db.add(snapshot)

    db.commit()
    db.refresh(snapshot)

    return WithdrawResponse(
        success=True,
        new_balance=snapshot.balance,
        message=f"Recorded ${req.amount:.2f} withdrawal",
    )