"""
One-shot script: create dedicated wallet for user cd83f801.
Run once:
  cd /opt/hypercopy && source venv/bin/activate && python3 scripts/create_wallet_for_cd83f801.py
"""
import sys
import os
sys.path.insert(0, "/opt/hypercopy")

from backend.database import SessionLocal
from backend.models.wallet import UserWallet
from backend.services.wallet_manager import generate_wallet, encrypt_private_key

USER_ID = "cd83f801-ed4f-4f67-8bd7-43621fafee17"

db = SessionLocal()
try:
    existing = db.query(UserWallet).filter(UserWallet.user_id == USER_ID).first()
    if existing:
        print(f"[SKIP] Wallet already exists: {existing.address}")
        sys.exit(0)

    address, private_key = generate_wallet()
    encrypted_key = encrypt_private_key(private_key)

    wallet = UserWallet(
        user_id=USER_ID,
        address=address,
        encrypted_private_key=encrypted_key,
        network="arbitrum",
        is_active=True,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    print(f"[OK] Created dedicated wallet for cd83f801")
    print(f"     address: {wallet.address}")
    print(f"     Next: fund this wallet with USDC via the Deposit flow")
except Exception as e:
    db.rollback()
    print(f"[ERROR] {e}")
    raise
finally:
    db.close()