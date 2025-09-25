# backend/services/sources/hyperliquid_sdk_source.py
from __future__ import annotations

import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Set

import requests

from backend.services.price_source_base import PriceSource

_HAS_SDK = False
try:
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    _HAS_SDK = True
except Exception as _e:
    logging.warning(f"[HL SDK] import failed, will use REST only for candles: {_e}")
    _HAS_SDK = False


class HyperliquidSDKPriceSource(PriceSource):


    REST_INFO_URL = "https://api.hyperliquid.xyz/info"

    def __init__(self, timeout: int = 10, use_testnet: bool = False, skip_ws: bool = True):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._supported: Set[str] = set()


        self._sdk = None
        if _HAS_SDK:
            try:
                base_url = constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL
                self._sdk = Info(base_url, skip_ws=skip_ws)
            except Exception as e:
                logging.warning(f"[HL SDK] init failed, fallback REST-only: {e}")
                self._sdk = None


        self._iv_map_sec = {
            "1": 60, "3": 180, "5": 300, "15": 900,
            "30": 1800, "60": 3600, "120": 7200, "240": 14400, "D": 86400,
        }
        self._iv_map_ms = {
            "1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000,
            "30": 1_800_000, "60": 3_600_000, "120": 7_200_000, "240": 14_400_000, "D": 86_400_000,
        }

    def normalize_symbol(self, raw: str) -> str:

        s = (raw or "").upper().strip()
        for suf in ("USDT", "USD"):
            if s.endswith(suf):
                s = s[:-len(suf)]
                break
        s = s.replace("/", "").replace("-", "")
        return s


    def ensure_instruments_loaded(self) -> None:
        if self._supported:
            return
        mids = self._all_mids()
        if isinstance(mids, dict):
            self._supported = {str(k).upper() for k in mids.keys()}
            logging.info(f"[HL] Loaded {len(self._supported)} symbols")
        else:
            self._supported = set()
            logging.warning("[HL] load instruments failed.")

    def is_supported_symbol(self, symbol: str) -> bool:
        self.ensure_instruments_loaded()
        return self.normalize_symbol(symbol) in self._supported

    #current
    def get_current_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        base = self.normalize_symbol(symbol)
        mids = self._all_mids()
        if not isinstance(mids, dict):
            return None
        px = mids.get(base) or mids.get(base.upper())
        if px is None:
            return None
        try:
            price_f = float(px)
        except Exception:
            return None
        return {
            "symbol": base,
            "price": price_f,
            "timestamp": datetime.now(timezone.utc),
            "market": "perp",
        }
    #kline
    def get_historical_klines(
        self,
        symbol: str,
        interval: str = "1",
        limit: int = 200,           
        category: str = "perp",
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> List[List[Any]]:

        if interval not in self._iv_map_sec:
            raise ValueError(f"Unsupported interval: {interval}")
        if start_ms is None or end_ms is None:
            raise ValueError("start_ms and end_ms are required")

        base = self.normalize_symbol(symbol)
        sec = self._iv_map_sec[interval]
        iv_ms = self._iv_map_ms[interval]

        def _align_minute(ms: int) -> int:
            return int(ms // 60_000 * 60_000)

        s = _align_minute(int(start_ms))
        e = _align_minute(int(end_ms))
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        if e - s < iv_ms:
            e = s + iv_ms

        if now_ms - e < 60_000:
            e = now_ms - 60_000
            if e - s < iv_ms:
                s = e - iv_ms

        def _body(S, E):
            return {
                "type": "candleSnapshot",
                "req": {
                    "coin": base,
                    "interval": int(sec),
                    "startTime": int(S),
                    "endTime": int(E),
                },
            }

        backoffs = [0.05, 0.1, 0.2]  # 秒
        windows = [
            (s, e),                                   
            (max(e - 10 * 60_000, s), e),             # 10 min window
            (max(e - 5 * 60_000, s), e),              # 5 min window
        ]

        last_err = None
        for (S, E) in windows:
            for bo in backoffs:
                try:
                    data = self._post(_body(S, E))
                    rows = self._parse_candles(data, base)
                    if rows:
                        return rows
                    time.sleep(bo)
                except requests.HTTPError as ehttp:
                    code = getattr(getattr(ehttp, "response", None), "status_code", None)
                    if code in (422, 429):
                        logging.warning(f"[HL REST] candleSnapshot HTTP {code} for {base} @{interval}, shrinking window & backoff {bo}s")
                        time.sleep(bo)
                        last_err = ehttp
                        continue
                    raise
                except Exception as e:
                    last_err = e
                    time.sleep(bo)
                    continue

        if last_err:
            logging.warning(f"[HL REST] candleSnapshot fallback exhausted for {base} @{interval}: {last_err}")
        return []

    # ranged kline
    def get_klines_range_chunked(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        category: str,
        limit_per_call: int = 2000,
        sleep_s: float = 0.03,
    ) -> List[List[Any]]:
        if interval not in self._iv_map_ms:
            raise ValueError(f"Unsupported interval: {interval}")
        iv_ms = self._iv_map_ms[interval]
        out: List[List[Any]] = []
        t0, t1 = int(start_ms), int(end_ms)
        window_ms = iv_ms * limit_per_call

        while t0 < t1:
            seg_end = min(t0 + window_ms, t1)
            rows = self.get_historical_klines(symbol, interval, category="perp", start_ms=t0, end_ms=seg_end)
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
        category: str = "perp",
        use_open: bool = True,
        window_minutes: int = 30,
    ) -> Optional[float]:
        """
        从 [at - window, at + window] 的 1m K 线里找到与 at 最接近的一根，返回 open 或 close。
        失败时不抛异常，返回 None（由上层决定是否用当前价兜底）。
        """
        base = self.normalize_symbol(symbol)
        target = self._ms(at)

        def _pick(kl):
            if not kl:
                return None
            picked = min(kl, key=lambda row: abs(int(row[0]) - target))
            o, c = float(picked[1]), float(picked[4])
            return o if use_open else c

        for m in (window_minutes, 10, 5, 2):
            start = target - m * 60_000
            end = target + m * 60_000
            kl = self.get_historical_klines(base, interval="1", start_ms=start, end_ms=end, category="perp")
            price = _pick(kl)
            if price is not None:
                return price

        return None

    def _ms(self, dt_obj: Any) -> int:
        dt = dt_obj
        if hasattr(dt, "to_pydatetime"):
            dt = dt.to_pydatetime()
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    def _parse_candles(self, data: Any, base: str) -> List[List[Any]]:

        rows: List[List[Any]] = []
        payload = None

        if isinstance(data, dict):
            if "candles" in data:
                c = data["candles"]
                if isinstance(c, list):
                    payload = c
                elif isinstance(c, dict):
                    payload = c.get("data") or c.get(base) or c.get(base.upper())
            elif base in data:
                payload = data.get(base)
            elif base.upper() in data:
                payload = data.get(base.upper())

        if not payload:
            return rows

        for r in payload:
            try:
                ts = int(r[0])
                o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                v = float(r[5]) if len(r) > 5 else 0.0
                rows.append([ts, o, h, l, c, v])
            except Exception:
                continue
        return rows

    def _all_mids(self) -> Optional[Dict[str, float]]:

        if self._sdk is not None:
            try:
                if hasattr(self._sdk, "all_mids"):
                    mids = self._sdk.all_mids()   
                else:
                    mids = self._sdk.allMids()     
                return {str(k).upper(): float(v) for k, v in mids.items()}
            except Exception as e:
                logging.warning(f"[HL SDK] all_mids failed, fallback REST: {e}")

        backoffs = [0.02, 0.05, 0.1, 0.2]
        last_err = None
        for bo in backoffs:
            try:
                resp = self.session.post(self.REST_INFO_URL, json={"type": "allMids"}, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict):
                    return {str(k).upper(): float(v) for k, v in data.items()}
                return None
            except Exception as e:
                last_err = e
                time.sleep(bo)
                continue
        if last_err:
            logging.error(f"[HL REST] allMids error: {last_err}")
        return None

    def _post(self, body: Dict[str, Any]) -> Any:
        resp = self.session.post(self.REST_INFO_URL, json=body, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()


HyperliquidPriceSource = HyperliquidSDKPriceSource

#test
if __name__ == "__main__":
    from datetime import timedelta

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    src = HyperliquidSDKPriceSource()
    src.ensure_instruments_loaded()
    print("BTC supported:", src.is_supported_symbol("BTC"))
    print("current:", src.get_current_price("BTC"))

    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=30)
    rows = src.get_historical_klines(
        "BTC", interval="1",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(now.timestamp() * 1000),
    )
    print("klines n=", len(rows))
    print("first:", rows[0] if rows else None)
    print("last :", rows[-1] if rows else None)

    t = now - timedelta(minutes=3)
    print("price@T open:", src.get_price_at("BTC", t, use_open=True))
    print("price@T close:", src.get_price_at("BTC", t, use_open=False))
