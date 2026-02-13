from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base

def _utcnow():
    return datetime.now(timezone.utc)

class Follow(Base):
    """User follows/copy-trades a trader."""
    __tablename__ = "follows"
    __table_args__ = (
        UniqueConstraint("user_id", "trader_id", name="uq_user_trader"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    trader_id: Mapped[str] = mapped_column(ForeignKey("traders.id"), nullable=False, index=True)
    is_copy_trading: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Relationships
    user = relationship("User", back_populates="follows")
    trader = relationship("Trader", back_populates="follows")