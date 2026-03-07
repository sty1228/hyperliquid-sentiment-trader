import uuid, random, string
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional

from backend.database import get_db
from backend.deps import get_current_user
from backend.models.referral import Referral, ReferralUse, AffiliateApplication
from backend.models.user import User
from backend.models.trade import Trade

router = APIRouter(prefix="/api", tags=["referral"])

FREE_COPY_TRADES_LIMIT = 10
TOTAL_SLOTS = 1000
FREE_TIER_TOTAL = 100
AFFILIATE_REVENUE_SHARE = 0.20  # 20% of fees earned by referred users


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_code(username: str) -> str:
    base = (username or "")[:7].upper().replace(" ", "").replace("@", "")
    if not base:
        base = "".join(random.choices(string.ascii_uppercase, k=5))
    suffix = "".join(random.choices(string.digits, k=2))
    return f"{base}{suffix}"


def _get_or_create_code(db: Session, user: User) -> str:
    ref = db.query(Referral).filter(Referral.user_id == user.id).first()
    if ref:
        return ref.code
    for _ in range(10):
        code = _make_code(user.twitter_username or user.id[:5])
        if not db.query(Referral).filter(Referral.code == code).first():
            break
    else:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    ref = Referral(id=str(uuid.uuid4()), user_id=user.id, code=code)
    db.add(ref)
    db.commit()
    return code


def get_user_free_trades_remaining(db: Session, user_id: str) -> int:
    """How many free copy trades this user still has."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.referral_code_used:
        return 0
    used = getattr(user, "free_copy_trades_used", 0) or 0
    return max(0, FREE_COPY_TRADES_LIMIT - used)


def consume_free_trade(db: Session, user_id: str) -> bool:
    """Consume one free trade. Returns True if a free trade was consumed."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.referral_code_used:
        return False
    used = getattr(user, "free_copy_trades_used", 0) or 0
    if used >= FREE_COPY_TRADES_LIMIT:
        return False
    user.free_copy_trades_used = used + 1
    db.commit()
    return True


# ── schemas ───────────────────────────────────────────────────────────────────

class InvitedBy(BaseModel):
    username: str
    display_name: str
    avatar_url: Optional[str] = None


class GlobalSlots(BaseModel):
    total_slots: int
    slots_used: int
    free_tier_total: int
    free_tier_full: bool


class ReferralInfoResponse(BaseModel):
    code: str
    link: str
    invited_count: int
    active_count: int
    earned_usd: float
    free_trades_remaining: int
    invited_by: Optional[InvitedBy] = None
    global_slots: GlobalSlots
    affiliate_applied: bool
    affiliate_status: Optional[str] = None


class ApplyCodeRequest(BaseModel):
    code: str


class PublicSlotsResponse(BaseModel):
    total_slots: int
    slots_used: int
    free_tier_total: int
    free_tier_full: bool
    inviter_username: Optional[str] = None
    inviter_display_name: Optional[str] = None
    inviter_avatar_url: Optional[str] = None
    code_valid: bool


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/referral/info", response_model=ReferralInfoResponse)
def get_referral_info(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    code = _get_or_create_code(db, current_user)
    link = f"hypercopy.io/join?ref={code}"

    # Stats for this user's referrals
    my_referrals = db.query(ReferralUse).filter(
        ReferralUse.referrer_user_id == current_user.id
    ).all()
    invited_count = len(my_referrals)
    active_count = sum(1 for r in my_referrals if r.is_active)

    # Earned USD: 20% of fees from referred users' trades
    referred_ids = [r.referred_user_id for r in my_referrals]
    total_fees = 0.0
    if referred_ids:
        result = db.query(func.sum(Trade.fee_usd)).filter(
            Trade.user_id.in_(referred_ids)
        ).scalar()
        total_fees = float(result or 0)
    earned_usd = round(total_fees * AFFILIATE_REVENUE_SHARE, 4)

    # Free trades remaining for this user
    free_trades_remaining = get_user_free_trades_remaining(db, current_user.id)

    # Who invited this user
    invited_by = None
    if current_user.referral_code_used:
        parent_ref = db.query(Referral).filter(
            Referral.code == current_user.referral_code_used
        ).first()
        if parent_ref:
            parent_user = db.query(User).filter(User.id == parent_ref.user_id).first()
            if parent_user:
                invited_by = InvitedBy(
                    username=parent_user.twitter_username or parent_user.id[:8],
                    display_name=parent_user.twitter_username or "Unknown",
                    avatar_url=None,
                )

    # Global slot counts
    slots_used = db.query(ReferralUse).count()
    total_users = db.query(User).count()

    # Affiliate status
    affiliate_app = db.query(AffiliateApplication).filter(
        AffiliateApplication.user_id == current_user.id
    ).first()

    return ReferralInfoResponse(
        code=code,
        link=link,
        invited_count=invited_count,
        active_count=active_count,
        earned_usd=earned_usd,
        free_trades_remaining=free_trades_remaining,
        invited_by=invited_by,
        global_slots=GlobalSlots(
            total_slots=TOTAL_SLOTS,
            slots_used=min(slots_used, TOTAL_SLOTS),
            free_tier_total=FREE_TIER_TOTAL,
            free_tier_full=total_users >= FREE_TIER_TOTAL,
        ),
        affiliate_applied=affiliate_app is not None,
        affiliate_status=affiliate_app.status if affiliate_app else None,
    )


@router.get("/referral/public-slots", response_model=PublicSlotsResponse)
def get_public_slots(code: Optional[str] = None, db: Session = Depends(get_db)):
    """Public endpoint — no auth required. Used by /join landing page."""
    slots_used = db.query(ReferralUse).count()
    total_users = db.query(User).count()

    inviter_username = None
    inviter_display_name = None
    inviter_avatar_url = None
    code_valid = False

    if code:
        ref = db.query(Referral).filter(Referral.code == code.upper().strip()).first()
        if ref:
            code_valid = True
            owner = db.query(User).filter(User.id == ref.user_id).first()
            if owner:
                inviter_username = owner.twitter_username or owner.id[:8]
                inviter_display_name = owner.twitter_username or "Unknown"

    return PublicSlotsResponse(
        total_slots=TOTAL_SLOTS,
        slots_used=min(slots_used, TOTAL_SLOTS),
        free_tier_total=FREE_TIER_TOTAL,
        free_tier_full=total_users >= FREE_TIER_TOTAL,
        inviter_username=inviter_username,
        inviter_display_name=inviter_display_name,
        inviter_avatar_url=inviter_avatar_url,
        code_valid=code_valid,
    )


@router.post("/referral/apply-code")
def apply_referral_code(
    req: ApplyCodeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.referral_code_used:
        raise HTTPException(400, "Referral code already applied")

    ref = db.query(Referral).filter(
        Referral.code == req.code.upper().strip()
    ).first()
    if not ref:
        raise HTTPException(404, "Invalid referral code")
    if ref.user_id == current_user.id:
        raise HTTPException(400, "Cannot use your own referral code")

    existing = db.query(ReferralUse).filter(
        ReferralUse.referred_user_id == current_user.id
    ).first()
    if existing:
        raise HTTPException(400, "Already used a referral code")

    use = ReferralUse(
        id=str(uuid.uuid4()),
        referrer_user_id=ref.user_id,
        referred_user_id=current_user.id,
        code=req.code.upper().strip(),
        is_active=True,
    )
    db.add(use)
    current_user.referral_code_used = req.code.upper().strip()
    db.commit()
    return {"ok": True, "free_trades_granted": FREE_COPY_TRADES_LIMIT}


@router.post("/referral/affiliate-apply")
def affiliate_apply(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = db.query(AffiliateApplication).filter(
        AffiliateApplication.user_id == current_user.id
    ).first()
    if existing:
        return {"ok": True, "status": existing.status}
    app = AffiliateApplication(id=str(uuid.uuid4()), user_id=current_user.id)
    db.add(app)
    db.commit()
    return {"ok": True, "status": "pending"}


@router.get("/referral/validate-code/{code}")
def validate_code(code: str, db: Session = Depends(get_db)):
    """Quick check if a code exists — no auth needed."""
    ref = db.query(Referral).filter(Referral.code == code.upper().strip()).first()
    if not ref:
        raise HTTPException(404, "Invalid code")
    owner = db.query(User).filter(User.id == ref.user_id).first()
    return {
        "valid": True,
        "inviter_username": owner.twitter_username if owner else None,
    }