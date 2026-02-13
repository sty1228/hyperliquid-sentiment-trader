from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
from backend.deps import get_db
from backend.config import get_settings

router = APIRouter(tags=["health"])

@router.get("/api/health")
def health(db: Session = Depends(get_db)):
    settings = get_settings()
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "ok": db_ok,
        "service": "HyperCopy API",
        "version": "3.0.0",
        "database": "connected" if db_ok else "error",
        "hl_mainnet": settings.HL_MAINNET,
    }