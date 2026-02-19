from __future__ import annotations
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

settings = get_settings()

app = FastAPI(title="HyperCopy API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
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

@app.get("/")
def root():
    return {"ok": True, "message": "HyperCopy API v3", "docs": "/docs"}