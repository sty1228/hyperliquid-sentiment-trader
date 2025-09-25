from __future__ import annotations
from typing import Optional, Any
from backend.config import load_env, env
from backend.services.sources import create_price_source

load_env()

def _get_price_number(payload: Any) -> Optional[float]:
    if payload is None: return None
    if isinstance(payload, (int, float)): return float(payload)
    if isinstance(payload, dict):
        for k in ("price", "last", "mark", "index", "close"):
            v = payload.get(k)
            if v is not None:
                try: return float(v)
                except Exception: pass
    return None

class PxAdapter:
    def __init__(self):
        src_env = env("PRICE_SOURCE", "bybit").strip().lower()
        if src_env in ("hyperliquid", "hl", "hyperliquid_sdk"):
            self.src = create_price_source(name="hyperliquid")
        else:
            self.src = create_price_source(
                name="bybit",
                api_key=env("BYBIT_API_KEY", ""),
                secret_key=env("BYBIT_SECRET", ""),
                testnet=env("BYBIT_TESTNET", "false").lower() in ("1","true","yes","y"),
            )
        try:
            if hasattr(self.src, "ensure_instruments_loaded"):
                self.src.ensure_instruments_loaded()
        except Exception:
            pass

    def normalize(self, symbol: str) -> str:
        return self.src.normalize_symbol(symbol)

    def is_supported(self, symbol: str) -> bool:
        try:
            return self.src.is_supported_symbol(self.normalize(symbol))
        except Exception:
            return False

    def mark(self, symbol: str) -> float:
        sym = self.normalize(symbol)
        cur = self.src.get_current_price(sym)
        px = _get_price_number(cur)
        if px is None:
            cur = self.src.get_current_price(sym)
            px = _get_price_number(cur)
        if px is None:
            raise RuntimeError(f"Could not fetch current price for {symbol} -> {sym}")
        return float(px)
