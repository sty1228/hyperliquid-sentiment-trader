from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import BigInteger, String, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class NetworkEvent(Base):
    """
    Append-only stream of trade lifecycle events used by the network-graph SSE channel.
    Engine inserts a row + pg_notify('network_events', id); the API process LISTENs and
    fans the row's payload to connected SSE clients keyed by user_id.
    """
    __tablename__ = "network_events"
    __table_args__ = (
        Index("ix_network_events_user_id_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
