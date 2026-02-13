"""
Settings API — 跟单设置（默认 + 每个 trader 个性化）
匹配前端 DefaultFollowSettings 接口
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.trader import Trader
from backend.models.setting import CopySetting

router = APIRouter(prefix="/api", tags=["settings"])


# ── Request / Response 模型（匹配前端 DefaultFollowSettings）────

class TPOrSL(BaseModel):
    type: str = "USD"   # "USD" | "PCT"
    value: float = 0.0


class CopySettingsRequest(BaseModel):
    tradeSizeType: str = "USD"     # "USD" | "PCT"
    tradeSize: float = 64.0
    leverage: float = 8.0
    leverageType: str = "cross"    # "isolated" | "cross"
    tp: TPOrSL = TPOrSL(type="PCT", value=15.0)
    sl: TPOrSL = TPOrSL(type="USD", value=169.0)
    orderType: str = "market"      # "market" | "limit"


class CopySettingsResponse(BaseModel):
    tradeSizeType: str
    tradeSize: float
    leverage: float
    leverageType: str
    tp: TPOrSL
    sl: TPOrSL
    orderType: str


class TraderSettingItem(BaseModel):
    trader_username: str
    display_name: str | None = None
    settings: CopySettingsResponse


# ── 工具函数 ─────────────────────────────────────────────

def _setting_to_response(s: CopySetting) -> CopySettingsResponse:
    return CopySettingsResponse(
        tradeSizeType="PCT" if s.size_type == "percent" else "USD",
        tradeSize=s.size_value,
        leverage=s.leverage,
        leverageType=s.margin_mode,
        tp=TPOrSL(type="PCT" if s.tp_type == "percent" else "USD", value=s.tp_value),
        sl=TPOrSL(type="PCT" if s.sl_type == "percent" else "USD", value=s.sl_value),
        orderType=s.order_type,
    )


def _apply_request_to_setting(s: CopySetting, body: CopySettingsRequest):
    s.size_type = "percent" if body.tradeSizeType == "PCT" else "fixed_usd"
    s.size_value = body.tradeSize
    s.leverage = body.leverage
    s.margin_mode = body.leverageType
    s.tp_type = "percent" if body.tp.type == "PCT" else "fixed_usd"
    s.tp_value = body.tp.value
    s.sl_type = "percent" if body.sl.type == "PCT" else "fixed_usd"
    s.sl_value = body.sl.value
    s.order_type = body.orderType


# ── API 端点 ─────────────────────────────────────────────

@router.get("/settings/default", response_model=CopySettingsResponse)
def get_default_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取默认跟单设置"""
    setting = (
        db.query(CopySetting)
        .filter(CopySetting.user_id == current_user.id, CopySetting.trader_id.is_(None))
        .first()
    )
    if not setting:
        # 没有设置过，返回默认值
        setting = CopySetting(user_id=current_user.id, trader_id=None)
        db.add(setting)
        db.commit()
        db.refresh(setting)

    return _setting_to_response(setting)


@router.put("/settings/default", response_model=CopySettingsResponse)
def update_default_settings(
    body: CopySettingsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新默认跟单设置"""
    setting = (
        db.query(CopySetting)
        .filter(CopySetting.user_id == current_user.id, CopySetting.trader_id.is_(None))
        .first()
    )
    if not setting:
        setting = CopySetting(user_id=current_user.id, trader_id=None)
        db.add(setting)

    _apply_request_to_setting(setting, body)
    db.commit()
    db.refresh(setting)
    return _setting_to_response(setting)


@router.get("/settings/trader/{trader_username}", response_model=CopySettingsResponse)
def get_trader_settings(
    trader_username: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取针对某个 trader 的个性化跟单设置"""
    trader = db.query(Trader).filter(Trader.username == trader_username).first()
    if not trader:
        raise HTTPException(404, f"Trader @{trader_username} not found")

    setting = (
        db.query(CopySetting)
        .filter(CopySetting.user_id == current_user.id, CopySetting.trader_id == trader.id)
        .first()
    )
    if not setting:
        # 没有个性化设置，返回默认设置
        default = (
            db.query(CopySetting)
            .filter(CopySetting.user_id == current_user.id, CopySetting.trader_id.is_(None))
            .first()
        )
        if default:
            return _setting_to_response(default)
        return CopySettingsResponse(
            tradeSizeType="PCT", tradeSize=64.0, leverage=8.0,
            leverageType="cross", tp=TPOrSL(type="PCT", value=15.0),
            sl=TPOrSL(type="USD", value=169.0), orderType="market",
        )

    return _setting_to_response(setting)


@router.put("/settings/trader/{trader_username}", response_model=CopySettingsResponse)
def update_trader_settings(
    trader_username: str,
    body: CopySettingsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新针对某个 trader 的个性化设置"""
    trader = db.query(Trader).filter(Trader.username == trader_username).first()
    if not trader:
        raise HTTPException(404, f"Trader @{trader_username} not found")

    setting = (
        db.query(CopySetting)
        .filter(CopySetting.user_id == current_user.id, CopySetting.trader_id == trader.id)
        .first()
    )
    if not setting:
        setting = CopySetting(user_id=current_user.id, trader_id=trader.id)
        db.add(setting)

    _apply_request_to_setting(setting, body)
    db.commit()
    db.refresh(setting)
    return _setting_to_response(setting)


@router.get("/settings/traders", response_model=list[TraderSettingItem])
def get_all_trader_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取所有个性化 trader 设置列表"""
    settings = (
        db.query(CopySetting)
        .filter(CopySetting.user_id == current_user.id, CopySetting.trader_id.isnot(None))
        .all()
    )

    result = []
    for s in settings:
        trader = db.query(Trader).filter(Trader.id == s.trader_id).first()
        if trader:
            result.append(TraderSettingItem(
                trader_username=trader.username,
                display_name=trader.display_name,
                settings=_setting_to_response(s),
            ))
    return result