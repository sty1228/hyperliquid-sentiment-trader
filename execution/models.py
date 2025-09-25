
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional
from uuid import uuid4
from datetime import datetime, timezone

Side = Literal["buy","sell"]

@dataclass
class OrderPlanDTO:
    id: str
    user_id: str
    signal_ref: str
    symbol: str
    side: Side
    qty: float
    limit_px: Optional[float]
    tif: str
    reduce_only: bool
    source: Literal["auto_follow","auto_counter","manual"]
    rule_ref: Optional[str]
    sl_price: Optional[float]

def new_plan_id() -> str:
    return str(uuid4())

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
