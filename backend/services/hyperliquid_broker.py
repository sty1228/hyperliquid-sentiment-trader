
from __future__ import annotations

import os
import re
import math
import traceback
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Any, Tuple, List

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator


HL_DEBUG = os.getenv("HL_DEBUG", "0").strip().lower() in ("1", "true", "yes", "y", "on")

HL_IMPORT_ERROR: Optional[str] = None
try:
    from hyperliquid.exchange import Exchange  
    from hyperliquid.info import Info         
    from hyperliquid.utils import types as hl_types 
    from hyperliquid.utils import signing as hl_signing 
except Exception as e1:
    try:
        from hyperliquid import Exchange, Info  
        hl_types = None  
        hl_signing = None  
    except Exception as e2:
        Exchange = None 
        Info = None     
        hl_types = None  
        hl_signing = None  
        HL_IMPORT_ERROR = f"pathA={repr(e1)}; pathB={repr(e2)}"

try:
    from eth_account import Account  
    from eth_account.signers.local import LocalAccount  
except Exception as e3:
    Account = None  
    LocalAccount = None  
    HL_IMPORT_ERROR = (HL_IMPORT_ERROR + "; " if HL_IMPORT_ERROR else "") + f"eth_account={repr(e3)}"

router = APIRouter(prefix="/api/hl", tags=["hyperliquid"])


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    if v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def _normalize_addr(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    v2 = v.strip()
    if re.fullmatch(r"0x[a-fA-F0-9]{40}", v2):
        return v2
    return None


def _normalize_pk(pk: str) -> str:
    pk2 = pk.strip()
    if not pk2:
        return pk2
    return pk2 if pk2.lower().startswith("0x") else "0x" + pk2


def _json_safe(x: Any) -> Any:
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


@dataclass
class HLConfig:
    mainnet: bool
    account_address: str
    api_secret_key: str  # private key (hex)
    builder_address: Optional[str]
    default_leverage: int
    default_builder_bps: int
    base_url_override: Optional[str]


def load_cfg() -> HLConfig:
    return HLConfig(
        mainnet=_env_bool("HL_MAINNET", False),
        account_address=os.getenv("HL_ACCOUNT_ADDRESS", "").strip(),
        api_secret_key=os.getenv("HL_API_SECRET_KEY", "").strip(),
        builder_address=_normalize_addr(os.getenv("HL_BUILDER_ADDRESS", "")),
        default_leverage=_env_int("HL_DEFAULT_LEVERAGE", 5),
        default_builder_bps=_env_int("HL_DEFAULT_BUILDER_BPS", 0),
        base_url_override=(os.getenv("HL_BASE_URL", "") or "").strip() or None,
    )


# Broker
@dataclass
class _Attempt:
    variant: str
    payload_summary: Dict[str, Any]
    ok: bool
    error: Optional[str] = None
    resp_preview: Optional[Any] = None


class HyperliquidBroker:


    def __init__(self, cfg: HLConfig):
        if not cfg.account_address or not cfg.api_secret_key:
            raise ValueError("Missing HL_ACCOUNT_ADDRESS or HL_API_SECRET_KEY in environment.")

        self.cfg = cfg
        self.base_url = (
            cfg.base_url_override
            or ("https://api.hyperliquid.xyz" if self.cfg.mainnet else "https://api.hyperliquid-testnet.xyz")
        )
        if not self.base_url.startswith("http"):
            raise RuntimeError(f"bad base_url={self.base_url}")
        print(f"[HL:init] mainnet={self.cfg.mainnet} base_url={self.base_url}")

        self._info: Optional[Any] = None
        self.ex: Optional[Any] = None
        self._ex_err: Optional[str] = None

        if Exchange is not None:
            try:
                addr = _normalize_addr(self.cfg.account_address)
                if not addr:
                    raise ValueError(f"HL_ACCOUNT_ADDRESS invalid: {self.cfg.account_address!r}")

                if Account is None:
                    raise RuntimeError("eth-account is not importable; install `eth-account`.")

                pk = _normalize_pk(self.cfg.api_secret_key)
                try:
                    acct: LocalAccount = Account.from_key(pk)
                except Exception as e:
                    raise RuntimeError(f"Account.from_key failed: {e}")

                attempts = [
                    ("pos (acct, base_url)", lambda: Exchange(acct, self.base_url)),
                    ("pos (acct, base_url, skip_ws=False)", lambda: Exchange(acct, self.base_url, False)),
                    ("pos (acct)", lambda: Exchange(acct)),
                    ("pos (acct, addr, base_url)", lambda: Exchange(acct, addr, self.base_url)),
                ]
                last_err = None
                for tag, ctor in attempts:
                    try:
                        self.ex = ctor()
                        print(f"[HL:exchange] init ok via: {tag}")
                        self._ex_err = None
                        break
                    except Exception as e:
                        last_err = e
                        self._ex_err = f"{tag}: {repr(e)}"

                if self.ex is None:
                    raise RuntimeError(self._ex_err or repr(last_err))

                try:
                    _ = self._get_info_health_preview()
                except Exception as le:
                    self._ex_err = f"liveness check warn: {le!r}"
            except Exception as e:
                self._ex_err = repr(e)

        print(f"[HL:dep] addr={self.cfg.account_address} base_url={self.base_url}")

        try:
            self._order_sig = str(getattr(Exchange, "order"))
        except Exception:
            self._order_sig = "unknown"

    def _info_lazy(self):
        if self._info is not None or Info is None:
            return self._info
        last_err: Optional[Exception] = None
        for attempt in (
            lambda: Info(base_url=self.base_url, skip_ws=True),
            lambda: Info(base_url=self.base_url),
        ):
            try:
                self._info = attempt()
                return self._info
            except Exception as e:
                last_err = e
        raise RuntimeError(f"Init Info failed: {repr(last_err)}")

    def _post_info(self, payload: dict) -> Any:
        url = self.base_url.rstrip("/") + "/info"
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            raise RuntimeError(f"POST {url} failed: {r.status_code} {r.text[:200]}")
        return r.json()

    def _get_info_health_preview(self) -> str:
        url = self.base_url.rstrip("/") + "/info"
        try:
            r = requests.get(url, timeout=6)  # may 405
            return f"{r.status_code} {r.text[:80]}"
        except Exception as e:
            raise RuntimeError(f"GET {url} error: {e}")

    def all_mids(self) -> Dict[str, float]:
        if Info is not None:
            try:
                info = self._info_lazy()
                if info is not None:
                    if hasattr(info, "all_mids"):
                        mids = info.all_mids()
                        return {str(k).upper(): float(v) for k, v in mids.items()}
                    if hasattr(info, "allMids"):
                        mids = info.allMids()
                        return {str(k).upper(): float(v) for k, v in mids.items()}
            except Exception as e:
                print(f"[HL:mids] SDK path failed: {e}")

        data = self._post_info({"type": "allMids"})
        if isinstance(data, dict):
            for key in (None, "data", "result", "mids"):
                obj = data if key is None else data.get(key)
                if isinstance(obj, dict):
                    try:
                        return {str(k).upper(): float(v) for k, v in obj.items()}
                    except Exception:
                        continue
        raise RuntimeError(f"Unexpected /info allMids response shape: {str(data)[:200]}")

    def _mid_for_symbol(self, symbol: str, mids: Dict[str, float]) -> Tuple[str, float]:
        s = symbol.upper().strip()
        base = re.sub(r"(USDT|USD|-PERP|-USD)$", "", s)
        if base in mids:
            return base, mids[base]
        if s in mids:
            return s, mids[s]
        raise ValueError(f"Unknown symbol {symbol!r}; mids keys: {list(mids.keys())[:6]}")

    def _qty_from_usd(self, usd: float, px: float) -> float:
        if px <= 0:
            raise ValueError("reference price must be > 0")
        return max(float(usd) / float(px), 1e-12)

    def _build_order_type_variants(self, kind: str, tif: str = "Gtc"):

        out = []
        k = (kind or "market").strip().lower()
        if hl_signing is not None and hasattr(hl_signing, "OrderType"):
            try:
                if k == "market" and hasattr(hl_signing.OrderType, "Market"):
                    out.append(("sdk_signing_market", hl_signing.OrderType.Market(), False))
                elif k == "limit" and hasattr(hl_signing.OrderType, "Limit"):
                    out.append(("sdk_signing_limit", hl_signing.OrderType.Limit(tif), True))
            except Exception:
                pass

        if hl_types is not None and hasattr(hl_types, "OrderType"):
            try:
                if k == "market" and hasattr(hl_types.OrderType, "Market"):
                    out.append(("sdk_types_market", hl_types.OrderType.Market(), False))
                elif k == "limit" and hasattr(hl_types.OrderType, "Limit"):
                    out.append(("sdk_types_limit", hl_types.OrderType.Limit(tif), True))
            except Exception:
                pass

        if not out:
            if k == "market":
                out.append(("dict_market", {"market": {}}, False))
            elif k == "limit":
                out.append(("dict_limit", {"limit": {"tif": tif}}, True))
            else:
                raise ValueError("order_type must be 'market' or 'limit'")
        return out

    def place_order(
        self,
        symbol: str,
        is_buy: bool,
        usd_notional: float,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        client_id: Optional[str] = None,
        builder_address: Optional[str] = None,
        builder_bps: Optional[int] = None,
        reduce_only: bool = False,
        leverage: Optional[int] = Field(None, ge=1, le=100),  # kept for interface parity
        want_debug: bool = False,
    ) -> Dict[str, Any]:
        if self.ex is None:
            raise RuntimeError("Exchange client not available. Ensure hyperliquid-python-sdk and eth-account are installed.")

        mids = self.all_mids()
        coin, mid = self._mid_for_symbol(symbol, mids)

        kind = (order_type or "market").lower()
        if kind == "limit":
            if limit_price is None or float(limit_price) <= 0:
                raise ValueError("limit_price must be positive for limit orders")
            ref_px = float(limit_price)
            limit_px_val: Optional[float] = float(limit_price)
        else:
            ref_px = float(mid)
            limit_px_val = None 

        if not math.isfinite(ref_px) or ref_px <= 0:
            raise ValueError("Bad reference price for sizing.")

        sz = round(self._qty_from_usd(usd_notional, ref_px), 8)
        if sz <= 0:
            raise ValueError("Computed size <= 0")

        builder = None
        _baddr = _normalize_addr(builder_address) or self.cfg.builder_address
        if HL_DEBUG:
            print("[HL:env] HL_BUILDER_ADDRESS=", os.environ.get("HL_BUILDER_ADDRESS"))
            print("[HL:env] HL_DEFAULT_BUILDER_BPS=", os.environ.get("HL_DEFAULT_BUILDER_BPS"))
            print("[HL:env] HL_ACCOUNT_ADDRESS=", os.environ.get("HL_ACCOUNT_ADDRESS"))
            print("[HL:env] HL_API_SECRET_KEY=", os.environ.get("HL_API_SECRET_KEY"))
            print(f"[HL:builder] builder_address={builder_address} cfg.builder_address={self.cfg.builder_address} builder_bps={builder_bps} _baddr={_baddr}")

        if _baddr is not None and builder_bps is not None:
            try:
                bps_int = int(builder_bps)
                if isinstance(bps_int, int) and bps_int > 0:
                    builder = {"b": _baddr, "f": bps_int * 10}
            except Exception as e:
                if HL_DEBUG:
                    print(f"[HL:builder] Invalid builder_bps: {builder_bps}, error: {e}")
    
        if not (isinstance(builder, dict) or builder is None):
            if HL_DEBUG:
                print(f"[HL:builder] Invalid builder type: {type(builder)}, value: {builder}")
            builder = None
        if HL_DEBUG:
            print(f"[HL:builder] FINAL builder={builder} (type={type(builder)})")
        assert (builder is None or isinstance(builder, dict)), f"Builder must be dict or None, got {type(builder)}: {builder}"

        cloid = client_id or None

        attempts: List[_Attempt] = []
        try:
            
            for variant_name, ot_val, needs_limit in self._build_order_type_variants(order_type):
                payload_summary = {
                    "variant": variant_name,
                    "name": coin,
                    "is_buy": bool(is_buy),
                    "sz": sz,
                    "limit_px": _json_safe(limit_px_val if needs_limit else None),
                    "reduce_only": bool(reduce_only),
                    "cloid": cloid,
                    "builder_present": bool(builder),
                }
                try:
                   
                    if variant_name.startswith("dict_"):
                        if needs_limit:
                            args = [coin, bool(is_buy), sz, float(limit_px_val)]
                        else:
                            args = [coin, bool(is_buy), sz]
                        resp = self.ex.order(*args, order_type=ot_val, reduce_only=bool(reduce_only), cloid=cloid, builder=builder)  # type: ignore[attr-defined]
                    else:
                        order_kwargs = dict(
                            name=coin,
                            is_buy=bool(is_buy),
                            sz=sz,
                            order_type=ot_val,
                            reduce_only=bool(reduce_only),
                            cloid=cloid,
                            builder=builder,
                        )
                        if needs_limit and limit_px_val is not None:
                            order_kwargs["limit_px"] = float(limit_px_val)
                        resp = self.ex.order(**order_kwargs)  
                    attempts.append(_Attempt(variant=variant_name, payload_summary=payload_summary, ok=True, resp_preview=resp))
                    result = {
                        "ok": True,
                        "order_id": getattr(resp, "oid", None) or getattr(resp, "id", None) or resp,
                    }
                    if want_debug or HL_DEBUG:
                        result["debug"] = {
                            "sdk_order_signature": self._order_sig,
                            "mid": mid,
                            "qty": sz,
                            "limit_px": _json_safe(limit_px_val),
                            "order_type_requested": order_type,
                            "attempts": [asdict(a) for a in attempts],
                            "builder": builder,
                        }
                    return result
                except TypeError as te:
                    attempts.append(_Attempt(variant=variant_name, payload_summary=payload_summary, ok=False, error=f"TypeError: {te}"))
                except Exception as e:
                    attempts.append(_Attempt(
                        variant=variant_name,
                        payload_summary=payload_summary,
                        ok=False,
                        error=f"{type(e).__name__}: {e}",
                        resp_preview=getattr(e, "args", None),
                    ))
            err = {"ok": False, "error": "All order variants failed"}
            if want_debug or HL_DEBUG:
                err["debug"] = {
                    "sdk_order_signature": self._order_sig,
                    "mid": mid,
                    "qty": sz,
                    "limit_px": _json_safe(limit_px_val),
                    "order_type_requested": order_type,
                    "attempts": [asdict(a) for a in attempts],
                    "traceback": traceback.format_exc(limit=2),
                }
            return err
        except Exception as e:
            err = {"ok": False, "error": str(e)}
            if want_debug or HL_DEBUG:
                err["debug"] = {
                    "traceback": traceback.format_exc(limit=2),
                }
            return err

    def approve_builder_fee(self, user_main_address: str, builder_address: str, max_fee_bps: int) -> Dict[str, Any]:
        if self.ex is None:
            raise RuntimeError("Exchange client not available.")
        builder_addr_norm = _normalize_addr(builder_address)
        if not builder_addr_norm:
            raise RuntimeError("Invalid builder_address (expect 0x...)")
        try:
            resp = self.ex.approve_builder_fee(builder_addr_norm, int(max_fee_bps))  
            return {"ok": True, "resp": resp}
        except AttributeError as e:
            raise RuntimeError("SDK does not expose approve_builder_fee(). Please upgrade hyperliquid-python-sdk. ({})".format(e))

    def cancel_by_client_id(self, client_id: str) -> Dict[str, Any]:
        if self.ex is None:
            raise RuntimeError("Exchange client not available.")
        try:
            try:
                resp = self.ex.cancel(cloid=client_id)  
            except TypeError:
                resp = self.ex.cancel(client_id)      
            return {"ok": True, "resp": resp}
        except Exception as e:
            raise RuntimeError(f"cancel error: {e}")

    # Diagnostics snapshot
    def _diag(self) -> Dict[str, Any]:
        info_methods = {}
        if self._info is not None:
            info_methods["has_allMids"] = bool(getattr(self._info, "allMids", None))
            info_methods["has_all_mids"] = bool(getattr(self._info, "all_mids", None))
        else:
            info_methods["has_allMids"] = False
            info_methods["has_all_mids"] = False

        return {
            "base_url": self.base_url,
            "sdk_has_Info": Info is not None,
            "sdk_has_Exchange": Exchange is not None,
            "import_error": HL_IMPORT_ERROR,
            **info_methods,
            "cfg_mainnet": self.cfg.mainnet,
            "builder_addr": self.cfg.builder_address,
            "default_leverage": self.cfg.default_leverage,
            "default_builder_bps": self.cfg.default_builder_bps,
            "exchange_ready": self.ex is not None,
            "exchange_init_error": self._ex_err,
            "order_signature": getattr(self, "_order_sig", "unknown"),
        }

# API
class OrderReq(BaseModel):
    symbol: str = Field(..., description="e.g. BTC or BTCUSDT")
    side: str = Field(..., description="buy | sell")
    usd_notional: float = Field(..., gt=0)
    order_type: str = Field("market", description="market | limit")
    limit_price: Optional[float] = Field(None, gt=0)
    client_id: Optional[str] = None
    builder_address: Optional[str] = None
    builder_bps: Optional[int] = Field(None, ge=0, le=10_000)
    reduce_only: bool = False
    leverage: Optional[int] = Field(None, ge=1, le=100)

    @field_validator("side")
    @classmethod
    def _v_side(cls, v: str) -> str:
        v2 = v.lower().strip()
        if v2 not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        return v2

    @field_validator("order_type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        v2 = v.lower().strip()
        if v2 not in ("market", "limit"):
            raise ValueError("order_type must be market or limit")
        return v2


class ApproveReq(BaseModel):
    user_main_address: str = Field(..., description="用户主钱包地址（签名钱包）")
    builder_address: str = Field(..., description="Builder 主体地址")
    max_fee_bps: int = Field(..., ge=0, le=10_000, description="最大可收费用（bps）")


class CancelReq(BaseModel):
    client_id: str



def _broker_dep() -> HyperliquidBroker:
    try:
        return HyperliquidBroker(load_cfg())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"broker init error: {e}")


@router.get("/_env")
def api_hl_env():
    cfg = load_cfg()
    base_url = cfg.base_url_override or ("https://api.hyperliquid.xyz" if cfg.mainnet else "https://api.hyperliquid-testnet.xyz")
    return {
        "ok": True,
        "mainnet": cfg.mainnet,
        "base_url": base_url,
        "has_keys": bool(cfg.account_address) and bool(cfg.api_secret_key),
        "builder_addr": cfg.builder_address,
        "default_leverage": cfg.default_leverage,
        "default_builder_bps": cfg.default_builder_bps,
    }


@router.get("/_ping")
def api_hl_ping():
    cfg = load_cfg()
    base_url = cfg.base_url_override or ("https://api.hyperliquid.xyz" if cfg.mainnet else "https://api.hyperliquid-testnet.xyz")
    url = base_url.rstrip("/") + "/info"
    try:
        r = requests.get(url, timeout=6)
        return {"ok": r.ok, "status": r.status_code, "url": url, "preview": r.text[:200]}
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}


@router.get("/_diag")
def api_diag(b: HyperliquidBroker = Depends(_broker_dep)):
    try:
        b._info_lazy()
    except Exception:
        pass
    return {"ok": True, "sdk": b._diag()}


@router.get("/mids")
def api_mids(b: HyperliquidBroker = Depends(_broker_dep)):
    try:
        mids = b.all_mids()
        return {"ok": True, "mids": mids, "network": "mainnet" if b.cfg.mainnet else "testnet"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"mids error: {e}")


@router.post("/order")
def api_order(
    req: OrderReq,
    b: HyperliquidBroker = Depends(_broker_dep),
    debug: bool = Query(False, description="Set true or use HL_DEBUG=1 to include verbose diagnostics"),
):
    try:
        is_buy = req.side.lower() == "buy"
        out = b.place_order(
            symbol=req.symbol,
            is_buy=is_buy,
            usd_notional=req.usd_notional,
            order_type=req.order_type,
            limit_price=req.limit_price,
            client_id=req.client_id,
            builder_address=req.builder_address or b.cfg.builder_address,
            builder_bps=(req.builder_bps if req.builder_bps is not None else b.cfg.default_builder_bps or None),
            reduce_only=req.reduce_only,
            leverage=req.leverage,
            want_debug=debug,
        )
        if not out.get("ok"):
            payload = (
                {"detail": "order error", "context": out}
                if (debug or HL_DEBUG)
                else {"detail": f"order error: {out.get('error')}"}
            )
            return JSONResponse(status_code=400, content=payload)
        return out
    except Exception as e:
        tb = traceback.format_exc()
        payload = {"detail": f"order error: {e}", "traceback": tb} if (debug or HL_DEBUG) else {"detail": f"order error: {e}"}
        return JSONResponse(status_code=500, content=payload)


@router.post("/approve_builder_fee")
def api_approve(req: ApproveReq, b: HyperliquidBroker = Depends(_broker_dep)):
    try:
        out = b.approve_builder_fee(
            user_main_address=req.user_main_address,
            builder_address=req.builder_address,
            max_fee_bps=req.max_fee_bps,
        )
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"approve error: {e}")


@router.post("/cancel_by_client_id")
def api_cancel(req: CancelReq, b: HyperliquidBroker = Depends(_broker_dep)):
    try:
        out = b.cancel_by_client_id(req.client_id)
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cancel error: {e}")
