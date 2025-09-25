# simple 
from __future__ import annotations
from execution.models import OrderPlanDTO

class RiskError(Exception): ...

def check_risk(plan: OrderPlanDTO, mark_price: float,
               day_used_notional: float, limit_daily: float | None,
               max_slippage_bps: float = 50.0):
    notional = plan.qty * mark_price
    if plan.qty <= 0:
        raise RiskError("Qty <= 0")
    if limit_daily and (day_used_notional + notional) > limit_daily:
        raise RiskError("Daily notional limit exceeded")
    
