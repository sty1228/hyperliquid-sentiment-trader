import time
import logging
import threading
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from backend.deps import get_db
from backend.database import SessionLocal
from backend.models.setting import BalanceSnapshot, BalanceEvent
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


class TransactionItem(BaseModel):
    id: str
    type: str          # "deposit" | "withdraw"
    amount: float
    status: str        # "bridging" | "bridged" | "failed" | "initiated" | "sending" | "completed" | "timeout"
    tx_hash: str | None = None
    created_at: str
    completed_at: str | None = None


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


# ═══════════════════════════════════════════════════════
# ★ Unified Transaction History (deposits + withdrawals)
# ═══════════════════════════════════════════════════════

@router.get("/transactions", response_model=list[TransactionItem])
def get_transactions(
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Unified deposit + withdrawal history, newest first."""
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
            tx_hash=r.bridge_tx_hash or r.arb_tx_hash,
            created_at=r.created_at.isoformat(),
            completed_at=r.bridged_at.isoformat() if r.bridged_at else None,
        )
        for r in records
    ]


# ═══════════════════════════════════════════════════════
# Withdraw — with withdraw_pending flag + transaction record
# ═══════════════════════════════════════════════════════

def _clear_withdraw_pending(user_id: str):
    """Clear the withdraw_pending flag so deposit_monitor resumes."""
    db = SessionLocal()
    try:
        w = db.query(UserWallet).filter(UserWallet.user_id == user_id).first()
        if w:
            w.withdraw_pending = False
            db.commit()
            logger.info(f"[Withdraw] Cleared withdraw_pending for user {user_id}")
    except Exception as e:
        logger.error(f"[Withdraw] Failed to clear withdraw_pending: {e}")
    finally:
        db.close()


def _update_tx_status(tx_id: str, status: str, tx_hash: str | None = None):
    """Update a wallet_deposits record status."""
    db = SessionLocal()
    try:
        rec = db.query(WalletDeposit).filter(WalletDeposit.id == tx_id).first()
        if rec:
            rec.status = status
            if tx_hash:
                rec.arb_tx_hash = tx_hash
            if status in ("completed", "failed", "timeout"):
                rec.bridged_at = datetime.utcnow()
            db.commit()
    except Exception as e:
        logger.error(f"[Withdraw] Failed to update tx status: {e}")
    finally:
        db.close()


def _process_withdraw_background(
    user_id: str, wallet_address: str, withdraw_address: str,
    private_key: str, amount: float, tx_id: str,
):
    """Background thread: wait for USDC on Arb, then transfer to user wallet."""
    db = SessionLocal()
    try:
        from backend.services.wallet_manager import get_usdc_balance, transfer_usdc_to_user, ensure_gas

        # Wait for USDC to land on Arb (up to 5 min)
        for i in range(60):
            time.sleep(5)
            bal = get_usdc_balance(wallet_address)
            if bal >= amount * 0.95:
                logger.info(f"[Withdraw] USDC landed: {bal:.2f} for user {user_id}")
                break
        else:
            logger.error(f"[Withdraw] Timeout waiting for USDC on Arb for user {user_id}")
            _update_tx_status(tx_id, "timeout")
            _clear_withdraw_pending(user_id)
            return

        _update_tx_status(tx_id, "sending")

        # Ensure gas for transfer
        ensure_gas(wallet_address)

        # Transfer to user wallet
        tx_hash = transfer_usdc_to_user(private_key, withdraw_address, amount)
        logger.info(f"[Withdraw] Sent {amount:.2f} USDC to {withdraw_address}, tx: {tx_hash}")

        _update_tx_status(tx_id, "completed", tx_hash)

        # Update BalanceSnapshot
        snapshot = db.query(BalanceSnapshot).filter(BalanceSnapshot.user_id == user_id).first()
        if snapshot:
            snapshot.balance = max(0, snapshot.balance - amount)
            snapshot.available = max(0, snapshot.available - amount)

        event = BalanceEvent(
            user_id=user_id,
            event_type="withdraw",
            amount=amount,
            balance_after=snapshot.balance if snapshot else 0,
        )
        db.add(event)
        db.commit()

    except Exception as e:
        logger.error(f"[Withdraw] Background failed for user {user_id}: {e}")
        _update_tx_status(tx_id, "failed")
    finally:
        _clear_withdraw_pending(user_id)
        db.close()


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

    # Block if another withdraw is already pending
    if wallet.withdraw_pending:
        raise HTTPException(status_code=409, detail="A withdrawal is already in progress. Please wait.")

    private_key = decrypt_key(wallet.encrypted_private_key)

    hl_state = get_hl_balance(wallet.address)
    if hl_state["withdrawable"] < req.amount:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient HL balance. Available: {hl_state['withdrawable']:.2f}",
        )

    try:
        # ★ Set withdraw_pending BEFORE initiating HL withdraw
        wallet.withdraw_pending = True

        # ★ Record withdrawal transaction (for history)
        tx_record = WalletDeposit(
            user_id=user.id,
            wallet_address=wallet.address,
            amount=req.amount,
            type="withdraw",
            status="initiated",
        )
        db.add(tx_record)
        db.commit()
        db.refresh(tx_record)
        tx_id = str(tx_record.id)

        # 1. Withdraw from HL (fast — just a signed API call)
        withdraw_from_hl(private_key, req.amount, wallet.address)

        # Update status
        tx_record.status = "hl_withdrawn"
        db.commit()

        # 2. Start background thread to wait for USDC and transfer to user
        t = threading.Thread(
            target=_process_withdraw_background,
            args=(user.id, wallet.address, wallet.withdraw_address, private_key, req.amount, tx_id),
            daemon=True,
        )
        t.start()

        return WithdrawResponse(
            status="processing",
            message=f"Withdrawal of {req.amount:.2f} USDC initiated. Funds will arrive in your wallet in ~5 minutes.",
        )

    except Exception as e:
        wallet.withdraw_pending = False
        db.commit()
        logger.error(f"Withdraw failed for user {user.id}: {e}")
        raise HTTPException(status_code=500, detail=f"Withdrawal failed: {str(e)}")