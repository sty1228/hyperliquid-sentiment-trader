
from __future__ import annotations

import uuid
import json
import hashlib
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from execution.schema import ensure_schema
from execution.db import connect, query as db_query
from execution.models import utcnow
from execution.price_feed import PxAdapter  

router = APIRouter(prefix="/api")

def _norm_symbol(s: str) -> str:
    s = (s or "").upper().strip()
    if s.endswith(("USDT", "USDC", "USD")):
        return s
    return s + "USDT"

def _mk_idempotency_key(
    *,
    user_id: str,
    symbol: str,
    side: str,
    qty: float,
    sl_price: Optional[float],
    signal_ref: str,
) -> str:
    """
    Stable idempotency key = sha256(hashable input tuple)
    qty/sl_price stringify to fixed precision to avoid float jitter.
    """
    payload = {
        "user_id": str(user_id).strip(),
        "symbol": _norm_symbol(symbol),
        "side": side.lower().strip(),
        "qty": f"{float(qty):.12g}",
        "sl_price": None if sl_price is None else f"{float(sl_price):.10g}",
        "signal_ref": str(signal_ref).strip(),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()  # 64 hex


class ManualReq(BaseModel):
    user_id: str = Field(..., description="你的系统中的用户ID")
    signal_ref: str
    symbol: str
    side: str  # 'buy' | 'sell'
    size_type: str = "fixed_usd" 
    size_value: float
    leverage: float = 1.0
    sl_type: str | None = "percent"   # 'percent' | None
    sl_value: float | None = 0.02

@router.post("/trade/manual")
def trade_manual(req: ManualReq):
    if req.side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy/sell")
    if req.size_type != "fixed_usd":
        raise HTTPException(400, "only size_type=fixed_usd is supported in this step")

    ensure_schema()
    con = connect()

    try:

        symbol = _norm_symbol(req.symbol)
        feed = PxAdapter()
        mark = float(feed.mark(symbol))
        if mark <= 0:
            raise HTTPException(400, "bad mark price")

        notional = float(req.size_value) * float(req.leverage)
        qty = notional / mark


        sl_price = None
        if req.sl_type == "percent" and req.sl_value:
            sl_price = mark * (1 - float(req.sl_value)) if req.side == "buy" else mark * (1 + float(req.sl_value))


        idempo = _mk_idempotency_key(
            user_id=req.user_id,
            symbol=symbol,
            side=req.side,
            qty=qty,
            sl_price=sl_price,
            signal_ref=req.signal_ref,
        )

       
        dup = db_query(
            con,
            """
            SELECT id FROM order_plans
            WHERE user_id=? AND signal_ref=? AND source='manual'
              AND datetime(created_at) > datetime('now','-5 minutes')
            """,
            (req.user_id, req.signal_ref),
        )
        if dup:
            
            row = db_query(con, "SELECT id FROM order_plans WHERE idempotency_key=?", (idempo,))
            plan_id = row[0]["id"] if row else dup[0]["id"]
            return {"ok": True, "plan_id": plan_id, "symbol": symbol, "mark": mark, "qty": qty, "cooldown": True}

        plan_id = str(uuid.uuid4())
        now = utcnow()

        con.execute(
            """
            INSERT INTO order_plans
              (id, user_id, signal_ref, symbol, side, qty,
               limit_px, tif, reduce_only, source, rule_ref, risk_json, status,
               sl_price, created_at, updated_at, idempotency_key)
            VALUES
              (?, ?, ?, ?, ?, ?,
               NULL, 'IOC', 0, 'manual', NULL, '{}', 'created',
               ?, ?, ?, ?)
            ON CONFLICT(idempotency_key)
            DO UPDATE SET updated_at = excluded.updated_at
            """,
            (
                plan_id, req.user_id, req.signal_ref, symbol, req.side, float(qty),
                None if sl_price is None else float(sl_price),
                now, now, idempo,
            ),
        )
        con.commit()

        row = db_query(con, "SELECT id FROM order_plans WHERE idempotency_key=?", (idempo,))
        if row:
            plan_id = row[0]["id"]

        return {"ok": True, "plan_id": plan_id, "symbol": symbol, "mark": mark, "qty": qty}
    finally:
        con.close()

from pydantic import BaseModel as _BaseModel 

class ExecQueryResp(_BaseModel):
    plan: dict
    events: list[dict]

@router.get("/executions/{plan_id}", response_model=ExecQueryResp)
def get_execution(plan_id: str):
    ensure_schema()
    con = connect()
    try:
        plans = db_query(con, "SELECT * FROM order_plans WHERE id=?", (plan_id,))
        if not plans:
            raise HTTPException(404, "plan not found")
        events = db_query(con, "SELECT * FROM exec_events WHERE plan_id=? ORDER BY ts ASC", (plan_id,))
        return {"plan": plans[0], "events": events}
    finally:
        con.close()
