import time
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.deps import get_db
from backend.api.auth import get_current_user
from backend.models.wallet import UserWallet, WalletDeposit
from backend.services.wallet_manager import (
    generate_wallet, encrypt_key, decrypt_key,
    get_usdc_balance, get_hl_balance,
    withdraw_from_hl, transfer_usdc_to_user,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


class WalletResponse(BaseModel):
    address: str
    withdraw_address: str


class BalanceResponse(BaseModel):
    address: str
    arb_usdc: float
    hl_equity: float
    hl_withdrawable: float
    hl_positions: float


class WithdrawRequest(BaseModel):
    amount: float


class WithdrawResponse(BaseModel):
    status: str
    message: str


@router.post("/create", response_model=WalletResponse)
def create_or_get_wallet(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    existing = db.query(UserWallet).filter(UserWallet.user_id == user.id).first()
    if existing:
        return WalletResponse(
            address=existing.address,
            withdraw_address=existing.withdraw_address,
        )

    wallet_data = generate_wallet()

    wallet = UserWallet(
        user_id=user.id,
        address=wallet_data["address"],
        encrypted_private_key=encrypt_key(wallet_data["private_key"]),
        withdraw_address=user.wallet_address,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)

    return WalletResponse(
        address=wallet.address,
        withdraw_address=wallet.withdraw_address,
    )


@router.get("/balance", response_model=BalanceResponse)
def get_wallet_balance(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    wallet = db.query(UserWallet).filter(UserWallet.user_id == user.id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="No wallet found")

    arb_bal = get_usdc_balance(wallet.address)
    hl_state = get_hl_balance(wallet.address)

    return BalanceResponse(
        address=wallet.address,
        arb_usdc=arb_bal,
        hl_equity=hl_state["equity"],
        hl_withdrawable=hl_state["withdrawable"],
        hl_positions=hl_state["positions"],
    )


@router.get("/deposits")
def get_deposit_history(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    deposits = (
        db.query(WalletDeposit)
        .filter(WalletDeposit.user_id == user.id)
        .order_by(WalletDeposit.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "amount": d.amount,
            "status": d.status,
            "arb_tx_hash": d.arb_tx_hash,
            "bridge_tx_hash": d.bridge_tx_hash,
            "created_at": d.created_at.isoformat(),
            "bridged_at": d.bridged_at.isoformat() if d.bridged_at else None,
        }
        for d in deposits
    ]


@router.post("/withdraw", response_model=WithdrawResponse)
def withdraw_to_user(
    req: WithdrawRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    wallet = db.query(UserWallet).filter(UserWallet.user_id == user.id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="No wallet found")

    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")

    private_key = decrypt_key(wallet.encrypted_private_key)

    hl_state = get_hl_balance(wallet.address)
    if hl_state["withdrawable"] < req.amount:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient HL balance. Available: {hl_state['withdrawable']:.2f}",
        )

    try:
        # 1. Withdraw from HL to dedicated wallet on Arb
        withdraw_from_hl(private_key, req.amount, wallet.address)

        # 2. Wait for USDC to land on Arb
        for _ in range(60):
            time.sleep(5)
            bal = get_usdc_balance(wallet.address)
            if bal >= req.amount * 0.99:
                break
        else:
            return WithdrawResponse(
                status="pending",
                message="HL withdrawal initiated but USDC hasn't landed yet. It will be sent automatically.",
            )

        # 3. Transfer to user's whitelisted wallet
        tx_hash = transfer_usdc_to_user(private_key, wallet.withdraw_address, req.amount)

        return WithdrawResponse(
            status="success",
            message=f"Sent {req.amount:.2f} USDC to {wallet.withdraw_address}. Tx: {tx_hash}",
        )

    except Exception as e:
        logger.error(f"Withdraw failed for user {user.id}: {e}")
        raise HTTPException(status_code=500, detail=f"Withdrawal failed: {str(e)}")