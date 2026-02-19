from __future__ import annotations
import uuid
from datetime import datetime, timezone, date
from sqlalchemy import String, Float, Integer, Boolean, DateTime, Date, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base

def _utcnow():
    return datetime.now(timezone.utc)

class CopySetting(Base):
    """Copy-trade settings. trader_id=NULL → default settings for all traders."""
    __tablename__ = "copy_settings"
    __table_args__ = (
        UniqueConstraint("user_id", "trader_id", name="uq_user_copy_setting"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    trader_id: Mapped[str | None] = mapped_column(ForeignKey("traders.id"), nullable=True)  # NULL = default

    size_type: Mapped[str] = mapped_column(String(20), default="percent")  # 'fixed_usd' | 'percent'
    size_value: Mapped[float] = mapped_column(Float, default=64.0)
    leverage: Mapped[float] = mapped_column(Float, default=8.0)
    margin_mode: Mapped[str] = mapped_column(String(10), default="cross")  # 'cross' | 'isolated'
    sl_type: Mapped[str] = mapped_column(String(10), default="percent")  # 'percent' | 'fixed_usd'
    sl_value: Mapped[float] = mapped_column(Float, default=169.0)
    tp_type: Mapped[str] = mapped_column(String(10), default="percent")  # 'percent' | 'fixed_usd'
    tp_value: Mapped[float] = mapped_column(Float, default=15.0)
    max_positions: Mapped[int] = mapped_column(Integer, default=10)
    order_type: Mapped[str] = mapped_column(String(10), default="market")  # 'market' | 'limit'
    notifications: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # Relationships
    user = relationship("User", back_populates="copy_settings")


class BalanceSnapshot(Base):
    """Daily equity snapshot for the balance chart."""
    __tablename__ = "balance_snapshots"
    __table_args__ = (
        UniqueConstraint("user_id", "snapshot_date", name="uq_user_snapshot_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    available: Mapped[float] = mapped_column(Float, default=0.0)
    used: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_daily: Mapped[float] = mapped_column(Float, default=0.0)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Relationships
    user = relationship("User", back_populates="balance_snapshots")


class BalanceEvent(Base):
    """Individual deposit/withdraw event with exact timestamp for intraday chart."""
    __tablename__ = "balance_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'deposit' | 'withdraw'
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    balance_after: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Relationships
    user = relationship("User", back_populates="balance_events")