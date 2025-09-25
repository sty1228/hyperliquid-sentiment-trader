
from __future__ import annotations
import time
import hmac
import hashlib
import logging
from urllib.parse import urlencode
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Set

import requests

from backend.services.price_source_base import PriceSource


class BybitPriceSource(PriceSource):

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        testnet: bool = False,
        recv_window: int = 5000,
        timeout: int = 10,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        self.recv_window = str(recv_window)
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        self._spot_symbols: Set[str] = set()
        self._linear_symbols: Set[str] = set()
        self._instruments_loaded = False

        self._interval_ms = {
            "1": 60_000,
            "3": 180_000,
            "5": 300_000,
            "15": 900_000,
            "30": 1_800_000,
            "60": 3_600_000,
            "120": 7_200_000,
            "240": 14_400_000,
            "D": 86_400_000,
        }


    def normalize_symbol(self, symbol: str) -> str:
        s = (symbol or "").upper().strip()
        for q in ("USDT", "USDC", "USD"):
            if s.endswith(q):
                return s
        return s + "USDT"

    def is_supported_symbol(self, symbol: str) -> bool:
        self.ensure_instruments_loaded()
        s = self.normalize_symbol(symbol)
        return (s in self._spot_symbols) or (s in self._linear_symbols)

    def _generate_signature(self, timestamp: str, params_str: str) -> str:
        """
        SIGN-TYPE=2: sha256( timestamp + api_key + recv_window + query_string )
        """
        message = f"{timestamp}{self.api_key}{self.recv_window}{params_str}"
        return hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _make_request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        method: str = "GET",
    ) -> Optional[Dict[str, Any]]:
        params = params or {}
        try:
            params_str = urlencode(sorted((k, v) for k, v in params.items() if v is not None))
        except Exception as e:
            logging.error(f"[Bybit] encode params error {params}: {e}")
            params_str = ""

        timestamp = str(int(time.time() * 1000))
        signature = self._generate_signature(timestamp, params_str)

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
        }

        url = f"{self.base_url}{endpoint}"
        try:
            if method.upper() == "GET":
                if params_str:
                    url = f"{url}?{params_str}"
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            else:
                resp = self.session.post(url, headers=headers, json=params, timeout=self.timeout)

            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("retCode", 0) != 0:
                logging.warning(
                    f"[Bybit] non-zero retCode: {data.get('retCode')} - {data.get('retMsg')} "
                    f"(endpoint={endpoint}, params={params})"
                )
            return data
        except requests.HTTPError as e:
            text = getattr(e.response, "text", "")
            logging.error(f"[Bybit] HTTP error for {url}: {e} | {text}")
        except Exception as e:
            logging.error(f"[Bybit] request failed for {url}: {e}")
        return None

    def _ms(self, dt_obj: Any) -> int:
        """
        datetime/pandas.Timestamp -> epoch ms（naive 视为 UTC）
        """
        dt = dt_obj
        if hasattr(dt, "to_pydatetime"):
            dt = dt.to_pydatetime()
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    # Instruments
    def ensure_instruments_loaded(self) -> None:
        if self._instruments_loaded:
            return
        self._spot_symbols = self._fetch_instruments(category="spot")
        self._linear_symbols = self._fetch_instruments(category="linear")
        self._instruments_loaded = True
        logging.info(f"[Bybit] Loaded instruments: spot={len(self._spot_symbols)}, linear={len(self._linear_symbols)}")

    def _fetch_instruments(self, category: str) -> Set[str]:
        out: Set[str] = set()
        cursor = None
        while True:
            params = {"category": category}
            if cursor:
                params["cursor"] = cursor
            data = self._make_request("/v5/market/instruments-info", params)
            if not data or data.get("retCode") != 0:
                break
            result = data.get("result", {}) or {}
            rows = result.get("list") or []
            for r in rows:
                sym = (r.get("symbol") or "").upper().strip()
                if sym:
                    out.add(sym)
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
        return out


    def get_current_price(self, symbol: str) -> Optional[Dict[str, Any]]:

        sym = self.normalize_symbol(symbol)

        # spot
        result = self._make_request("/v5/market/tickers", {"category": "spot", "symbol": sym})
        if result and result.get("retCode") == 0 and result.get("result", {}).get("list"):
            item = result["result"]["list"][0]
            return {
                "symbol": sym,
                "price": float(item["lastPrice"]),
                "timestamp": datetime.now(timezone.utc),
                "market": "spot",
            }

        # linear fallback
        result = self._make_request("/v5/market/tickers", {"category": "linear", "symbol": sym})
        if result and result.get("retCode") == 0 and result.get("result", {}).get("list"):
            item = result["result"]["list"][0]
            return {
                "symbol": sym,
                "price": float(item["lastPrice"]),
                "timestamp": datetime.now(timezone.utc),
                "market": "linear",
            }

        logging.warning(f"[Bybit] No ticker found for {sym} (spot/linear)")
        return None

    def get_historical_klines(
        self,
        symbol: str,
        interval: str = "1",
        limit: int = 200,
        category: str = "spot",
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> List[List[Any]]:

        sym = self.normalize_symbol(symbol)
        params: Dict[str, Any] = {
            "category": category,
            "symbol": sym,
            "interval": interval,
            "limit": limit,
        }
        if start_ms is not None:
            params["start"] = int(start_ms)
        if end_ms is not None:
            params["end"] = int(end_ms)

        result = self._make_request("/v5/market/kline", params)
        if result and result.get("retCode") == 0:
            return result.get("result", {}).get("list", []) or []
        return []

    def get_klines_range_chunked(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        category: str,
        limit_per_call: int = 2000,
        sleep_s: float = 0.02,
    ) -> List[List[Any]]:

        sym = self.normalize_symbol(symbol)
        iv_ms = self._interval_ms.get(interval)
        if not iv_ms:
            raise ValueError(f"Unsupported interval: {interval}")

        out: List[List[Any]] = []
        t0 = int(start_ms)
        t1 = int(end_ms)

        window_ms = iv_ms * limit_per_call

        while t0 < t1:
            seg_end = min(t0 + window_ms, t1)
            rows = self.get_historical_klines(
                sym, interval=interval, limit=limit_per_call, category=category, start_ms=t0, end_ms=seg_end
            )
            if not rows:
                t0 = seg_end
                continue
            out.extend(rows)
            last_ts = int(rows[-1][0])
            t0 = max(seg_end, last_ts + iv_ms)
            time.sleep(sleep_s)

        seen = set()
        dedup: List[List[Any]] = []
        for r in sorted(out, key=lambda x: int(x[0])):
            ts = int(r[0])
            if ts in seen:
                continue
            seen.add(ts)
            dedup.append(r)
        return dedup

    def choose_interval_for_horizon(self, hours: int) -> str:

        if hours <= 33:
            return "1"
        if hours <= 166:
            return "5"
        if hours <= 500:
            return "15"
        return "60"


    def get_price_at(
        self,
        symbol: str,
        at: datetime,
        category: str = "spot",
        use_open: bool = True,
        window_minutes: int = 10,
    ) -> Optional[float]:

        sym = self.normalize_symbol(symbol)
        target_ms = self._ms(at)
        start = target_ms - window_minutes * 60 * 1000
        end = target_ms + window_minutes * 60 * 1000

        def _pick_price(kl: List[List[Any]]) -> Optional[float]:
            if not kl:
                return None
            picked = min(kl, key=lambda row: abs(int(row[0]) - target_ms))
            open_p, close_p = float(picked[1]), float(picked[4])
            return open_p if use_open else close_p

        for cat in (category, "linear" if category != "linear" else "spot"):
            rows = self.get_historical_klines(sym, interval="1", limit=200, category=cat, start_ms=start, end_ms=end)
            price = _pick_price(rows)
            if price is not None:
                return price
        return None


BybitService = BybitPriceSource

# test
if __name__ == "__main__":
    src = BybitPriceSource(api_key="", secret_key="", testnet=False)
    src.ensure_instruments_loaded()
    print("BTC supported:", src.is_supported_symbol("BTC"))
    print("BTC ticker:", src.get_current_price("BTC"))
