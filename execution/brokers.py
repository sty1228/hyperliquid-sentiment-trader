
from __future__ import annotations

from typing import TypedDict, Optional

from execution.px_adapter import PxAdapter


class BrokerAck(TypedDict):
    broker_order_id: str
    status: str         # 'ack' | 'rejected'
    detail: dict


class Broker:

    def place_market(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str = "IOC",
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> BrokerAck:
        raise NotImplementedError

# sim broker for testing
class SimBroker(Broker):

    def __init__(self, px: PxAdapter | None = None):
        self.px = px or PxAdapter()

    def place_market(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str = "IOC",
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> BrokerAck:
        fill_px = self.px.mark(symbol)
        return {
            "broker_order_id": f"sim-{client_order_id or 'noid'}",
            "status": "ack",
            "detail": {
                "fill_px": float(fill_px),
                "qty": float(qty),
                "side": side,
                "reduce_only": bool(reduce_only),
                "tif": tif,
            },
        }

# later need to do: hyperliquid broker for real trading
class HyperliquidBroker(Broker):

    def __init__(self, builder_code: str, key: str, px: PxAdapter | None = None):
        self.builder_code = builder_code
        self.key = key
        self.px = px or PxAdapter()

    def place_market(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str = "IOC",
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> BrokerAck:
        raise NotImplementedError("HyperliquidBroker.place_market() not implemented yet")
