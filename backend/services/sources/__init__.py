
from __future__ import annotations

def create_price_source(name: str, **kwargs):
    n = (name or "").strip().lower()

    if n == "bybit":
        from .bybit_source import BybitPriceSource
        return BybitPriceSource(**kwargs)

    if n in ("hyperliquid", "hl", "hyperliquid_sdk"):
        from .hyperliquid_sdk_source import HyperliquidSDKPriceSource as HyperliquidPriceSource
        return HyperliquidPriceSource(**kwargs)

    raise ValueError(f"Unknown price source: '{name}'. options：bybit, hyperliquid")
