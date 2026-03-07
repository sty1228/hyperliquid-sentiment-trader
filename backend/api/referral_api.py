import uuid, random, string
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from backend.database import get_db
from backend.deps import get_current_user
from backend.models.referral import Referral, ReferralUse, AffiliateApplication
from backend.models.user import User

router = APIRouter(prefix="/api", tags=["referral"])

TOTAL_SLOTS = 1000
FREE_TIER_TOTAL = 100

# ── helpers ──────────────────────────────────────────────
def _make_code(username: str) -> str:
    base = (username or "")[:7].upper().replace(" ", "")
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

# ── schemas ──────────────────────────────────────────────
class InvitedBy(BaseModel):
    username: str
    display_name: str
    avatar_url: Optional[str]

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
    invited_by: Optional[InvitedBy]
    global_slots: GlobalSlots
    affiliate_applied: bool

class ApplyCodeRequest(BaseModel):
    code: str

# ── endpoints ────────────────────────────────────────────
@router.get("/referral/info", response_model=ReferralInfoResponse)
def get_referral_info(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    code = _get_or_create_code(db, current_user)
    link = f"hypercopy.io/join?ref={code}"

    invited = db.query(ReferralUse).filter(ReferralUse.referrer_user_id == current_user.id).all()
    invited_count = len(invited)
    active_count = sum(1 for r in invited if r.is_active)
    # fees: placeholder — wire up when trade fee tracking is live
    earned_usd = 0.0

    # who invited this user
    invited_by = None
    if current_user.referral_code_used:
        parent_ref = db.query(Referral).filter(Referral.code == current_user.referral_code_used).first()
        if parent_ref:
            parent_user = db.query(User).filter(User.id == parent_ref.user_id).first()
            if parent_user:
                invited_by = InvitedBy(
                    username=parent_user.twitter_username or parent_user.id[:8],
                    display_name=parent_user.twitter_username or "Unknown",
                    avatar_url=None,
                )

    slots_used = db.query(ReferralUse).count()
    total_users = db.query(User).count()
    affiliate_applied = db.query(AffiliateApplication).filter(
        AffiliateApplication.user_id == current_user.id
    ).first() is not None

    return ReferralInfoResponse(
        code=code,
        link=link,
        invited_count=invited_count,
        active_count=active_count,
        earned_usd=earned_usd,
        invited_by=invited_by,
        global_slots=GlobalSlots(
            total_slots=TOTAL_SLOTS,
            slots_used=min(slots_used, TOTAL_SLOTS),
            free_tier_total=FREE_TIER_TOTAL,
            free_tier_full=total_users >= FREE_TIER_TOTAL,
        ),
        affiliate_applied=affiliate_applied,
    )

@router.post("/referral/apply-code")
def apply_referral_code(req: ApplyCodeRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.referral_code_used:
        raise HTTPException(400, "Referral code already applied")
    ref = db.query(Referral).filter(Referral.code == req.code.upper().strip()).first()
    if not ref:
        raise HTTPException(404, "Invalid referral code")
    if ref.user_id == current_user.id:
        raise HTTPException(400, "Cannot use your own referral code")
    existing = db.query(ReferralUse).filter(ReferralUse.referred_user_id == current_user.id).first()
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
    return {"ok": True}

@router.post("/referral/affiliate-apply")
def affiliate_apply(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    existing = db.query(AffiliateApplication).filter(AffiliateApplication.user_id == current_user.id).first()
    if existing:
        return {"ok": True, "status": existing.status}
    app = AffiliateApplication(id=str(uuid.uuid4()), user_id=current_user.id)
    db.add(app)
    db.commit()
    return {"ok": True, "status": "pending"}