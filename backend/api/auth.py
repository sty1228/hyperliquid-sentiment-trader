"""
认证 API — 钱包连接 + JWT

★ 2026-03-15: Dual-account merge fix
  connect-wallet now looks up by twitter_username FIRST, then wallet_address.
  If a user with the same twitter_username exists, update their wallet_address
  instead of creating a duplicate account.

★ 2026-05-02: Resilient to uq_users_twitter_username partial unique index.
  Manual SQL on prod added:
    CREATE UNIQUE INDEX uq_users_twitter_username
      ON users (twitter_username) WHERE twitter_username IS NOT NULL;
  Two commit sites can now raise IntegrityError on a twitter_username
  collision: (a) Step 2's UPDATE that attaches a twitter_username to an
  existing user matched by wallet_address, and (b) Step 3's INSERT in a
  race between two near-simultaneous connect-wallet calls. Both are caught
  and resolved by issuing the JWT for the canonical (oldest active) user
  with that twitter_username — no data mutation on collision.
"""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import jwt  # pyjwt
import logging
import uuid

from backend.config import get_settings
from backend.deps import get_db, get_current_user
from backend.models.user import User

settings = get_settings()
router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger("auth")


# ── Request / Response 模型 ──────────────────────────────

class ConnectWalletRequest(BaseModel):
    wallet_address: str
    twitter_username: str | None = None

    @field_validator("wallet_address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError("Invalid wallet address format")
        return v.lower()


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class MeResponse(BaseModel):
    id: str
    wallet_address: str
    display_name: str | None
    twitter_username: str | None = None
    is_active: bool
    created_at: datetime


class SubAccountBody(BaseModel):
    sub_account_address: str

    @field_validator("sub_account_address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError("Invalid sub-account address format")
        return v.lower()


# ── 工具函数 ─────────────────────────────────────────────

def create_jwt_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGO)


def _is_twitter_username_conflict(err: IntegrityError) -> bool:
    """
    Distinguish the partial unique index `uq_users_twitter_username` from the
    other UNIQUE constraints on `users` (wallet_address, sub_account_address).
    psycopg2 surfaces the constraint name in `err.orig.diag.constraint_name`
    on most failures; we check both the constraint name and a fallback string
    match to stay robust across psycopg2 versions and Postgres minor releases.
    """
    diag_name = getattr(getattr(err.orig, "diag", None), "constraint_name", None) or ""
    msg = str(err.orig).lower()
    return (
        diag_name == "uq_users_twitter_username"
        or "uq_users_twitter_username" in msg
        or ("twitter_username" in msg and "duplicate" in msg)
    )


def _resolve_canonical_by_twitter(db: Session, twitter_username: str) -> User | None:
    """
    On twitter_username collision, the oldest active user wins as canonical.
    Older `created_at` reflects "this user was here first". Inactive rows
    (deactivated orphans from prior merges) are excluded — never auth as a
    deactivated account.
    """
    return (
        db.query(User)
        .filter(
            User.twitter_username == twitter_username,
            User.is_active.is_(True),
        )
        .order_by(User.created_at.asc())
        .first()
    )


# ── API 端点 ─────────────────────────────────────────────

@router.post("/connect-wallet", response_model=AuthResponse)
def connect_wallet(body: ConnectWalletRequest, db: Session = Depends(get_db)):
    """
    ★ Dual-account merge logic (priority order):
      1. If twitter_username provided → find existing user by twitter_username
         - If found, update wallet_address on that user (merge)
      2. Otherwise → find by wallet_address (original behavior)
      3. If neither found → create new user
    """
    user = None

    # ── Step 1: Look up by twitter_username (highest priority) ──
    if body.twitter_username:
        user = (
            db.query(User)
            .filter(User.twitter_username == body.twitter_username)
            .first()
        )
        if user and user.wallet_address != body.wallet_address:
            log.info(
                f"🔗 MERGE: twitter_username={body.twitter_username} "
                f"updating wallet {user.wallet_address} → {body.wallet_address}"
            )
            # Check if the NEW wallet_address belongs to a DIFFERENT user
            other = (
                db.query(User)
                .filter(
                    User.wallet_address == body.wallet_address,
                    User.id != user.id,
                )
                .first()
            )
            if other:
                # Another user row owns this wallet but has no twitter
                # or a different twitter — deactivate it to prevent conflicts
                log.warning(
                    f"⚠️ DEACTIVATE orphan user {other.id[:8]}… "
                    f"(wallet {other.wallet_address}, twitter={other.twitter_username}) "
                    f"— wallet now belongs to {user.id[:8]}…"
                )
                other.is_active = False
                # ★ 2026-05-01 — replace the prior 96-byte marker
                # ("merged-into-<new_user_id>-<old_wallet>") with a fixed-shape
                # 38-byte token. Preserves the UNIQUE constraint and fits
                # comfortably even in the legacy VARCHAR(42); the merge target
                # is now reconstructable from is_active=False + log line, not
                # from the marker itself.
                other.wallet_address = f"deact_{uuid.uuid4().hex}"

            user.wallet_address = body.wallet_address
            db.commit()
            db.refresh(user)

    # ── Step 2: Fall back to wallet_address lookup ──
    if not user:
        user = (
            db.query(User)
            .filter(User.wallet_address == body.wallet_address)
            .first()
        )
        if user and body.twitter_username and user.twitter_username != body.twitter_username:
            user.twitter_username = body.twitter_username
            try:
                db.commit()
                db.refresh(user)
            except IntegrityError as e:
                # Another user already owns this twitter_username (partial
                # unique index). Roll back the rename, resolve canonical,
                # auth as them. The wallet-matched user keeps their existing
                # twitter_username (or NULL); we don't reassign.
                db.rollback()
                if not _is_twitter_username_conflict(e):
                    raise
                canonical = _resolve_canonical_by_twitter(db, body.twitter_username)
                if not canonical:
                    raise HTTPException(
                        409,
                        f"twitter_username={body.twitter_username} is in use but its "
                        "owner is inactive — contact support",
                    )
                log.warning(
                    f"⚠️ TWITTER_CONFLICT (Step 2): wallet={body.wallet_address[:10]}… "
                    f"matched user={user.id[:8]}… (twitter={user.twitter_username}); "
                    f"requested twitter={body.twitter_username} already owned by "
                    f"canonical={canonical.id[:8]}… (wallet={canonical.wallet_address[:10]}…) "
                    f"— issuing JWT for canonical, no data change"
                )
                user = canonical

    # ── Step 3: Create new user ──
    if not user:
        user = User(
            wallet_address=body.wallet_address,
            twitter_username=body.twitter_username,
        )
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
        except IntegrityError as e:
            # Race: between Step 1's lookup and the INSERT, another request
            # inserted a user with the same twitter_username. Roll back, find
            # the canonical, auth as them.
            db.rollback()
            if not _is_twitter_username_conflict(e):
                raise
            if not body.twitter_username:
                # Cannot have hit twitter_username conflict without one being set.
                raise
            canonical = _resolve_canonical_by_twitter(db, body.twitter_username)
            if not canonical:
                raise HTTPException(
                    409,
                    f"twitter_username={body.twitter_username} is in use but its "
                    "owner is inactive — contact support",
                )
            log.warning(
                f"⚠️ TWITTER_CONFLICT (Step 3, race): attempted insert "
                f"wallet={body.wallet_address[:10]}… twitter={body.twitter_username}; "
                f"already owned by canonical={canonical.id[:8]}… "
                f"(wallet={canonical.wallet_address[:10]}…) — issuing JWT for canonical, "
                f"requested wallet stays external"
            )
            user = canonical

    token = create_jwt_token(user.id)

    return AuthResponse(
        access_token=token,
        user={
            "id": user.id,
            "wallet_address": user.wallet_address,
            "display_name": user.display_name,
            "twitter_username": user.twitter_username,
        },
    )


# ── NOTE: single /me endpoint (was duplicated before) ────

@router.get("/me", response_model=MeResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return MeResponse(
        id=current_user.id,
        wallet_address=current_user.wallet_address,
        display_name=current_user.display_name,
        twitter_username=current_user.twitter_username,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
    )


@router.get("/sub-account")
def get_sub_account(
    current_user: User = Depends(get_current_user),
):
    return {"sub_account_address": current_user.sub_account_address}


@router.put("/sub-account")
def save_sub_account(
    body: SubAccountBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_user.sub_account_address = body.sub_account_address
    db.commit()
    db.refresh(current_user)
    return {"sub_account_address": current_user.sub_account_address}


@router.post("/logout")
def logout():
    return {"message": "Logged out successfully"}