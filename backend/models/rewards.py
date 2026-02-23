# ================================================================
# FILE: backend/models/rewards.py
# ================================================================
# DB models for the KOL rewards system.
# Tables: kol_rewards, kol_distributions, share_events
#
# After creating this file, run:
#   cd /opt/hypercopy && source venv/bin/activate
#   python -c "from backend.models.rewards import *; from backend.database import engine, Base; Base.metadata.create_all(bind=engine)"
# ================================================================

from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, Text, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.sql import func
from backend.database import Base
import uuid
import enum


class DistributionStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"


class ShareType(str, enum.Enum):
    pnl_card = "pnl_card"
    leaderboard = "leaderboard"


# ── KOL Rewards State (one row per user) ──
# Stores the current aggregated rewards state.
# Updated by: weekly distribution cron, share events, backend recalc.
class KOLReward(Base):
    __tablename__ = "kol_rewards"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), unique=True, nullable=False, index=True)

    # Points
    total_points = Column(Integer, default=0, nullable=False)
    current_week_points = Column(Integer, default=0, nullable=False)
    rank = Column(Integer, nullable=True)  # null = unranked

    # Fee Share (USDC)
    total_fee_share = Column(Float, default=0.0, nullable=False)      # lifetime earned
    claimable_fee_share = Column(Float, default=0.0, nullable=False)  # unclaimed balance

    # Smart Followers & Boost
    smart_follower_count = Column(Integer, default=0, nullable=False)
    boost_multiplier = Column(Float, default=1.0, nullable=False)

    # X Account
    x_account_linked = Column(Boolean, default=False, nullable=False)
    x_account_handle = Column(String(100), nullable=True)

    # Phase (backend controls which phase is active)
    current_phase = Column(String(20), default="beta", nullable=False)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ── Weekly Distribution Record ──
# One row per user per week. Created by weekly distribution cron.
class KOLDistribution(Base):
    __tablename__ = "kol_distributions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), nullable=False, index=True)

    week_number = Column(Integer, nullable=False)
    distribution_date = Column(DateTime(timezone=True), nullable=False)

    # Points breakdown
    total_points = Column(Integer, default=0, nullable=False)
    copy_volume_points = Column(Integer, default=0, nullable=False)
    own_trading_points = Column(Integer, default=0, nullable=False)
    signal_quality_bonus = Column(Integer, default=0, nullable=False)

    # Multipliers applied
    x_account_boost = Column(Float, default=1.0, nullable=False)
    smart_follower_boost = Column(Float, default=1.0, nullable=False)

    # Fee Share
    fee_share_usdc = Column(Float, default=0.0, nullable=False)
    status = Column(SAEnum(DistributionStatus), default=DistributionStatus.paid, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Composite unique: one distribution per user per week
    __table_args__ = (
        # SQLAlchemy UniqueConstraint
        {"sqlite_autoincrement": True},
    )


# ── Share Events (PnL card / Leaderboard shares to X) ──
# Logged by POST /api/kol/share. Used for multiplier calculation.
class ShareEvent(Base):
    __tablename__ = "share_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), nullable=False, index=True)

    share_type = Column(SAEnum(ShareType), nullable=False)  # pnl_card | leaderboard
    target_platform = Column(String(20), default="x", nullable=False)
    reference_id = Column(String(100), nullable=True)  # trade_id or leaderboard snapshot id

    created_at = Column(DateTime(timezone=True), server_default=func.now())