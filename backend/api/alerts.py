"""
Alerts API — 通知（Trades/Social/System）
"""
from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc

from backend.deps import get_db, get_current_user
from backend.models.user import User
from backend.models.alert import Alert

router = APIRouter(prefix="/api", tags=["alerts"])


# ── Response 模型 ────────────────────────────────────────

class AlertResponse(BaseModel):
    id: str
    type: str
    category: str
    title: str
    message: str
    data_json: str | None = None
    is_read: bool
    created_at: datetime


class AlertsPageResponse(BaseModel):
    alerts: list[AlertResponse]
    total_count: int
    unread_count: int


class UnreadCountResponse(BaseModel):
    trades: int = 0
    social: int = 0
    system: int = 0
    total: int = 0


# ── API 端点 ─────────────────────────────────────────────

@router.get("/alerts", response_model=AlertsPageResponse)
def get_alerts(
    category: str = Query("all", regex="^(all|trades|social|system)$"),
    is_read: str = Query("all", regex="^(all|true|false)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    获取通知列表
    - category: all / trades / social / system
    - is_read: all / true / false
    """
    query = db.query(Alert).filter(Alert.user_id == current_user.id)

    if category != "all":
        query = query.filter(Alert.category == category)
    if is_read == "true":
        query = query.filter(Alert.is_read.is_(True))
    elif is_read == "false":
        query = query.filter(Alert.is_read.is_(False))

    total_count = query.count()
    unread_count = (
        db.query(Alert)
        .filter(Alert.user_id == current_user.id, Alert.is_read.is_(False))
        .count()
    )

    alerts = (
        query
        .order_by(desc(Alert.created_at))
        .offset(offset)
        .limit(limit)
        .all()
    )

    return AlertsPageResponse(
        alerts=[
            AlertResponse(
                id=a.id,
                type=a.type,
                category=a.category,
                title=a.title,
                message=a.message,
                data_json=a.data_json,
                is_read=a.is_read,
                created_at=a.created_at,
            )
            for a in alerts
        ],
        total_count=total_count,
        unread_count=unread_count,
    )


@router.get("/alerts/unread-count", response_model=UnreadCountResponse)
def get_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取各类未读通知数量"""
    counts = {}
    for cat in ["trades", "social", "system"]:
        counts[cat] = (
            db.query(Alert)
            .filter(Alert.user_id == current_user.id, Alert.category == cat, Alert.is_read.is_(False))
            .count()
        )

    return UnreadCountResponse(
        trades=counts["trades"],
        social=counts["social"],
        system=counts["system"],
        total=sum(counts.values()),
    )


@router.patch("/alerts/{alert_id}/read")
def mark_as_read(
    alert_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记单条通知为已读"""
    alert = (
        db.query(Alert)
        .filter(Alert.id == alert_id, Alert.user_id == current_user.id)
        .first()
    )
    if not alert:
        raise HTTPException(404, "Alert not found")

    alert.is_read = True
    db.commit()
    return {"message": "Marked as read"}


@router.patch("/alerts/read-all")
def mark_all_as_read(
    category: str = Query("all", regex="^(all|trades|social|system)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记全部通知为已读（可按 category 筛选）"""
    query = db.query(Alert).filter(
        Alert.user_id == current_user.id,
        Alert.is_read.is_(False),
    )
    if category != "all":
        query = query.filter(Alert.category == category)

    count = query.update({"is_read": True})
    db.commit()
    return {"message": f"Marked {count} alerts as read"}


@router.delete("/alerts/{alert_id}")
def delete_alert(
    alert_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除单条通知"""
    alert = (
        db.query(Alert)
        .filter(Alert.id == alert_id, Alert.user_id == current_user.id)
        .first()
    )
    if not alert:
        raise HTTPException(404, "Alert not found")

    db.delete(alert)
    db.commit()
    return {"message": "Alert deleted"}