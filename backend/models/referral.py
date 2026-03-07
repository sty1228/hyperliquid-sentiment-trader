import uuid
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey
from sqlalchemy.sql import func
from backend.database import Base

class Referral(Base):
    __tablename__ = "referrals"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, unique=True)
    code = Column(String(20), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ReferralUse(Base):
    __tablename__ = "referral_uses"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    referrer_user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    referred_user_id = Column(String(36), ForeignKey("users.id"), nullable=False, unique=True)
    code = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    is_active = Column(Boolean, default=False)

class AffiliateApplication(Base):
    __tablename__ = "affiliate_applications"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, unique=True)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())