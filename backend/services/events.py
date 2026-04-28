"""
Event publishing — feeds the SSE network-graph stream.

publish() inserts a `network_events` row inside the caller's transaction and
issues `pg_notify('network_events', <id>)`. The API process listens on that
channel and fans rows to connected SSE clients keyed by user_id. Because the
notify is part of the same transaction, events for rolled-back trades never
reach the channel.

Bundled side-effect: also writes an `Alert` row so the existing /api/alerts
endpoints actually surface trade lifecycle activity (the table was unused
prior to 2026-04-28).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.models.alert import Alert
from backend.models.network_event import NetworkEvent

log = logging.getLogger("events")


# Map NetworkEvent.type → (Alert.type, Alert.title, Alert.category).
# Anything not in the map skips the alert write but still publishes the event.
_ALERT_MAP: dict[str, tuple[str, str, str]] = {
    "trade_opened":   ("trade_opened",  "Trade opened",  "trades"),
    "trade_closed":   ("trade_closed",  "Trade closed",  "trades"),
    "tp_hit":         ("take_profit",   "Take profit hit", "trades"),
    "sl_hit":         ("stop_loss",     "Stop loss hit", "trades"),
    "equity_protect": ("trade_closed",  "Equity protection", "trades"),
}


def _alert_message(ev_type: str, payload: dict[str, Any]) -> str:
    ticker = payload.get("ticker", "?")
    direction = payload.get("direction", "")
    size_usd = payload.get("size_usd")
    pnl_usd = payload.get("pnl_usd")
    src = payload.get("source")
    bits = []
    if direction:
        bits.append(f"{direction.upper()} {ticker}")
    else:
        bits.append(ticker)
    if size_usd is not None:
        bits.append(f"${float(size_usd):.2f}")
    if pnl_usd is not None:
        sign = "+" if float(pnl_usd) >= 0 else ""
        bits.append(f"PnL {sign}{float(pnl_usd):.2f}")
    if src:
        bits.append(f"({src})")
    return " · ".join(bits)


def publish(db: Session, user_id: str, event_type: str, payload: dict[str, Any]) -> NetworkEvent:
    """
    Insert a NetworkEvent + Alert row, flush so the BIGSERIAL id is assigned,
    then NOTIFY listeners with the row id. All inside the caller's transaction.

    Caller is responsible for the surrounding commit/rollback. If the caller
    rolls back, the NOTIFY is also rolled back (Postgres holds notifications
    until commit), so listeners only ever see committed events.
    """
    body = {
        "v": 1,
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        **payload,
    }
    row = NetworkEvent(user_id=user_id, type=event_type, payload=body)
    db.add(row)
    db.flush()  # assign id

    body["id"] = row.id  # echo id into the payload for SSE event-id correlation

    # Alert mirror (best-effort; never break the publish on Alert errors).
    mapping = _ALERT_MAP.get(event_type)
    if mapping is not None:
        alert_type, title, category = mapping
        try:
            db.add(
                Alert(
                    user_id=user_id,
                    type=alert_type,
                    category=category,
                    title=title,
                    message=_alert_message(event_type, body),
                    data_json=json.dumps(body, default=str),
                )
            )
        except Exception as e:
            log.warning(f"Alert mirror failed for {event_type}: {e}")

    # NOTIFY — channel name is fixed; payload is the row id (small, fits 8KB limit).
    try:
        db.execute(
            text("SELECT pg_notify('network_events', :id)"),
            {"id": str(row.id)},
        )
    except Exception as e:
        # NOTIFY isn't critical for correctness — listeners can poll
        # network_events.id > last_seen as a backstop.
        log.warning(f"pg_notify failed (event {row.id}): {e}")

    return row
