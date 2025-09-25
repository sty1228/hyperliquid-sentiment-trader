
from __future__ import annotations
import os
import logging
from typing import List
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
try:
    from backend.config import load_env, env
except Exception:
    def load_env(): ...
    def env(k: str, d: str | None = None): return os.environ.get(k, d)

load_env()

APP_TITLE   = env("APP_TITLE",   "Crypto Sentiment API")
APP_VERSION = env("APP_VERSION", "2.0.0")
APP_DEBUG   = env("APP_DEBUG",   "false").lower() == "true"

app = FastAPI(title=APP_TITLE, version=APP_VERSION, debug=APP_DEBUG)

cors_origins: List[str] = [
    "http://127.0.0.1:3000", "http://localhost:3000",
    "http://127.0.0.1:5173", "http://localhost:5173",
    "http://127.0.0.1:8000", "http://localhost:8000",
]
extra = env("CORS_ORIGIN")
if extra: cors_origins.append(extra)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _try_include(module_path: str, attr: str = "router"):
    try:
        mod = __import__(module_path, fromlist=[attr])
        router = getattr(mod, attr)
        app.include_router(router)
        logging.info(f"[router] mounted: {module_path}.{attr}")
    except Exception as e:
        logging.warning(f"[router] skip {module_path}: {e}")

_try_include("backend.api.trade")         # /api/trade/manual
_try_include("backend.api.executions")   # /api/executions/*
_try_include("backend.api.user")         # /api/user/*
_try_include("backend.api.leaderboard")  # /api/leaderboard*
_try_include("backend.api.summary")      # /api/summary
_try_include("backend.api.horizon")      # /api/horizon
_try_include("backend.api.debug_routes") # /api/debug/routes

try:
    from backend.services import hyperliquid_broker
    app.include_router(hyperliquid_broker.router)
    logging.info("[router] mounted: backend.services.hyperliquid_broker.router (/api/hl/*)")
except Exception as e:
    logging.error(f"[router] Hyperliquid not mounted: {e}")

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": APP_TITLE,
        "version": APP_VERSION,
        "hl_mainnet": os.environ.get("HL_MAINNET", "true"),
        "has_hl_keys": bool(os.environ.get("HL_ACCOUNT_ADDRESS")) and bool(os.environ.get("HL_API_SECRET_KEY")),
    }

@app.get("/")
def root():
    return {"ok": True, "message": f"{APP_TITLE} running", "docs": "/docs"}
