
from __future__ import annotations

import enum
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from sqlalchemy import (
    create_engine, Column, String, Integer, DateTime, Text, Enum as SAEnum,
    ForeignKey, Index, select, event
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

from backend.config import get_db_path

DATABASE_URL = f"sqlite:///{get_db_path()}"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.close()
    except Exception:
        pass

import logging, os
logging.getLogger().setLevel("INFO")
logging.info("lifecycle.py loaded from: %s", __file__)
try:
    db_path_fs = DATABASE_URL.replace("sqlite:///", "")
    logging.info("DATABASE_URL=%s exists=%s cwd=%s",
                 DATABASE_URL, os.path.exists(db_path_fs), os.getcwd())
except Exception:
    pass

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

class PlanStatus(str, enum.Enum):
    created = "created"
    submitted = "submitted"
    partially_filled = "partially_filled"
    filled = "filled"
    canceled = "canceled"
    error = "error"

TERMINAL_STATUSES = {PlanStatus.filled, PlanStatus.canceled, PlanStatus.error}
INFLIGHT_STATUSES = {PlanStatus.created, PlanStatus.submitted, PlanStatus.partially_filled}

class OrderPlan(Base):
    __tablename__ = "order_plans"

    plan_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    size_type = Column(String, nullable=False)
    size_value = Column(String, nullable=False)
    leverage = Column(Integer, nullable=True)
    sl_type = Column(String, nullable=True)
    sl_value = Column(String, nullable=True)

    status = Column(SAEnum(PlanStatus), nullable=False, index=True, default=PlanStatus.created)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    meta_json = Column(Text, nullable=True)

    events = relationship("ExecEvent", back_populates="plan", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_order_plans_status_updated", "status", "updated_at"),)

    @property
    def meta(self) -> Optional[Dict[str, Any]]:
        if self.meta_json:
            try:
                return json.loads(self.meta_json)
            except Exception:
                return None
        return None

    @meta.setter
    def meta(self, value: Optional[Dict[str, Any]]):
        self.meta_json = json.dumps(value or {}, ensure_ascii=False)


class ExecEvent(Base):
    __tablename__ = "exec_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(String, ForeignKey("order_plans.plan_id", ondelete="CASCADE"), nullable=False, index=True)

    at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    from_status = Column(SAEnum(PlanStatus), nullable=True)
    to_status = Column(SAEnum(PlanStatus), nullable=False)

    event = Column(String, nullable=False)
    reason = Column(Text, nullable=True)
    receipt_json = Column(Text, nullable=True)

    plan = relationship("OrderPlan", back_populates="events")

    __table_args__ = (Index("ix_exec_events_plan_time", "plan_id", "at"),)

    @property
    def receipt(self) -> Optional[Dict[str, Any]]:
        if self.receipt_json:
            try:
                return json.loads(self.receipt_json)
            except Exception:
                return None
        return None

    @receipt.setter
    def receipt(self, value: Optional[Dict[str, Any]]):
        self.receipt_json = json.dumps(value or {}, ensure_ascii=False)

class ExecEventOut(BaseModel):
    at: datetime
    from_status: Optional[PlanStatus]
    to_status: PlanStatus
    event: str
    reason: Optional[str]
    receipt: Optional[Dict[str, Any]]

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class OrderPlanOut(BaseModel):
    plan_id: str
    status: PlanStatus
    user_id: str
    symbol: str
    side: str
    size_type: str
    size_value: str
    leverage: Optional[int]
    sl_type: Optional[str]
    sl_value: Optional[str]
    created_at: datetime
    updated_at: datetime
    meta: Optional[Dict[str, Any]]

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class ExchangeAdapter:
    def submit(self, plan: OrderPlan) -> Dict[str, Any]:
        return {"ok": True, "order_id": str(uuid.uuid4())}

    def query(self, plan: OrderPlan) -> Dict[str, Any]:
        return {"status": "submitted", "raw": {}}

    def cancel(self, plan: OrderPlan) -> Dict[str, Any]:
        return {"ok": True}

class Lifecycle:
    def __init__(self, db: Session, adapter: ExchangeAdapter):
        self.db = db
        self.adapter = adapter

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _log_event(
        self,
        plan: OrderPlan,
        to_status: PlanStatus,
        event: str,
        reason: Optional[str] = None,
        receipt: Optional[Dict[str, Any]] = None,
    ) -> ExecEvent:
        e = ExecEvent(
            plan_id=plan.plan_id,
            from_status=plan.status,
            to_status=to_status,
            event=event,
            reason=reason,
            at=self._now(),
        )
        e.receipt = receipt or {}
        plan.status = to_status
        plan.updated_at = e.at
        self.db.add(e)
        self.db.add(plan)
        self.db.commit()
        self.db.refresh(plan)
        return e

    def submit(self, plan_id: str) -> OrderPlan:
        plan = self.db.query(OrderPlan).filter(OrderPlan.plan_id == plan_id).one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="plan not found")
        if plan.status not in {PlanStatus.created, PlanStatus.error}:
            return plan
        try:
            rc = self.adapter.submit(plan)
            self._log_event(plan, PlanStatus.submitted, "submit", "order submitted", rc)
            return plan
        except Exception as ex:
            self._log_event(plan, PlanStatus.error, "submit_error", str(ex), {})
            return plan

    def refresh(self, plan_id: str) -> OrderPlan:
        plan = self.db.query(OrderPlan).filter(OrderPlan.plan_id == plan_id).one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="plan not found")
        if plan.status in TERMINAL_STATUSES:
            return plan
        try:
            q = self.adapter.query(plan)
            mapping = {
                "submitted": PlanStatus.submitted,
                "partially_filled": PlanStatus.partially_filled,
                "filled": PlanStatus.filled,
                "canceled": PlanStatus.canceled,
                "error": PlanStatus.error,
            }
            new_status = mapping.get(q.get("status"), PlanStatus.error)
            if new_status != plan.status:
                self._log_event(
                    plan,
                    new_status,
                    "refresh",
                    f"venue status → {q.get('status')}",
                    {"raw": q.get("raw", q)},
                )
            return plan
        except Exception as ex:
            self._log_event(plan, PlanStatus.error, "refresh_error", str(ex), {})
            return plan

    def cancel(self, plan_id: str, reason: str = "user_cancel") -> OrderPlan:
        plan = self.db.query(OrderPlan).filter(OrderPlan.plan_id == plan_id).one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="plan not found")
        if plan.status in TERMINAL_STATUSES:
            return plan
        try:
            rc = self.adapter.cancel(plan)
            self._log_event(plan, PlanStatus.canceled, "cancel", reason, rc)
            return plan
        except Exception as ex:
            self._log_event(plan, PlanStatus.error, "cancel_error", str(ex), {})
            return plan

def resume_inflight_on_startup() -> None:
    with SessionLocal() as db:
        adapter = ExchangeAdapter()
        sm = Lifecycle(db, adapter)
        inflight: List[OrderPlan] = db.execute(
            select(OrderPlan).where(OrderPlan.status.in_(list(INFLIGHT_STATUSES)))
        ).scalars().all()
        for plan in inflight:
            try:
                ev = ExecEvent(
                    plan_id=plan.plan_id,
                    from_status=plan.status,
                    to_status=plan.status,
                    event="resume_check",
                    reason="process resumed – polling venue",
                    at=datetime.now(timezone.utc),
                )
                ev.receipt = {}
                db.add(ev)
                db.commit()
                sm.refresh(plan.plan_id)
            except Exception as ex:
                sm._log_event(plan, PlanStatus.error, "resume_error", str(ex), {})


router = APIRouter(prefix="/api", tags=["executions"])

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.on_event("startup")
def _on_startup():
    Base.metadata.create_all(bind=engine)
    resume_inflight_on_startup()

class PlanCreate(BaseModel):
    user_id: str
    symbol: str
    side: str
    size_type: str
    size_value: str
    leverage: Optional[int] = None
    sl_type: Optional[str] = None
    sl_value: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

@router.post("/executions", response_model=OrderPlanOut)
async def create_plan(payload: PlanCreate, db: Session = Depends(get_db)):
    def _do():
        plan = OrderPlan(
            plan_id=str(uuid.uuid4()),
            user_id=payload.user_id,
            symbol=payload.symbol,
            side=payload.side,
            size_type=payload.size_type,
            size_value=payload.size_value,
            leverage=payload.leverage,
            sl_type=payload.sl_type,
            sl_value=payload.sl_value,
        )
        plan.meta = payload.meta or {}
        db.add(plan); db.commit(); db.refresh(plan)

        e = ExecEvent(
            plan_id=plan.plan_id,
            from_status=None,
            to_status=PlanStatus.created,
            event="init",
            reason="plan created",
            at=datetime.now(timezone.utc),
        )
        e.receipt = plan.meta or {}
        db.add(e); db.commit()

        return OrderPlanOut(
            plan_id=plan.plan_id, status=plan.status, user_id=plan.user_id,
            symbol=plan.symbol, side=plan.side, size_type=plan.size_type, size_value=plan.size_value,
            leverage=plan.leverage, sl_type=plan.sl_type, sl_value=plan.sl_value,
            created_at=plan.created_at, updated_at=plan.updated_at, meta=plan.meta,
        )
    return await run_in_threadpool(_do)

@router.get("/executions/{plan_id}/events", response_model=List[ExecEventOut])
async def get_execution_events(plan_id: str, db: Session = Depends(get_db)):
    def _load():
        exists = (
            db.query(OrderPlan.plan_id)
            .filter(OrderPlan.plan_id == plan_id)
            .one_or_none()
        )
        if not exists:
            raise HTTPException(status_code=404, detail="plan not found")

        events: List[ExecEvent] = (
            db.query(ExecEvent)
            .filter(ExecEvent.plan_id == plan_id)
            .order_by(ExecEvent.at.asc(), ExecEvent.id.asc())
            .all()
        )
        out: List[ExecEventOut] = []
        for e in events:
            out.append(
                ExecEventOut(
                    at=e.at,
                    from_status=e.from_status,
                    to_status=e.to_status,
                    event=e.event,
                    reason=e.reason,
                    receipt=e.receipt,
                )
            )
        return out
    return await run_in_threadpool(_load)


@router.post("/executions/{plan_id}/submit", response_model=OrderPlanOut)
async def api_submit(plan_id: str, db: Session = Depends(get_db)):
    def _do():
        sm = Lifecycle(db, ExchangeAdapter())
        plan = sm.submit(plan_id)
        return OrderPlanOut(
            plan_id=plan.plan_id, status=plan.status, user_id=plan.user_id,
            symbol=plan.symbol, side=plan.side, size_type=plan.size_type, size_value=plan.size_value,
            leverage=plan.leverage, sl_type=plan.sl_type, sl_value=plan.sl_value,
            created_at=plan.created_at, updated_at=plan.updated_at, meta=plan.meta,
        )
    return await run_in_threadpool(_do)

@router.post("/executions/{plan_id}/refresh", response_model=OrderPlanOut)
async def api_refresh(plan_id: str, db: Session = Depends(get_db)):
    def _do():
        sm = Lifecycle(db, ExchangeAdapter())
        plan = sm.refresh(plan_id)
        return OrderPlanOut(
            plan_id=plan.plan_id, status=plan.status, user_id=plan.user_id,
            symbol=plan.symbol, side=plan.side, size_type=plan.size_type, size_value=plan.size_value,
            leverage=plan.leverage, sl_type=plan.sl_type, sl_value=plan.sl_value,
            created_at=plan.created_at, updated_at=plan.updated_at, meta=plan.meta,
        )
    return await run_in_threadpool(_do)

@router.post("/executions/{plan_id}/cancel", response_model=OrderPlanOut)
async def api_cancel(plan_id: str, reason: Optional[str] = None, db: Session = Depends(get_db)):
    def _do():
        sm = Lifecycle(db, ExchangeAdapter())
        plan = sm.cancel(plan_id, reason or "user_cancel")
        return OrderPlanOut(
            plan_id=plan.plan_id, status=plan.status, user_id=plan.user_id,
            symbol=plan.symbol, side=plan.side, size_type=plan.size_type, size_value=plan.size_value,
            leverage=plan.leverage, sl_type=plan.sl_type, sl_value=plan.sl_value,
            created_at=plan.created_at, updated_at=plan.updated_at, meta=plan.meta,
        )
    return await run_in_threadpool(_do)

__all__ = ["router", "Base", "engine", "resume_inflight_on_startup"]
