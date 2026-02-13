from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, Integer, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base

def _utcnow():
    return datetime.now(timezone.utc)

class Trader(Base):
    """KOL / Twitter trader we track."""
    __tablename__ = "traders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bio: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    followers_count: Mapped[int] = mapped_column(Integer, default=0)
    following_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # Relationships
    stats = relationship("TraderStats", back_populates="trader", cascade="all, delete-orphan")
    signals = relationship("Signal", back_populates="trader", cascade="all, delete-orphan")
    follows = relationship("Follow", back_populates="trader", cascade="all, delete-orphan")


class TraderStats(Base):
    """Pre-computed leaderboard stats per time window."""
    __tablename__ = "trader_stats"
    __table_args__ = (
        UniqueConstraint("trader_id", "window", name="uq_trader_window"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    trader_id: Mapped[str] = mapped_column(ForeignKey("traders.id"), nullable=False, index=True)
    window: Mapped[str] = mapped_column(String(10), nullable=False)  # '24h','7d','30d'

    total_signals: Mapped[int] = mapped_column(Integer, default=0)
    win_count: Mapped[int] = mapped_column(Integer, default=0)
    loss_count: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_return_pct: Mapped[float] = mapped_column(Float, default=0.0)
    total_profit_usd: Mapped[float] = mapped_column(Float, default=0.0)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    points: Mapped[float] = mapped_column(Float, default=0.0)
    profit_grade: Mapped[str | None] = mapped_column(String(5), nullable=True)  # S+, S, A, B, C
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    copiers_count: Mapped[int] = mapped_column(Integer, default=0)
    signal_to_noise: Mapped[float] = mapped_column(Float, default=0.0)

    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Relationships
    trader = relationship("Trader", back_populates="stats")