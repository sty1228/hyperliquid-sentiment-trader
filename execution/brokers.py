from __future__ import annotations

import logging
from typing import TypedDict, Optional

from execution.px_adapter import PxAdapter

log = logging.getLogger(__name__)


class BrokerAck(TypedDict):
    broker_order_id: str
    status: str  # 'ack' | 'rejected'
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


# ─── Sim broker for testing ──────────────────────────────
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


# ─── Real HyperLiquid broker ────────────────────────────
class HyperliquidBroker(Broker):
    """
    Places real orders on HyperLiquid via the Python SDK.
    Attaches builder code to every order so we earn fees.

    Args:
        account_address: Your main HL wallet address (the one with ≥100 USDC)
        api_secret_key:  Private key of the API wallet (generated on HL API page)
        builder_address: Builder address to receive fees (usually same as account_address)
        builder_fee_bps: Fee in tenths of basis points (10 = 1bp = 0.01%)
        mainnet:         True for mainnet, False for testnet
    """

    def __init__(
        self,
        account_address: str,
        api_secret_key: str,
        builder_address: str,
        builder_fee_bps: int = 10,
        mainnet: bool = True,
        px: PxAdapter | None = None,
    ):
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants

        self.account_address = account_address
        self.builder_address = builder_address
        self.builder_fee_bps = builder_fee_bps
        self.px = px or PxAdapter()

        # Create wallet from API wallet private key
        self.wallet = Account.from_key(api_secret_key)

        # Connect to HL — agent wallet acts on behalf of account_address
        base_url = constants.MAINNET_API_URL if mainnet else constants.TESTNET_API_URL
        self.exchange = Exchange(
            self.wallet,
            base_url,
            account_address=account_address,
        )

        log.info(
            "HyperliquidBroker initialized | account=%s | builder=%s | fee=%dbps/10 | net=%s",
            account_address[:10],
            builder_address[:10],
            builder_fee_bps,
            "mainnet" if mainnet else "testnet",
        )

    def _normalize_coin(self, symbol: str) -> str:
        """Strip quote currency: 'BTCUSDT' → 'BTC', 'ETH' → 'ETH'"""
        s = symbol.upper().strip()
        for suffix in ("USDT", "USDC", "USD", "PERP"):
            if s.endswith(suffix) and len(s) > len(suffix):
                return s[: -len(suffix)]
        return s

    def place_market(
        self,
        symbol: str,
        side: str,
        qty: float,
        tif: str = "IOC",
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> BrokerAck:
        coin = self._normalize_coin(symbol)
        is_buy = side.lower() in ("buy", "long")

        builder = {"b": self.builder_address, "f": self.builder_fee_bps}

        log.info(
            "HL order | coin=%s side=%s qty=%.6f reduce_only=%s builder=%s",
            coin, side, qty, reduce_only, self.builder_address[:10],
        )

        try:
            if reduce_only:
                # market_close for reducing positions
                result = self.exchange.market_close(
                    coin=coin,
                    sz=qty,
                    builder=builder,
                )
            else:
                # market_open for new positions
                result = self.exchange.market_open(
                    coin=coin,
                    is_buy=is_buy,
                    sz=qty,
                    builder=builder,
                )

            # Parse response
            status_data = result.get("response", {}).get("data", {})
            statuses = status_data.get("statuses", [])

            if statuses and "filled" in statuses[0]:
                fill = statuses[0]["filled"]
                return {
                    "broker_order_id": str(fill.get("oid", "")),
                    "status": "ack",
                    "detail": {
                        "fill_px": float(fill.get("avgPx", 0)),
                        "qty": float(fill.get("totalSz", qty)),
                        "side": side,
                        "coin": coin,
                        "builder": builder,
                    },
                }
            elif statuses and "error" in statuses[0]:
                error_msg = statuses[0]["error"]
                log.warning("HL order rejected: %s", error_msg)
                return {
                    "broker_order_id": "",
                    "status": "rejected",
                    "detail": {"error": error_msg, "raw": result},
                }
            else:
                # Unexpected response format
                log.warning("HL unexpected response: %s", result)
                return {
                    "broker_order_id": "",
                    "status": "rejected",
                    "detail": {"error": "unexpected response", "raw": result},
                }

        except Exception as e:
            log.exception("HL order failed: %s", e)
            return {
                "broker_order_id": "",
                "status": "rejected",
                "detail": {"error": str(e)},
            }