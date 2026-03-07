"""
KOL Rewards API — 4 endpoints.
All return real data from DB. Zero/empty when user has no activity yet.

Endpoints:
  GET  /api/kol/rewards           — user's current rewards state
  GET  /api/kol/distributions     — weekly distribution history
  POST /api/kol/share             — log a share event (PnL card / leaderboard)
  POST /api/kol/claim-fee-share   — initiate USDC claim to user's Arb wallet

★ 2026-03-07: referral boost
  - Users who signed up via a referral code get a permanent 1.15x points multiplier
  - Applied to every points-earning event via _apply_referral_boost()
  - Affiliate revenue share: 20% of referred users' fees credited to referrer
"""

import logging
import threading
from datetime import date, datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional, List, Literal

from backend.deps import get_db, get_current_user
from backend.models.rewards import KOLReward, KOLDistribution, ShareEvent, ShareType
from backend.models.wallet import UserWallet
from backend.models.trade import Trade
from backend.models.follow import Follow
from backend.models.signal import Signal

router = APIRouter(prefix="/api/kol", tags=["kol-rewards"])
log = logging.getLogger("kol-rewards")

# ── Phase config ──────────────────────────────────────────────
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

# ★ Referral boost constants (must match referral_api.py)
REFERRAL_POINTS_BOOST   = 1.15   # +15% permanent multiplier for referred users
AFFILIATE_REVENUE_SHARE = 0.20   # 20% of referred users' fees go to referrer


# ── Schemas ───────────────────────────────────────────────────

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
    # ★ referral extras
    referralBoostActive: bool
    freeTradesRemaining: int
    affiliateEarned: float


class DistributionBreakdown(BaseModel):
    copyVolumePoints: int
    ownTradingPoints: int
    signalQualityBonus: int
    xAccountBoost: float
    smartFollowerBoost: float
    feeShareEarned: float
    referralBoost: float   # ★ multiplier applied this week


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
    amount: Optional[float] = None   # None = claim all


class ClaimResponse(BaseModel):
    status: str   # "processing" | "insufficient" | "error"
    amount: float
    message: str


# ── Helpers ───────────────────────────────────────────────────

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


def _is_referred(db: Session, user_id: str) -> bool:
    """True if this user signed up using a referral code."""
    from backend.models.user import User
    user = db.query(User).filter(User.id == user_id).first()
    return bool(user and getattr(user, "referral_code_used", None))


def _apply_referral_boost(db: Session, user_id: str, base_points: float) -> float:
    """
    ★ Apply the permanent +15% referral multiplier if the user was referred.
    Returns boosted points (float — caller should round to int).
    """
    if _is_referred(db, user_id):
        return base_points * REFERRAL_POINTS_BOOST
    return base_points


def _get_free_trades_remaining(db: Session, user_id: str) -> int:
    """How many fee-free copy trades this user still has left."""
    from backend.models.user import User
    FREE_LIMIT = 10
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not getattr(user, "referral_code_used", None):
        return 0
    used = getattr(user, "free_copy_trades_used", 0) or 0
    return max(0, FREE_LIMIT - used)


def _get_affiliate_earned(db: Session, user_id: str) -> float:
    """
    ★ How much USDC this user has earned via affiliate revenue share.
    = 20% of all fees generated by users they referred.
    """
    from backend.models.referral import Referral, ReferralUse
    ref = db.query(Referral).filter(Referral.user_id == user_id).first()
    if not ref:
        return 0.0
    referred = db.query(ReferralUse).filter(
        ReferralUse.referrer_user_id == user_id
    ).all()
    if not referred:
        return 0.0
    referred_ids = [r.referred_user_id for r in referred]
    total_fees = db.query(func.sum(Trade.fee_usd)).filter(
        Trade.user_id.in_(referred_ids)
    ).scalar() or 0.0
    return round(float(total_fees) * AFFILIATE_REVENUE_SHARE, 4)


def _accrue_affiliate_revenue(db: Session, user_id: str, fee_usd: float):
    """
    Called after a trade is placed by a referred user.
    Credits 20% of the fee to their referrer's claimable_fee_share.
    """
    if fee_usd <= 0:
        return
    from backend.models.referral import ReferralUse
    use = db.query(ReferralUse).filter(
        ReferralUse.referred_user_id == user_id
    ).first()
    if not use:
        return
    referrer_reward = get_or_create_reward(db, use.referrer_user_id)
    share = round(fee_usd * AFFILIATE_REVENUE_SHARE, 6)
    referrer_reward.total_fee_share      = (referrer_reward.total_fee_share or 0) + share
    referrer_reward.claimable_fee_share  = (referrer_reward.claimable_fee_share or 0) + share
    log.debug(
        f"Affiliate +${share:.6f} → referrer {use.referrer_user_id[:8]}… "
        f"(from user {user_id[:8]}…)"
    )


# ── Points calculation ────────────────────────────────────────

def _compute_weekly_points(db: Session, user_id: str, week_start: datetime) -> dict:
    """
    Compute points breakdown for a given week.
    ★ Applies referral boost after summing base points.

    Point sources:
      - Copy Volume  : 1 pt per $10 copy-traded (fees paid by their copiers)
      - Own Trading  : 1 pt per $5 copy/counter-traded by the user themselves
      - Signal Quality: bonus from signal win-rate this week
      - X Account    : 1.2x boost if linked
      - Smart Followers: 0.1x per smart follower (capped 5x)
    """
    week_end = week_start + timedelta(days=7)

    # Copy volume (as a KOL — fees generated by people copying this user)
    copy_fees = db.query(func.sum(Trade.fee_usd)).filter(
        Trade.trader_username == (
            db.query(Trade.trader_username)
            .filter(Trade.user_id == user_id)
            .limit(1)
            .scalar()
        ),
        Trade.opened_at >= week_start,
        Trade.opened_at < week_end,
    ).scalar() or 0.0
    copy_volume_pts = int(copy_fees / 0.10)   # $0.10 fee ≈ 1 pt

    # Own trading (as a copier/counter trader)
    own_size = db.query(func.sum(Trade.size_usd)).filter(
        Trade.user_id == user_id,
        Trade.source.in_(["copy", "counter"]),
        Trade.opened_at >= week_start,
        Trade.opened_at < week_end,
    ).scalar() or 0.0
    own_trading_pts = int(float(own_size) / 5.0)

    base_pts = copy_volume_pts + own_trading_pts

    # Signal quality bonus (win-rate this week for KOL signals)
    week_sigs = db.query(Signal).filter(
        Signal.status.in_(["processed", "expired"]),
        Signal.created_at >= week_start,
        Signal.created_at < week_end,
        Signal.pct_change.isnot(None),
    ).all()
    # match signals to this user's trader record
    from backend.models.trader import Trader
    trader = db.query(Trader).join(
        Trade, Trade.trader_username == Trader.username
    ).filter(Trade.user_id == user_id).first()
    if trader:
        my_sigs = [s for s in week_sigs if s.trader_id == trader.id]
        if my_sigs:
            wins = sum(1 for s in my_sigs if (s.pct_change or 0) > 0)
            wr   = wins / len(my_sigs)
            signal_quality_bonus = int(wr * 50)
        else:
            signal_quality_bonus = 0
    else:
        signal_quality_bonus = 0

    # X account boost
    from backend.models.user import User
    u = db.query(User).filter(User.id == user_id).first()
    x_linked    = bool(u and u.twitter_username)
    x_boost     = 1.2 if x_linked else 1.0

    # Smart follower boost (placeholder — real smart-follower logic TBD)
    reward         = get_or_create_reward(db, user_id)
    sf_count       = reward.smart_follower_count or 0
    sf_boost       = min(1.0 + sf_count * 0.1, 5.0)

    # Combine boosts
    total_before_ref = int((base_pts + signal_quality_bonus) * x_boost * sf_boost)

    # ★ Apply referral +15% boost on top of everything
    total_pts        = int(_apply_referral_boost(db, user_id, total_before_ref))
    ref_boost_used   = REFERRAL_POINTS_BOOST if _is_referred(db, user_id) else 1.0

    return {
        "copy_volume_points":   copy_volume_pts,
        "own_trading_points":   own_trading_pts,
        "signal_quality_bonus": signal_quality_bonus,
        "x_account_boost":      x_boost,
        "smart_follower_boost": sf_boost,
        "referral_boost":       ref_boost_used,
        "total_points":         total_pts,
    }


# ── Background helpers ────────────────────────────────────────

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
            from backend.database import SessionLocal
            db = SessionLocal()
            rw = db.query(KOLReward).filter(KOLReward.user_id == user_id).first()
            if rw:
                rw.claimable_fee_share += amount
                db.commit()
            db.close()
        except Exception as e2:
            log.error(f"Refund failed: {e2}")


# ── Endpoints ─────────────────────────────────────────────────

@router.get("/rewards", response_model=RewardsResponse)
async def get_rewards(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    reward       = get_or_create_reward(db, user.id)
    phase        = reward.current_phase
    config       = PHASE_CONFIG.get(phase, PHASE_CONFIG["beta"])
    current_week = calculate_current_week(phase)

    # ★ Referral extras
    referred            = _is_referred(db, user.id)
    free_trades_left    = _get_free_trades_remaining(db, user.id)
    affiliate_earned    = _get_affiliate_earned(db, user.id)

    # ★ Sync affiliate earned into claimable if new fees accrued
    if affiliate_earned > (reward.total_fee_share or 0):
        delta = affiliate_earned - (reward.total_fee_share or 0)
        reward.total_fee_share     = affiliate_earned
        reward.claimable_fee_share = (reward.claimable_fee_share or 0) + delta
        db.commit()

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
        # ★
        referralBoostActive=referred,
        freeTradesRemaining=free_trades_left,
        affiliateEarned=affiliate_earned,
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
                referralBoost=getattr(r, "referral_boost", 1.0),  # ★
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

    # ★ Sync latest affiliate earnings before claiming
    affiliate_earned = _get_affiliate_earned(db, user.id)
    if affiliate_earned > (reward.total_fee_share or 0):
        delta = affiliate_earned - (reward.total_fee_share or 0)
        reward.total_fee_share     = affiliate_earned
        reward.claimable_fee_share = (reward.claimable_fee_share or 0) + delta
        db.commit()

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
            message=(
                f"Requested ${amount:.2f} but only "
                f"${reward.claimable_fee_share:.2f} is claimable."
            ),
        )

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

    threading.Thread(
        target=_do_claim_transfer,
        args=(user.id, amount, wallet.withdraw_address),
        daemon=True,
    ).start()

    return ClaimResponse(
        status="processing",
        amount=amount,
        message=(
            f"Claiming ${amount:.2f} USDC. "
            f"Will arrive in your wallet within ~5 minutes."
        ),
    )


# ── Called from trading_engine after each trade ───────────────

def on_trade_placed(db: Session, user_id: str, fee_usd: float):
    """
    ★ Hook called by trading_engine after a trade is successfully placed.
    - Accrues affiliate revenue share to the referrer
    - Updates current_week_points for the user
    """
    if fee_usd > 0:
        _accrue_affiliate_revenue(db, user_id, fee_usd)

    # Recompute this week's points snapshot
    now        = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        pts_data = _compute_weekly_points(db, user_id, week_start)
        reward   = get_or_create_reward(db, user_id)
        reward.current_week_points = pts_data["total_points"]
        db.commit()
    except Exception as e:
        log.error(f"on_trade_placed pts update failed for {user_id[:8]}…: {e}")