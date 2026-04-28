"""
SSE event channel for the trader-network graph.

Two endpoints:
  POST /api/auth/stream-token       — exchange a JWT bearer for a 60s SSE-scoped token.
  GET  /api/events/stream?token=…   — text/event-stream; emits NetworkEvent payloads
                                       for the authenticated user.

Why a stream-token? EventSource cannot send custom headers, so the JWT can't
ride in Authorization. We mint a short-lived JWT with `aud="sse"` and pass it
in the query string. The cookie alternative requires SameSite/CORS tuning and
couples the session cookie to a long-lived stream URL — the token is cleaner.

Connection lifecycle:
  - On connect: optional `?last_id=N` triggers a backfill from network_events
    where id > last_id; then we attach an asyncio.Queue to the per-process
    fanout map and stream live events as they arrive from LISTEN/NOTIFY.
  - Heartbeat `: ping` every 20s defeats proxies.
  - On disconnect: queue is removed from the fanout set.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.deps import get_current_user, get_db
from backend.models.network_event import NetworkEvent
from backend.models.user import User

log = logging.getLogger("events_api")
settings = get_settings()

router = APIRouter(prefix="/api", tags=["events"])

# Process-global fanout: { user_id: set[asyncio.Queue] }.
# The LISTEN task in main.py lifespan pushes events into every queue keyed by user_id.
USER_QUEUES: dict[str, set[asyncio.Queue]] = {}

STREAM_TOKEN_TTL_SEC = 60
HEARTBEAT_INTERVAL_SEC = 20
QUEUE_MAXSIZE = 64


def _make_stream_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "aud": "sse",
        "exp": datetime.now(tz=timezone.utc) + timedelta(seconds=STREAM_TOKEN_TTL_SEC),
        "iat": datetime.now(tz=timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGO)


def _decode_stream_token(token: str) -> str:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGO],
            audience="sse",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Stream token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid stream token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(401, "Stream token missing sub")
    return user_id


@router.post("/auth/stream-token")
def issue_stream_token(current_user: User = Depends(get_current_user)):
    """Mint a short-lived JWT for the EventSource query-string handshake."""
    return {
        "token": _make_stream_token(current_user.id),
        "expires_in": STREAM_TOKEN_TTL_SEC,
    }


def _format_sse(event_id: int | None, data: dict) -> str:
    """Format a payload as an SSE message frame."""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data, default=str)}")
    return "\n".join(lines) + "\n\n"


async def _event_generator(
    request: Request,
    user_id: str,
    last_id: int | None,
    db: Session,
) -> AsyncIterator[str]:
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    USER_QUEUES.setdefault(user_id, set()).add(queue)
    try:
        # Backfill any events missed since `last_id` (reconnect).
        if last_id is not None and last_id >= 0:
            backlog = (
                db.query(NetworkEvent)
                .filter(NetworkEvent.user_id == user_id, NetworkEvent.id > last_id)
                .order_by(NetworkEvent.id.asc())
                .limit(500)
                .all()
            )
            for row in backlog:
                yield _format_sse(row.id, row.payload)

        # Initial comment so the client's onopen fires before the first real event.
        yield ": connected\n\n"

        while True:
            if await request.is_disconnected():
                break
            try:
                payload = await asyncio.wait_for(
                    queue.get(), timeout=HEARTBEAT_INTERVAL_SEC
                )
            except asyncio.TimeoutError:
                # Heartbeat — comment line, ignored by EventSource consumers.
                yield ": ping\n\n"
                continue
            yield _format_sse(payload.get("id"), payload)
    finally:
        bucket = USER_QUEUES.get(user_id)
        if bucket is not None:
            bucket.discard(queue)
            if not bucket:
                USER_QUEUES.pop(user_id, None)


@router.get("/events/stream")
async def events_stream(
    request: Request,
    token: str | None = Query(
        None,
        description="Stream token from POST /api/auth/stream-token. "
                    "Required as a query param because browser EventSource "
                    "cannot set Authorization headers.",
    ),
    last_id: int | None = Query(None, description="Resume after this event id"),
    db: Session = Depends(get_db),
):
    """SSE stream of network events for the authenticated user."""
    # Custom 422 instead of the default Pydantic "field required" message —
    # the hint about EventSource header limitations saves a round of FE debugging.
    if not token:
        raise HTTPException(
            status_code=422,
            detail=(
                "missing required query param 'token' "
                "(browser EventSource cannot set Authorization header; "
                "mint via POST /api/auth/stream-token then pass as ?token=...)"
            ),
        )
    user_id = _decode_stream_token(token)
    return StreamingResponse(
        _event_generator(request, user_id, last_id, db),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )


# ── LISTEN/NOTIFY bridge — invoked from main.py lifespan ─────────


async def listen_network_events_task() -> None:
    """
    Long-running asyncio task that LISTENs on Postgres channel 'network_events'
    and pushes each row's payload into all per-user queues.

    Uses psycopg2 in autocommit on a dedicated connection. Reconnects on failure.
    """
    import psycopg2  # local import — only used by the API process
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    while True:
        conn = None
        try:
            conn = psycopg2.connect(settings.DATABASE_URL)
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            cur.execute("LISTEN network_events;")
            log.info("📡 LISTEN network_events — connected")

            loop = asyncio.get_running_loop()
            while True:
                # Wait for socket to become readable, then drain notifies.
                await loop.run_in_executor(None, conn.poll)
                while conn.notifies:
                    n = conn.notifies.pop(0)
                    try:
                        ev_id = int(n.payload)
                    except (TypeError, ValueError):
                        continue
                    await _fanout_event(ev_id)
                # Yield briefly so we don't spin.
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            log.info("📡 LISTEN task cancelled")
            raise
        except Exception as e:
            log.error(f"📡 LISTEN task error: {e} — reconnecting in 5s")
            await asyncio.sleep(5.0)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass


async def _fanout_event(event_id: int) -> None:
    """Fetch the event row by id and push it to every queue for that user_id."""
    from backend.database import SessionLocal

    def _load():
        db = SessionLocal()
        try:
            row = db.query(NetworkEvent).filter(NetworkEvent.id == event_id).first()
            if row is None:
                return None
            return (row.user_id, row.payload)
        finally:
            db.close()

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, _load)
    if not res:
        return
    user_id, payload = res
    bucket = USER_QUEUES.get(user_id)
    if not bucket:
        return
    for q in list(bucket):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # Slow consumer — drop the event for this client; reconnect with last_id will backfill.
            pass
