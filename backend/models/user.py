import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    wallet_address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    twitter_username: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    sub_account_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # ★ Referral
    referral_code_used: Mapped[str | None] = mapped_column(String(20), nullable=True)
    free_copy_trades_used: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    follows = relationship("Follow", back_populates="user", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="user", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="user", cascade="all, delete-orphan")
    copy_settings = relationship("CopySetting", back_populates="user", cascade="all, delete-orphan")
    balance_snapshots = relationship("BalanceSnapshot", back_populates="user", cascade="all, delete-orphan")
    balance_events = relationship("BalanceEvent", back_populates="user")