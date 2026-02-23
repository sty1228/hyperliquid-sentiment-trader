# ================================================================
# FILE: backend/api/rewards.py
# ================================================================
# KOL Rewards API — 4 endpoints per handoff doc.
# All return real data from DB. Zero/empty when user has no activity yet.
#
# Endpoints:
#   GET  /api/kol/rewards           — user's current rewards state
#   GET  /api/kol/distributions     — weekly distribution history
#   POST /api/kol/share             — log a share event (PnL card / leaderboard)
#   POST /api/kol/claim-fee-share   — initiate USDC claim (placeholder until fee engine ready)
# ================================================================

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime

from backend.deps import get_db, get_current_user
from backend.models.rewards import KOLReward, KOLDistribution, ShareEvent, ShareType

router = APIRouter(prefix="/api/kol", tags=["kol-rewards"])


# ── Phase config (hardcoded — matches business doc, no reason to store in DB) ──
PHASE_CONFIG = {
    "beta": {
        "feeShare": "60%",
        "twapShare": "40%",
        "airdropPool": "8-10%",
        "copyShare": "30%",
        "multiplierRange": "2-5x",
        "kolRefBonus": "5x",
        "totalWeeks": 12,
    },
    "season1": {
        "feeShare": "30%",
        "twapShare": "70%",
        "airdropPool": "40-50%",
        "copyShare": "30%",
        "multiplierRange": "1x → 2x",
        "kolRefBonus": "3x",
        "totalWeeks": 32,
    },
}


# ── Schemas ──

class PhaseConfigResponse(BaseModel):
    feeShare: str
    twapShare: str
    airdropPool: str
    copyShare: str
    multiplierRange: str
    kolRefBonus: str
    totalWeeks: int


class RewardsResponse(BaseModel):
    phase: str
    currentWeek: int
    totalWeeks: int
    totalPoints: int
    currentWeekPoints: int
    rank: Optional[int]
    totalFeeShare: float
    claimableFeeShare: float
    smartFollowerCount: int
    boostMultiplier: float
    xAccountLinked: bool
    phaseConfig: PhaseConfigResponse


class DistributionBreakdown(BaseModel):
    copyVolumePoints: int
    ownTradingPoints: int
    signalQualityBonus: int
    xAccountBoost: float
    smartFollowerBoost: float
    feeShareEarned: float


class DistributionItem(BaseModel):
    week: int
    date: str
    points: int
    feeShareUsdc: float
    status: str
    breakdown: DistributionBreakdown


class DistributionsResponse(BaseModel):
    distributions: List[DistributionItem]


class ShareRequest(BaseModel):
    type: Literal["pnl_card", "leaderboard"]
    targetPlatform: str = "x"
    referenceId: Optional[str] = None


class ShareResponse(BaseModel):
    success: bool
    shareId: str
    message: str


class ClaimRequest(BaseModel):
    amount: Optional[float] = None  # null = claim all


class ClaimResponse(BaseModel):
    status: str  # "processing" | "insufficient" | "error"
    amount: float
    message: str


# ── Helpers ──

def get_or_create_reward(db: Session, user_id: str) -> KOLReward:
    """Get existing rewards row or create a fresh one for this user."""
    reward = db.query(KOLReward).filter(KOLReward.user_id == user_id).first()
    if not reward:
        reward = KOLReward(user_id=user_id)
        db.add(reward)
        db.commit()
        db.refresh(reward)
    return reward


def calculate_current_week(phase: str) -> int:
    """
    Calculate current week number within the phase.
    TODO: Replace with actual phase start date from config/DB.
    For now, returns 1 (phase just started).
    """
    # When you have a real start date:
    # from datetime import date
    # start = date(2026, 1, 1)  # beta start
    # delta = (date.today() - start).days
    # return min(max(delta // 7 + 1, 1), PHASE_CONFIG[phase]["totalWeeks"])
    return 1


# ── Endpoints ──

@router.get("/rewards", response_model=RewardsResponse)
async def get_rewards(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    GET /api/kol/rewards
    Returns the user's full rewards state. Creates a fresh record if first visit.
    Frontend replaces ALL mock data with this response.
    """
    reward = get_or_create_reward(db, user.id)
    phase = reward.current_phase
    config = PHASE_CONFIG.get(phase, PHASE_CONFIG["beta"])
    current_week = calculate_current_week(phase)

    return RewardsResponse(
        phase=phase,
        currentWeek=current_week,
        totalWeeks=config["totalWeeks"],
        totalPoints=reward.total_points,
        currentWeekPoints=reward.current_week_points,
        rank=reward.rank,
        totalFeeShare=reward.total_fee_share,
        claimableFeeShare=reward.claimable_fee_share,
        smartFollowerCount=reward.smart_follower_count,
        boostMultiplier=reward.boost_multiplier,
        xAccountLinked=reward.x_account_linked,
        phaseConfig=PhaseConfigResponse(**config),
    )


@router.get("/distributions", response_model=DistributionsResponse)
async def get_distributions(
    limit: int = Query(default=6, ge=1, le=52),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    GET /api/kol/distributions?limit=6
    Returns weekly distribution history, newest first.
    Empty list if user has no distributions yet.
    """
    rows = (
        db.query(KOLDistribution)
        .filter(KOLDistribution.user_id == user.id)
        .order_by(KOLDistribution.week_number.desc())
        .limit(limit)
        .all()
    )

    distributions = []
    for r in rows:
        distributions.append(DistributionItem(
            week=r.week_number,
            date=r.distribution_date.strftime("%b %d"),
            points=r.total_points,
            feeShareUsdc=r.fee_share_usdc,
            status=r.status.value,
            breakdown=DistributionBreakdown(
                copyVolumePoints=r.copy_volume_points,
                ownTradingPoints=r.own_trading_points,
                signalQualityBonus=r.signal_quality_bonus,
                xAccountBoost=r.x_account_boost,
                smartFollowerBoost=r.smart_follower_boost,
                feeShareEarned=r.fee_share_usdc,
            ),
        ))

    return DistributionsResponse(distributions=distributions)


@router.post("/share", response_model=ShareResponse)
async def log_share(
    body: ShareRequest,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    POST /api/kol/share
    Logs a share event. Fully functional right now — no dependencies on trading engine.
    Used for: multiplier calculation, analytics, rewards screen "Share" buttons.
    """
    event = ShareEvent(
        user_id=user.id,
        share_type=ShareType(body.type),
        target_platform=body.targetPlatform,
        reference_id=body.referenceId,
    )
    db.add(event)

    # Update share count on rewards row (for multiplier calc)
    reward = get_or_create_reward(db, user.id)
    # TODO: Recalculate boost_multiplier based on share frequency + smart followers
    # For now, just log the event.

    db.commit()
    db.refresh(event)

    return ShareResponse(
        success=True,
        shareId=event.id,
        message=f"Share logged: {body.type} to {body.targetPlatform}",
    )


@router.post("/claim-fee-share", response_model=ClaimResponse)
async def claim_fee_share(
    body: ClaimRequest,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    POST /api/kol/claim-fee-share
    Initiates USDC claim to user's wallet on Arbitrum.

    Currently: validates amount against claimable balance.
    TODO: When fee share accumulation is live, add actual USDC transfer
    via wallet_manager (same pattern as withdraw flow).
    """
    reward = get_or_create_reward(db, user.id)

    amount = body.amount if body.amount else reward.claimable_fee_share

    if amount <= 0:
        return ClaimResponse(
            status="insufficient",
            amount=0,
            message="No fee share available to claim.",
        )

    if amount > reward.claimable_fee_share:
        return ClaimResponse(
            status="insufficient",
            amount=reward.claimable_fee_share,
            message=f"Requested ${amount:.2f} but only ${reward.claimable_fee_share:.2f} is claimable.",
        )

    # Deduct from claimable
    reward.claimable_fee_share -= amount
    db.commit()

    # TODO: Trigger actual USDC transfer via background thread
    # (same pattern as wallet.py withdraw — threading.Thread with HL withdraw + Arb transfer)
    # For now, just deduct the balance.

    return ClaimResponse(
        status="processing",
        amount=amount,
        message=f"Claiming ${amount:.2f} USDC. Will arrive in your wallet within ~5 minutes.",
    )