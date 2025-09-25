
from __future__ import annotations

import json

from execution.db import connect, query, exec as db_exec, scalar
from execution.models import OrderPlanDTO, utcnow
from execution.risk import check_risk, RiskError
from execution.brokers import SimBroker
from execution.px_adapter import PxAdapter


class Executor:

    def __init__(self, daily_limit: float | None = None):
        self.px = PxAdapter()
        self.broker = SimBroker(self.px)
        self.daily_limit = daily_limit

    def _emit(self, con, plan_id: str, event: str, detail: dict | None = None):
        db_exec(
            con,
            "INSERT INTO exec_events (plan_id, ts, event, detail_json) VALUES (?,?,?,?)",
            (plan_id, utcnow(), event, json.dumps(detail or {})),
        )

    def process_created_plans(self):
        con = connect()
        try:
            plans = query(con, "SELECT * FROM order_plans WHERE status='created'")
            for p in plans:
                plan = OrderPlanDTO(
                    id=p["id"],
                    user_id=p["user_id"],
                    signal_ref=p["signal_ref"],
                    symbol=p["symbol"],
                    side=p["side"],
                    qty=float(p["qty"]),
                    limit_px=p["limit_px"],
                    tif=p["tif"] or "IOC",
                    reduce_only=bool(p["reduce_only"]),
                    source=p["source"],
                    rule_ref=p["rule_ref"],
                    sl_price=p["sl_price"],
                )

                mark = self.px.mark(plan.symbol)

                used = scalar(
                    con,
                    "SELECT COALESCE(SUM(qty * ?),0) FROM order_plans "
                    "WHERE user_id=? AND date(created_at)=date('now') "
                    "AND status IN ('sent','acked','filled')",
                    (mark, plan.user_id),
                ) or 0.0

                try:

                    check_risk(plan, float(mark), float(used), self.daily_limit)


                    ack = self.broker.place_market(
                        plan.symbol,
                        plan.side,
                        plan.qty,
                        tif=plan.tif,
                        reduce_only=plan.reduce_only,
                        client_order_id=plan.id,
                    )

                    new_status = "acked" if ack.get("status") == "ack" else "rejected"
                    db_exec(
                        con,
                        "UPDATE order_plans SET status=?, broker_order_id=?, updated_at=? WHERE id=?",
                        (new_status, ack.get("broker_order_id"), utcnow(), plan.id),
                    )
                    self._emit(con, plan.id, "sent", {"ack": ack, "mark": mark})

                except RiskError as e:
                    db_exec(
                        con,
                        "UPDATE order_plans SET status=?, updated_at=? WHERE id=?",
                        ("rejected", utcnow(), plan.id),
                    )
                    self._emit(con, plan.id, "reject", {"reason": str(e)})
        finally:
            con.close()


    def sl_daemon_tick(self):
        con = connect()
        try:
            rows = query(
                con,
                "SELECT * FROM order_plans "
                "WHERE sl_price IS NOT NULL AND status IN ('acked','partially_filled')",
            )
            for r in rows:
                mark = self.px.mark(r["symbol"])

                hit = (r["side"] == "buy" and mark <= r["sl_price"]) or (
                    r["side"] == "sell" and mark >= r["sl_price"]
                )
                if not hit:
                    continue

   
                ack = self.broker.place_market(
                    r["symbol"],
                    "sell" if r["side"] == "buy" else "buy",
                    float(r["qty"]),
                    tif="IOC",
                    reduce_only=True,
                    client_order_id=f"{r['id']}-sl",
                )
                self._emit(con, r["id"], "sl_trigger", {"ack": ack, "mark": mark})

   
                db_exec(
                    con,
                    "UPDATE order_plans SET status=?, updated_at=? WHERE id=?",
                    ("filled", utcnow(), r["id"]),
                )
        finally:
            con.close()
