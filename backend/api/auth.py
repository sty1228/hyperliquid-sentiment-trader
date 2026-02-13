"""
认证 API — 钱包连接 + JWT
"""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session
import jwt  # pyjwt

from backend.config import get_settings
from backend.deps import get_db, get_current_user
from backend.models.user import User

settings = get_settings()
router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── Request / Response 模型 ──────────────────────────────

class ConnectWalletRequest(BaseModel):
    """前端发来的钱包地址"""
    wallet_address: str

    @field_validator("wallet_address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError("Invalid wallet address format")
        return v.lower()  # 统一小写存储


class AuthResponse(BaseModel):
    """返回给前端的 token + 用户信息"""
    access_token: str
    token_type: str = "bearer"
    user: dict


class MeResponse(BaseModel):
    """当前用户信息"""
    id: str
    wallet_address: str
    display_name: str | None
    is_active: bool
    created_at: datetime


# ── 工具函数 ─────────────────────────────────────────────

def create_jwt_token(user_id: str) -> str:
    """生成 JWT token"""
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGO)


# ── API 端点 ─────────────────────────────────────────────

@router.post("/connect-wallet", response_model=AuthResponse)
def connect_wallet(body: ConnectWalletRequest, db: Session = Depends(get_db)):
    """
    钱包连接登录
    - 如果钱包地址已存在 → 直接登录
    - 如果是新地址 → 自动注册 + 登录
    """
    user = db.query(User).filter(
        User.wallet_address == body.wallet_address
    ).first()

    if not user:
        user = User(wallet_address=body.wallet_address)
        db.add(user)
        db.commit()
        db.refresh(user)

    token = create_jwt_token(user.id)

    return AuthResponse(
        access_token=token,
        user={
            "id": user.id,
            "wallet_address": user.wallet_address,
            "display_name": user.display_name,
        },
    )


@router.get("/me", response_model=MeResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """
    获取当前登录用户信息
    需要 Header: Authorization: Bearer <token>
    """
    return MeResponse(
        id=current_user.id,
        wallet_address=current_user.wallet_address,
        display_name=current_user.display_name,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
    )


@router.post("/logout")
def logout():
    """
    登出 — JWT 是无状态的，前端删除 token 即可
    """
    return {"message": "Logged out successfully"}