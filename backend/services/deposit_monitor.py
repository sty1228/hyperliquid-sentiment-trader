"""
Deposit Monitor — watches dedicated wallets for USDC deposits and auto-bridges to HL.

Run as: python -m backend.services.deposit_monitor
Or via systemd: systemctl start hypercopy-monitor
"""

import time
import logging
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()  # Load .env before anything else

from backend.database import SessionLocal
from backend.models.wallet import UserWallet, WalletDeposit
from backend.models.setting import BalanceSnapshot, BalanceEvent
from backend.services.wallet_manager import (
    get_usdc_balance, bridge_usdc_to_hl, decrypt_key, ensure_gas,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

MIN_DEPOSIT = 5.0   # USDC — below this HL eats it
POLL_INTERVAL = 15   # seconds


def check_and_bridge():
    db = SessionLocal()
    try:
        wallets = db.query(UserWallet).filter(UserWallet.is_active == True).all()

        for w in wallets:
            try:
                balance = get_usdc_balance(w.address)

                if balance >= MIN_DEPOSIT:
                    logger.info(f"[{w.address[:10]}...] Found {balance:.2f} USDC, bridging...")

                    private_key = decrypt_key(w.encrypted_private_key)

                    # Record deposit
                    deposit = WalletDeposit(
                        user_id=w.user_id,
                        wallet_address=w.address,
                        amount=balance,
                        status="bridging",
                    )
                    db.add(deposit)
                    db.commit()

                    try:
                        # Ensure wallet has ETH for gas
                        if not ensure_gas(w.address):
                            deposit.status = "failed"
                            db.commit()
                            logger.error(f"[{w.address[:10]}...] No gas, skipping bridge")
                            continue

                        tx_hash = bridge_usdc_to_hl(private_key, balance)

                        deposit.bridge_tx_hash = tx_hash
                        deposit.status = "bridged"
                        deposit.bridged_at = datetime.utcnow()

                        # Update BalanceSnapshot
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

                        # Log BalanceEvent
                        event = BalanceEvent(
                            user_id=w.user_id,
                            event_type="deposit",
                            amount=balance,
                            balance_after=snapshot.balance,
                        )
                        db.add(event)
                        db.commit()

                        logger.info(f"[{w.address[:10]}...] Bridged {balance:.2f} USDC, tx: {tx_hash}")

                    except Exception as e:
                        deposit.status = "failed"
                        db.commit()
                        logger.error(f"[{w.address[:10]}...] Bridge failed: {e}")

                elif balance > 0 and balance < MIN_DEPOSIT:
                    logger.warning(
                        f"[{w.address[:10]}...] Has {balance:.2f} USDC — below {MIN_DEPOSIT} minimum, skipping"
                    )

            except Exception as e:
                logger.error(f"Error checking {w.address[:10]}...: {e}")

    finally:
        db.close()


def main():
    logger.info(f"Deposit monitor started. Polling every {POLL_INTERVAL}s, min deposit: {MIN_DEPOSIT} USDC")
    while True:
        try:
            check_and_bridge()
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()