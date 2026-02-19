"""Record deposits made through HyperCopy frontend."""

from datetime import date
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.deps import get_db
from backend.api.auth import get_current_user
from backend.models.user import User
from backend.models.setting import BalanceSnapshot

import uuid

router = APIRouter(prefix="/api/portfolio", tags=["deposit"])


class DepositRequest(BaseModel):
    amount: float
    tx_hash: str | None = None


class DepositResponse(BaseModel):
    success: bool
    new_balance: float
    message: str


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