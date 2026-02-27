"""
Deposit Monitor — watches dedicated wallets for USDC and handles:
  1. DEPOSITS:  wallet(withdraw_pending=False) + USDC → bridge to HL
  2. WITHDRAWALS: wallet(withdraw_pending=True) + USDC →
       a) Arbitrum (chain 42161): direct ERC-20 transfer
       b) Other chains: Stargate V2 bridge out

Run as: python -m backend.services.deposit_monitor
Or via systemd: systemctl start hypercopy-monitor
"""

import time
import logging
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from backend.database import SessionLocal
from backend.models.wallet import UserWallet, WalletDeposit
from backend.models.setting import BalanceSnapshot, BalanceEvent
from backend.services.wallet_manager import (
    get_usdc_balance, bridge_usdc_to_hl, decrypt_key,
    ensure_gas, transfer_usdc_to_user, stargate_bridge_out,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

MIN_DEPOSIT = 5.0    # USDC — below this HL eats it in fees
MIN_WITHDRAW = 0.5   # USDC — minimum to trigger withdraw transfer
POLL_INTERVAL = 15   # seconds

# Stargate bridge needs more ETH for gas + LZ messaging fee
BRIDGE_OUT_MIN_ETH = 0.001
BRIDGE_OUT_TOP_UP_ETH = 0.001


# ═══════════════════════════════════════════════════════
# DEPOSIT FLOW — bridge USDC to HyperLiquid
# ═══════════════════════════════════════════════════════

def _handle_deposit(db, w, balance):
    """Normal deposit: bridge USDC from Arb to HL."""
    logger.info(f"[{w.address[:10]}...] [DEPOSIT] Found {balance:.2f} USDC, bridging to HL...")

    private_key = decrypt_key(w.encrypted_private_key)

    deposit = WalletDeposit(
        user_id=w.user_id,
        wallet_address=w.address,
        amount=balance,
        type="deposit",
        status="bridging",
    )
    db.add(deposit)
    db.commit()

    try:
        if not ensure_gas(w.address):
            deposit.status = "failed"
            db.commit()
            logger.error(f"[{w.address[:10]}...] No gas, skipping bridge")
            return

        tx_hash = bridge_usdc_to_hl(private_key, balance)

        deposit.bridge_tx_hash = tx_hash
        deposit.status = "bridged"
        deposit.bridged_at = datetime.utcnow()

        snapshot = db.query(BalanceSnapshot).filter(
            BalanceSnapshot.user_id == w.user_id
        ).first()

        if snapshot:
            snapshot.balance += balance
            snapshot.available += balance
        else:
            snapshot = BalanceSnapshot(
                user_id=w.user_id,
                balance=balance,
                available=balance,
                used=0,
                pnl_daily=0,
                snapshot_date=datetime.utcnow().date(),
            )
            db.add(snapshot)

        event = BalanceEvent(
            user_id=w.user_id,
            event_type="deposit",
            amount=balance,
            balance_after=snapshot.balance,
        )
        db.add(event)
        db.commit()

        logger.info(f"[{w.address[:10]}...] [DEPOSIT] Bridged {balance:.2f} USDC, tx: {tx_hash}")

    except Exception as e:
        deposit.status = "failed"
        db.commit()
        logger.error(f"[{w.address[:10]}...] [DEPOSIT] Bridge failed: {e}")


# ═══════════════════════════════════════════════════════
# WITHDRAW FLOW — transfer USDC to user's wallet
# ═══════════════════════════════════════════════════════

def _handle_withdraw(db, w, balance):
    """Withdraw completion: Arb direct transfer or Stargate bridge-out."""

    private_key = decrypt_key(w.encrypted_private_key)

    # Find the pending withdraw record to get target_chain_id
    pending_tx = (
        db.query(WalletDeposit)
        .filter(
            WalletDeposit.user_id == w.user_id,
            WalletDeposit.type == "withdraw",
            WalletDeposit.status.in_(["initiated", "hl_withdrawn"]),
        )
        .order_by(WalletDeposit.created_at.desc())
        .first()
    )

    target_chain = pending_tx.target_chain_id if pending_tx else None
    dest_address = (
        pending_tx.destination_address
        if pending_tx and pending_tx.destination_address
        else w.withdraw_address
    )
    is_cross_chain = target_chain is not None and target_chain != 42161

    if is_cross_chain:
        logger.info(
            f"[{w.address[:10]}...] [WITHDRAW] {balance:.2f} USDC "
            f"→ Stargate bridge to chain {target_chain} ({dest_address[:10]}...)"
        )
    else:
        logger.info(
            f"[{w.address[:10]}...] [WITHDRAW] {balance:.2f} USDC "
            f"→ direct transfer to {dest_address[:10]}..."
        )

    try:
        if is_cross_chain:
            # ── CROSS-CHAIN: Stargate V2 bridge out ──
            if not ensure_gas(w.address, min_eth=BRIDGE_OUT_MIN_ETH, top_up_eth=BRIDGE_OUT_TOP_UP_ETH):
                logger.error(f"[{w.address[:10]}...] [WITHDRAW] No gas for bridge, will retry")
                return

            tx_hash = stargate_bridge_out(private_key, balance, target_chain, dest_address)

            if pending_tx:
                pending_tx.bridge_tx_hash = tx_hash
                pending_tx.status = "completed"
                pending_tx.bridged_at = datetime.utcnow()

        else:
            # ── ARBITRUM DIRECT: ERC-20 transfer ──
            if not ensure_gas(w.address):
                logger.error(f"[{w.address[:10]}...] [WITHDRAW] No gas, will retry")
                return

            tx_hash = transfer_usdc_to_user(private_key, dest_address, balance)

            if pending_tx:
                pending_tx.arb_tx_hash = tx_hash
                pending_tx.status = "completed"
                pending_tx.bridged_at = datetime.utcnow()

        # Update BalanceSnapshot
        snapshot = db.query(BalanceSnapshot).filter(
            BalanceSnapshot.user_id == w.user_id
        ).first()

        if snapshot:
            snapshot.balance = max(0, snapshot.balance - balance)
            snapshot.available = max(0, snapshot.available - balance)

        # Log BalanceEvent
        event = BalanceEvent(
            user_id=w.user_id,
            event_type="withdraw",
            amount=balance,
            balance_after=snapshot.balance if snapshot else 0,
        )
        db.add(event)

        # Clear flag — resume normal deposit monitoring
        w.withdraw_pending = False
        db.commit()

        route = f"Stargate → chain {target_chain}" if is_cross_chain else "Arb direct"
        logger.info(
            f"[{w.address[:10]}...] [WITHDRAW] Sent {balance:.2f} USDC "
            f"({route}), tx: {tx_hash}"
        )

    except Exception as e:
        logger.error(
            f"[{w.address[:10]}...] [WITHDRAW] Failed: {e}, will retry next cycle"
        )
        # Don't clear withdraw_pending — will retry on next poll


# ═══════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════

def check_and_process():
    db = SessionLocal()
    try:
        wallets = db.query(UserWallet).filter(UserWallet.is_active == True).all()

        for w in wallets:
            try:
                balance = get_usdc_balance(w.address)

                if w.withdraw_pending:
                    if balance >= MIN_WITHDRAW:
                        _handle_withdraw(db, w, balance)
                    else:
                        logger.debug(
                            f"[{w.address[:10]}...] withdraw_pending, "
                            f"waiting for USDC (current: {balance:.2f})"
                        )
                else:
                    if balance >= MIN_DEPOSIT:
                        _handle_deposit(db, w, balance)
                    elif balance > 0 and balance < MIN_DEPOSIT:
                        logger.warning(
                            f"[{w.address[:10]}...] Has {balance:.2f} USDC "
                            f"— below {MIN_DEPOSIT} minimum, skipping"
                        )

            except Exception as e:
                logger.error(f"Error checking {w.address[:10]}...: {e}")

    finally:
        db.close()


def main():
    logger.info(
        f"Deposit monitor started. "
        f"Polling every {POLL_INTERVAL}s, "
        f"min deposit: {MIN_DEPOSIT} USDC, "
        f"min withdraw: {MIN_WITHDRAW} USDC"
    )
    while True:
        try:
            check_and_process()
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()