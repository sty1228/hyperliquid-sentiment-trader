"""
KOL Rewards API — 4 endpoints.
All return real data from DB. Zero/empty when user has no activity yet.

Endpoints:
  GET  /api/kol/rewards           — user's current rewards state
  GET  /api/kol/distributions     — weekly distribution history
  POST /api/kol/share             — log a share event (PnL card / leaderboard)
  POST /api/kol/claim-fee-share   — initiate USDC claim to user's Arb wallet
"""

import logging
import threading
from datetime import date, datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List, Literal

from backend.deps import get_db, get_current_user
from backend.models.rewards import KOLReward, KOLDistribution, ShareEvent, ShareType
from backend.models.wallet import UserWallet

router = APIRouter(prefix="/api/kol", tags=["kol-rewards"])
log = logging.getLogger("kol-rewards")

# ── Phase config ──
BETA_START = date(2026, 2, 28)

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
    reward = db.query(KOLReward).filter(KOLReward.user_id == user_id).first()
    if not reward:
        reward = KOLReward(user_id=user_id)
        db.add(reward)
        db.commit()
        db.refresh(reward)
    return reward


def calculate_current_week(phase: str) -> int:
    """Real week number since beta start (1-indexed), capped at totalWeeks."""
    delta = (date.today() - BETA_START).days
    total = PHASE_CONFIG.get(phase, PHASE_CONFIG["beta"])["totalWeeks"]
    return min(max(delta // 7 + 1, 1), total)


def _do_claim_transfer(user_id: str, amount: float, wallet_address: str):
    """Background thread: transfer USDC from master wallet to user's Arb wallet."""
    try:
        from backend.database import SessionLocal
        from backend.services.wallet_manager import master_transfer_usdc
        master_transfer_usdc(wallet_address, amount)
        log.info(f"Claim transfer ${amount:.2f} → {wallet_address[:10]}… OK")
    except Exception as e:
        log.error(f"Claim transfer failed for {user_id[:8]}…: {e}")
        # Refund claimable on failure
        try:
            db = SessionLocal()
            rw = db.query(KOLReward).filter(KOLReward.user_id == user_id).first()
            if rw:
                rw.claimable_fee_share += amount
                db.commit()
            db.close()
        except Exception as e2:
            log.error(f"Refund failed: {e2}")


# ── Endpoints ──

@router.get("/rewards", response_model=RewardsResponse)
async def get_rewards(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
    event = ShareEvent(
        user_id=user.id,
        share_type=ShareType(body.type),
        target_platform=body.targetPlatform,
        reference_id=body.referenceId,
    )
    db.add(event)
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

    # Find user's external wallet for payout
    wallet = (
        db.query(UserWallet)
        .filter(UserWallet.user_id == user.id, UserWallet.is_active.is_(True))
        .first()
    )
    if not wallet or not wallet.withdraw_address:
        return ClaimResponse(
            status="error",
            amount=0,
            message="No withdraw address found. Please set up your wallet first.",
        )

    # Deduct immediately (refunded on failure in background thread)
    reward.claimable_fee_share -= amount
    db.commit()

    # Transfer in background (same pattern as withdraw flow)
    threading.Thread(
        target=_do_claim_transfer,
        args=(user.id, amount, wallet.withdraw_address),
        daemon=True,
    ).start()

    return ClaimResponse(
        status="processing",
        amount=amount,
        message=f"Claiming ${amount:.2f} USDC. Will arrive in your wallet within ~5 minutes.",
    )