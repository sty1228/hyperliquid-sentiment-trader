"""
Follow model — user ↔ trader relationship
Supports: follow-only, copy trading (same direction), counter trading (opposite direction).
Copy and counter are mutually exclusive.
"""
from __future__ import annotations
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, String, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from backend.database import Base


class Follow(Base):
    __tablename__ = "follows"
    __table_args__ = (
        UniqueConstraint("user_id", "trader_id", name="uq_follow_user_trader"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    trader_id = Column(String(36), ForeignKey("traders.id"), nullable=False, index=True)

    is_copy_trading = Column(Boolean, default=False, nullable=False)
    # ★ NEW — reverse-direction copy trading (mutually exclusive with is_copy_trading)
    is_counter_trading = Column(Boolean, default=False, nullable=False, server_default="false")

    # ★ NEW (2026-04-28) — one-shot "Copy Next" mode
    # copy_mode: "all" = continuous (legacy behavior); "next" = consume remaining_copies then auto-disable
    copy_mode = Column(String(10), default="all", nullable=False, server_default="all")
    remaining_copies = Column(Integer, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # ── Relationships ────────────────────────────────────
    user   = relationship("User",   foreign_keys=[user_id])
    trader = relationship("Trader", foreign_keys=[trader_id])