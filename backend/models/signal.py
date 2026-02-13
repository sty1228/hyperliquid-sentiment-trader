
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Float, Integer, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base

def _utcnow():
    return datetime.now(timezone.utc)

class Signal(Base):
    """A trading signal (derived from a tweet)."""
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    trader_id: Mapped[str] = mapped_column(ForeignKey("traders.id"), nullable=False, index=True)
    tweet_id: Mapped[str | None] = mapped_column(String(30), nullable=True, unique=True)

    tweet_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)      # 'long' | 'short'
    sentiment: Mapped[str] = mapped_column(String(10), nullable=False)      # 'bullish' | 'bearish' | 'neutral'

    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_change: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="active")
    likes: Mapped[int] = mapped_column(Integer, default=0)
    retweets: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    tweet_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # ← NEW

    tweet_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Relationships
    trader = relationship("Trader", back_populates="signals")
    trades = relationship("Trade", back_populates="signal")
