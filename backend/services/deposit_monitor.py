"""
Deposit Monitor — watches dedicated wallets for USDC and handles:
  1. DEPOSITS:  wallet(withdraw_pending=False) + USDC on Arb → bridge to HL
  2. WITHDRAWALS: wallet(withdraw_pending=True) →
       a) HL internal transfer to master wallet (zero fee, instant)
       b) Master wallet Arb USDC → user's external wallet
       c) If master Arb USDC insufficient → fallback to withdraw_from_bridge ($1 fee)

Run as: python -m backend.services.deposit_monitor
Or via systemd: systemctl start hypercopy-monitor
"""

import time
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from backend.database import SessionLocal
from backend.models.wallet import UserWallet, WalletDeposit
from backend.models.setting import BalanceSnapshot, BalanceEvent
from backend.services.wallet_manager import (
    get_usdc_balance, bridge_usdc_to_hl, decrypt_key,
    ensure_gas, transfer_usdc_to_user, stargate_bridge_out,
    get_hl_balance, hl_internal_transfer,
    get_master_arb_usdc_balance, master_transfer_usdc,
    withdraw_from_hl,
    MASTER_WALLET_ADDRESS, MASTER_WALLET_KEY,
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

# Master wallet low balance warning threshold
MASTER_LOW_BALANCE_WARN = 10.0


def _now():
    return datetime.now(timezone.utc)


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
        deposit.bridged_at = _now()

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
                snapshot_date=_now().date(),
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
# WITHDRAW FLOW — zero-fee via HL internal transfer
#
# Priority path (zero fee):
#   1. dedicated wallet HL → usd_transfer → master wallet HL (free)
#   2. master wallet Arb USDC → ERC-20 transfer → user (or Stargate)
#
# Fallback path ($1 fee):
#   If master Arb USDC is too low, use withdraw_from_bridge
#
# Legacy path (Arb balance):
#   If USDC already sitting on Arb (from prior HL withdraw),
#   just transfer directly from dedicated wallet
# ═══════════════════════════════════════════════════════

def _handle_withdraw(db, w, arb_balance):
    """Withdraw: try zero-fee path first, fallback to bridge."""

    private_key = decrypt_key(w.encrypted_private_key)

    # Find the pending withdraw record
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
    withdraw_amount = pending_tx.amount if pending_tx else arb_balance

    try:
        # Check dedicated wallet HL balance
        hl_state = get_hl_balance(w.address)
        hl_available = hl_state.get("withdrawable", 0)

        # ────────────────────────────────────────────
        # PATH A: USDC on HL → zero-fee internal transfer
        # ────────────────────────────────────────────
        if hl_available >= withdraw_amount * 0.95:
            logger.info(
                f"[{w.address[:10]}...] [WITHDRAW] HL balance {hl_available:.2f}, "
                f"using zero-fee internal transfer to master"
            )

            # Step 1: dedicated → master (HL internal, free)
            result = hl_internal_transfer(private_key, withdraw_amount, MASTER_WALLET_ADDRESS)
            status = result.get("status", "")
            if status != "ok":
                logger.error(f"[{w.address[:10]}...] [WITHDRAW] HL internal transfer failed: {result}")
                return

            time.sleep(2)  # small delay for HL to process

            # Step 2: master Arb USDC → user
            master_bal = get_master_arb_usdc_balance()
            logger.info(
                f"[{w.address[:10]}...] [WITHDRAW] Master Arb USDC: {master_bal:.2f}, "
                f"need: {withdraw_amount:.2f}"
            )

            if master_bal >= withdraw_amount:
                tx_hash = _master_pay_user(
                    withdraw_amount, dest_address, target_chain, is_cross_chain
                )
                if pending_tx:
                    if is_cross_chain:
                        pending_tx.bridge_tx_hash = tx_hash
                    else:
                        pending_tx.arb_tx_hash = tx_hash
            else:
                # Master Arb USDC insufficient — fallback: withdraw master HL → Arb
                logger.warning(
                    f"[{w.address[:10]}...] [WITHDRAW] Master Arb USDC too low "
                    f"({master_bal:.2f}), doing withdraw_from_bridge for master ($1 fee)"
                )
                try:
                    withdraw_from_hl(MASTER_WALLET_KEY, withdraw_amount + 1, MASTER_WALLET_ADDRESS)
                    logger.info(
                        f"[{w.address[:10]}...] [WITHDRAW] Master HL withdraw initiated, "
                        f"will complete transfer on next cycle"
                    )
                except Exception as e:
                    logger.error(f"[{w.address[:10]}...] [WITHDRAW] Master fallback failed: {e}")
                # Don't clear withdraw_pending — will retry next cycle
                # when master Arb USDC has arrived
                return

            route = f"zero-fee → master → {'Stargate chain ' + str(target_chain) if is_cross_chain else 'Arb direct'}"

        # ────────────────────────────────────────────
        # PATH B: USDC already on Arb (legacy/retry)
        # ────────────────────────────────────────────
        elif arb_balance >= MIN_WITHDRAW:
            logger.info(
                f"[{w.address[:10]}...] [WITHDRAW] USDC on Arb ({arb_balance:.2f}), "
                f"transferring from dedicated wallet"
            )

            if is_cross_chain:
                if not ensure_gas(w.address, min_eth=BRIDGE_OUT_MIN_ETH, top_up_eth=BRIDGE_OUT_TOP_UP_ETH):
                    logger.error(f"[{w.address[:10]}...] [WITHDRAW] No gas for bridge-out")
                    return
                tx_hash = stargate_bridge_out(private_key, arb_balance, target_chain, dest_address)
                if pending_tx:
                    pending_tx.bridge_tx_hash = tx_hash
            else:
                if not ensure_gas(w.address):
                    logger.error(f"[{w.address[:10]}...] [WITHDRAW] No gas, will retry")
                    return
                tx_hash = transfer_usdc_to_user(private_key, dest_address, arb_balance)
                if pending_tx:
                    pending_tx.arb_tx_hash = tx_hash

            withdraw_amount = arb_balance
            route = f"Arb direct (from dedicated wallet)"

        # ────────────────────────────────────────────
        # PATH C: Nothing available yet — wait
        # ────────────────────────────────────────────
        else:
            logger.debug(
                f"[{w.address[:10]}...] [WITHDRAW] Waiting for USDC "
                f"(HL: {hl_available:.2f}, Arb: {arb_balance:.2f})"
            )
            return

        # ── Update DB ──
        if pending_tx:
            pending_tx.status = "completed"
            pending_tx.bridged_at = _now()

        snapshot = db.query(BalanceSnapshot).filter(
            BalanceSnapshot.user_id == w.user_id
        ).first()

        if snapshot:
            snapshot.balance = max(0, snapshot.balance - withdraw_amount)
            snapshot.available = max(0, snapshot.available - withdraw_amount)

        event = BalanceEvent(
            user_id=w.user_id,
            event_type="withdraw",
            amount=withdraw_amount,
            balance_after=snapshot.balance if snapshot else 0,
        )
        db.add(event)

        w.withdraw_pending = False
        db.commit()

        logger.info(
            f"[{w.address[:10]}...] [WITHDRAW] ✓ Sent {withdraw_amount:.2f} USDC "
            f"to {dest_address[:10]}... ({route}), tx: {tx_hash}"
        )

    except Exception as e:
        logger.error(
            f"[{w.address[:10]}...] [WITHDRAW] Failed: {e}, will retry next cycle"
        )


def _master_pay_user(amount: float, dest_address: str, target_chain, is_cross_chain: bool) -> str:
    """Master wallet pays user on Arb (direct) or via Stargate (cross-chain)."""
    if is_cross_chain:
        ensure_gas(MASTER_WALLET_ADDRESS, min_eth=BRIDGE_OUT_MIN_ETH, top_up_eth=BRIDGE_OUT_TOP_UP_ETH)
        tx_hash = stargate_bridge_out(MASTER_WALLET_KEY, amount, target_chain, dest_address)
        logger.info(f"[MASTER] Stargate bridge-out {amount} USDC → chain {target_chain}, tx: {tx_hash}")
    else:
        # Master wallet should have enough ETH (it's the gas station)
        tx_hash = master_transfer_usdc(dest_address, amount)
        logger.info(f"[MASTER] Transferred {amount} USDC → {dest_address[:10]}..., tx: {tx_hash}")
    return tx_hash


# ═══════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════

def check_and_process():
    db = SessionLocal()
    try:
        wallets = db.query(UserWallet).filter(UserWallet.is_active == True).all()

        for w in wallets:
            try:
                arb_balance = get_usdc_balance(w.address)

                if w.withdraw_pending:
                    # ── WITHDRAW MODE ──
                    # Check both Arb and HL balance to decide path
                    hl_state = get_hl_balance(w.address)
                    hl_available = hl_state.get("withdrawable", 0)

                    if arb_balance >= MIN_WITHDRAW or hl_available >= MIN_WITHDRAW:
                        _handle_withdraw(db, w, arb_balance)
                    else:
                        logger.debug(
                            f"[{w.address[:10]}...] withdraw_pending, "
                            f"waiting (Arb: {arb_balance:.2f}, HL: {hl_available:.2f})"
                        )
                else:
                    # ── DEPOSIT MODE ──
                    if arb_balance >= MIN_DEPOSIT:
                        _handle_deposit(db, w, arb_balance)
                    elif arb_balance > 0 and arb_balance < MIN_DEPOSIT:
                        logger.warning(
                            f"[{w.address[:10]}...] Has {arb_balance:.2f} USDC "
                            f"— below {MIN_DEPOSIT} minimum, skipping"
                        )

            except Exception as e:
                logger.error(f"Error checking {w.address[:10]}...: {e}")

    finally:
        db.close()


def main():
    logger.info(
        f"Deposit monitor started (zero-fee withdraw mode). "
        f"Polling every {POLL_INTERVAL}s, "
        f"min deposit: {MIN_DEPOSIT} USDC, "
        f"min withdraw: {MIN_WITHDRAW} USDC, "
        f"master wallet: {MASTER_WALLET_ADDRESS[:10]}..."
    )

    # Startup check: warn if master Arb USDC is low
    try:
        master_bal = get_master_arb_usdc_balance()
        logger.info(f"Master wallet Arb USDC: {master_bal:.2f}")
        if master_bal < MASTER_LOW_BALANCE_WARN:
            logger.warning(
                f"⚠️  Master wallet Arb USDC is low ({master_bal:.2f}). "
                f"Withdrawals will fall back to $1 fee mode. "
                f"Please fund {MASTER_WALLET_ADDRESS} with USDC on Arbitrum."
            )
    except Exception as e:
        logger.error(f"Failed to check master wallet balance: {e}")

    while True:
        try:
            check_and_process()
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()