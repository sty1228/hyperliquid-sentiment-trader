from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager

import sentry_sdk
sentry_sdk.init(
    dsn="https://8ec3ec7c2d0d95a793c6357e47ead90f@o4510965336113152.ingest.us.sentry.io/4510965338537984",
    send_default_pii=True,
    traces_sample_rate=0.1,
)
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from backend.config import get_settings
from backend.api.health import router as health_router
from backend.api.auth import router as auth_router
from backend.api.leaderboard import router as leaderboard_router
from backend.api.trader import router as trader_router
from backend.api.follow import router as follow_router
from backend.api.settings import router as settings_router
from backend.api.portfolio import router as portfolio_router
from backend.api.trades import router as trades_router
from backend.api.alerts import router as alerts_router
from backend.api.deposit import router as deposit_router
from backend.api.wallet import router as wallet_router
from backend.api.explore import router as explore_router
from backend.api.rewards import router as rewards_router
from backend.api.network import router as network_router
from backend.api.events import router as events_router, listen_network_events_task

log = logging.getLogger("main")

# referral_api is optional — skip gracefully if file doesn't exist
try:
    from backend.api.referral_api import router as referral_router
    _has_referral = True
except ImportError:
    _has_referral = False

settings = get_settings()

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ★ Start the LISTEN/NOTIFY bridge that fans NetworkEvent rows to SSE clients.
    listen_task = asyncio.create_task(listen_network_events_task())
    try:
        yield
    finally:
        listen_task.cancel()
        try:
            await listen_task
        except (asyncio.CancelledError, Exception) as e:
            log.info(f"LISTEN task shutdown: {type(e).__name__}")


app = FastAPI(title="HyperCopy API", version="3.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(leaderboard_router)
app.include_router(trader_router)
app.include_router(follow_router)
app.include_router(settings_router)
app.include_router(portfolio_router)
app.include_router(trades_router)
app.include_router(alerts_router)
app.include_router(deposit_router)
app.include_router(wallet_router)
app.include_router(explore_router)
app.include_router(rewards_router)
app.include_router(network_router)
app.include_router(events_router)

if _has_referral:
    app.include_router(referral_router)

@app.get("/")
def root():
    return {"ok": True, "message": "HyperCopy API v3", "docs": "/docs"}