import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Float, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from backend.database import Base


class UserWallet(Base):
    __tablename__ = "user_wallets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(36), ForeignKey("users.id"), unique=True, nullable=False)
    address = Column(String, unique=True, nullable=False, index=True)
    encrypted_private_key = Column(String, nullable=False)
    withdraw_address = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

    user = relationship("User", backref="dedicated_wallet")


class WalletDeposit(Base):
    __tablename__ = "wallet_deposits"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    wallet_address = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    arb_tx_hash = Column(String, nullable=True)
    bridge_tx_hash = Column(String, nullable=True)
    status = Column(String, default="detected")
    created_at = Column(DateTime, default=datetime.utcnow)
    bridged_at = Column(DateTime, nullable=True)