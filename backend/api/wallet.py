import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from backend.deps import get_db
from backend.models.setting import BalanceSnapshot, BalanceEvent
from backend.api.auth import get_current_user
from backend.models.wallet import UserWallet, WalletDeposit
from backend.services.wallet_manager import (
    generate_wallet, encrypt_key,
    get_usdc_balance, get_hl_balance,
    CHAIN_ID_TO_LZ_EID,
)

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/api/wallet", tags=["wallet"])

# Allowed withdraw destination chain IDs
ALLOWED_WITHDRAW_CHAINS = {42161} | set(CHAIN_ID_TO_LZ_EID.keys())

CHAIN_NAMES = {
    42161: "Arbitrum", 1: "Ethereum", 10: "Optimism", 137: "Polygon",
    8453: "Base", 43114: "Avalanche", 5000: "Mantle", 534352: "Scroll",
}


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
    chain_id: int = 42161


class WithdrawResponse(BaseModel):
    status: str
    message: str


class TransactionItem(BaseModel):
    id: str
    type: str
    amount: float
    status: str
    target_chain_id: int | None = None
    tx_hash: str | None = None
    created_at: str
    completed_at: str | None = None


# ═══════════════════════════════════════════════════════
# Wallet CRUD
# ═══════════════════════════════════════════════════════

@router.post("/create", response_model=WalletResponse)
@limiter.limit("3/minute")
def create_or_get_wallet(
    request: Request,
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
@limiter.limit("20/minute")
def get_wallet_balance(
    request: Request,
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
@limiter.limit("20/minute")
def get_deposit_history(
    request: Request,
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


# ═══════════════════════════════════════════════════════
# Unified Transaction History
# ═══════════════════════════════════════════════════════

@router.get("/transactions", response_model=list[TransactionItem])
@limiter.limit("20/minute")
def get_transactions(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    records = (
        db.query(WalletDeposit)
        .filter(WalletDeposit.user_id == user.id)
        .order_by(desc(WalletDeposit.created_at))
        .limit(limit)
        .all()
    )
    return [
        TransactionItem(
            id=str(r.id),
            type=r.type or "deposit",
            amount=r.amount,
            status=r.status,
            target_chain_id=r.target_chain_id,
            tx_hash=r.bridge_tx_hash or r.arb_tx_hash,
            created_at=r.created_at.isoformat(),
            completed_at=r.bridged_at.isoformat() if r.bridged_at else None,
        )
        for r in records
    ]


# ═══════════════════════════════════════════════════════
# Withdraw — just set flag, deposit_monitor handles everything
#
# Flow:
#   1. API: validate balance, record tx, set withdraw_pending=True
#   2. Monitor detects withdraw_pending + HL balance:
#      a) usd_transfer to master wallet (free, instant)
#      b) master Arb USDC → user's wallet (15s)
#   3. No $1 HL bridge fee in normal path
# ═══════════════════════════════════════════════════════

@router.post("/withdraw", response_model=WithdrawResponse)
@limiter.limit("5/minute")
def withdraw_to_user(
    request: Request,
    req: WithdrawRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    wallet = db.query(UserWallet).filter(UserWallet.user_id == user.id).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="No wallet found")

    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")

    if req.chain_id not in ALLOWED_WITHDRAW_CHAINS:
        raise HTTPException(status_code=400, detail=f"Unsupported chain: {req.chain_id}")

    if wallet.withdraw_pending:
        raise HTTPException(
            status_code=409,
            detail="A withdrawal is already in progress. Please wait.",
        )

    # Check HL balance (this is where the funds are)
    hl_state = get_hl_balance(wallet.address)
    if hl_state["withdrawable"] < req.amount:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. Available: {hl_state['withdrawable']:.2f}",
        )

    is_cross_chain = req.chain_id != 42161
    chain_name = CHAIN_NAMES.get(req.chain_id, f"chain {req.chain_id}")

    try:
        # 1. Record transaction
        tx_record = WalletDeposit(
            user_id=user.id,
            wallet_address=wallet.address,
            amount=req.amount,
            type="withdraw",
            status="initiated",
            target_chain_id=req.chain_id,
            destination_address=wallet.withdraw_address,
        )
        db.add(tx_record)

        # 2. Set withdraw_pending — monitor will handle everything
        wallet.withdraw_pending = True
        db.commit()

        # NOTE: We do NOT call withdraw_from_hl here anymore.
        # The deposit_monitor will:
        #   1. Detect HL balance + withdraw_pending
        #   2. usd_transfer to master wallet (free)
        #   3. Master pays user from Arb USDC

        if is_cross_chain:
            msg = (
                f"Withdrawal of {req.amount:.2f} USDC initiated. "
                f"Bridging to {chain_name} via Stargate V2. "
                f"Funds will arrive in ~3-5 minutes."
            )
        else:
            msg = (
                f"Withdrawal of {req.amount:.2f} USDC initiated. "
                f"Funds will arrive in your Arbitrum wallet in ~1-2 minutes."
            )

        logger.info(
            f"[Withdraw] User {user.id}: {req.amount:.2f} USDC "
            f"→ {chain_name} ({wallet.withdraw_address[:10]}...). "
            f"Monitor will process via zero-fee path."
        )

        return WithdrawResponse(status="processing", message=msg)

    except Exception as e:
        wallet.withdraw_pending = False
        if tx_record:
            tx_record.status = "failed"
        db.commit()
        logger.error(f"[Withdraw] Failed for user {user.id}: {e}")
        raise HTTPException(status_code=500, detail=f"Withdrawal failed: {str(e)}")