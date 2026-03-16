"""
HyperCopy Trading Engine
=========================
Background service handling the full trade lifecycle:
  1. Signal Processing   — new KOL signals → copy trades for followers
  2. Position Management — live PnL, TP/SL enforcement, auto-close
  3. ★ Equity Protection — force-close all positions if equity < threshold
  4. Signal Price Update  — keep pct_change fresh for leaderboard
  5. Balance Sync         — HL equity → BalanceSnapshot
  6. Stats Recompute      — refresh TraderStats for leaderboard
  7. Mark Stale Signals

★ 2026-03-14 fixes:
  - Ghost position prevention: if DB write fails after HL fill, close on HL
  - Auto builder fee approval: on "not approved" error, approve + retry once
  - Session-level cache of approved wallets to avoid repeated HL calls

★ 2026-03-15 fixes:
  - Same-ticker conflict guard: skip if user already has ANY open trade
    on the same coin (regardless of which trader), preventing HL net
    position cancellation and close failures.

★ 2026-03-16 fixes:
  - Withdrawable margin check: use HL withdrawable (not just equity) to
    decide if user can open a new trade. Prevents HL margin rejections.
  - Size cap uses min(equity*0.9, withdrawable*0.9) to stay within margin.

Run:  python -m backend.services.trading_engine
"""
from backend.services.rewards_engine import recompute_kol_points, run_weekly_distribution
import os, sys, time, math, logging, requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.database import SessionLocal
from backend.models.signal import Signal
from backend.models.trade import Trade
from backend.models.trader import Trader, TraderStats
from backend.models.follow import Follow
from backend.models.user import User
from backend.models.wallet import UserWallet
from backend.models.setting import CopySetting, BalanceSnapshot
from backend.services.wallet_manager import (
    decrypt_key, execute_copy_trade, get_hl_balance,
    approve_builder_fee_for_wallet,
)

log = logging.getLogger("trading_engine")

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

SIGNAL_MAX_AGE_SEC     = 300        # ignore signals older than 5 min
SLIPPAGE_BPS           = 50         # 0.5 % slippage for IOC limit
MIN_TRADE_USD          = 10.0       # minimum position notional
LOOP_SLEEP_SEC         = 15         # main loop cadence
BALANCE_SYNC_INTERVAL  = 300        # 5 min
STATS_RECOMPUTE_INTERVAL = 600      # 10 min
META_REFRESH_INTERVAL  = 3600       # 1 hour

EQUITY_SKIP_THRESHOLD  = 5.0        # ★ Skip new trades below this equity
MIN_EQUITY_CLOSE_ALL   = 2.0        # ★ Force-close ALL positions below this equity

# ★ Referral / free-trade config
FREE_COPY_TRADES_LIMIT  = 10
BUILDER_BPS_DEFAULT     = int(os.environ.get("HL_DEFAULT_BUILDER_BPS", "10"))
BUILDER_ADDRESS         = os.environ.get("HL_BUILDER_ADDRESS", "")

# ★ Session-level cache: wallets that have been confirmed builder-fee-approved
#   Avoids repeated HL API calls. Cleared on engine restart.
_approved_wallets: set[str] = set()


# ═══════════════════════════════════════════════════════════════
#  HYPERLIQUID HELPERS
# ═══════════════════════════════════════════════════════════════

def _hl_post(payload: dict, timeout: int = 10) -> dict:
    r = requests.post(HL_INFO_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def hl_load_meta() -> dict[str, int]:
    data = _hl_post({"type": "meta"})
    return {a["name"]: a.get("szDecimals", 2) for a in data.get("universe", [])}


def hl_all_mids() -> dict[str, float]:
    raw = _hl_post({"type": "allMids"})
    return {k: float(v) for k, v in raw.items()}


def hl_clearinghouse(address: str) -> dict:
    return _hl_post({"type": "clearinghouseState", "user": address.lower()})


def hl_parse_positions(state: dict) -> dict[str, dict]:
    out = {}
    for ap in state.get("assetPositions", []):
        p = ap.get("position", {})
        coin = p.get("coin")
        if coin:
            out[coin] = {
                "szi":     float(p.get("szi", "0")),
                "entryPx": float(p.get("entryPx", "0")),
                "upnl":    float(p.get("unrealizedPnl", "0")),
            }
    return out


def _hl_set_leverage(private_key: str, coin: str, leverage: int, cross: bool = True):
    try:
        from hyperliquid.exchange import Exchange
        import eth_account
        acct = eth_account.Account.from_key(private_key)
        ex = Exchange(wallet=acct, base_url="https://api.hyperliquid.xyz")
        ex.update_leverage(int(leverage), coin, is_cross=cross)
    except Exception as e:
        log.warning(f"Set leverage {coin} {leverage}x failed: {e}")


def _parse_order_result(result: dict) -> tuple[bool, float, str]:
    """
    Parse HL order result. Returns (filled, avg_price, error_msg).
    error_msg is empty string if no error.
    """
    try:
        statuses = (
            result.get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        for st in statuses:
            if "filled" in st:
                return True, float(st["filled"].get("avgPx", 0)), ""
            if "resting" in st:
                return True, 0.0, ""
            if "error" in st:
                return False, 0.0, st["error"]
    except Exception:
        pass
    return False, 0.0, "unknown error"


def _round_price(raw: float) -> float:
    """Round price to 5 significant figures — HyperLiquid's rule."""
    if raw <= 0:
        return 0.0
    magnitude = math.floor(math.log10(raw)) + 1
    decimal_places = max(0, 5 - magnitude)
    return round(raw, decimal_places)


# ═══════════════════════════════════════════════════════════════
#  ★ BUILDER FEE AUTO-APPROVAL
# ═══════════════════════════════════════════════════════════════

def _ensure_builder_approved(pk: str, wallet_address: str) -> bool:
    """
    Ensure builder fee is approved for this wallet.
    Uses session cache to avoid repeated HL calls.
    Returns True if approved (or was already approved).
    """
    if wallet_address in _approved_wallets:
        return True
    try:
        result = approve_builder_fee_for_wallet(pk)
        if result.get("status") in ("ok", "skipped"):
            _approved_wallets.add(wallet_address)
            return True
        log.warning(f"Builder fee approval unexpected: {result}")
        _approved_wallets.add(wallet_address)
        return True
    except Exception as e:
        log.error(f"Builder fee approval failed for {wallet_address[:10]}…: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  ★ GHOST POSITION PREVENTION
# ═══════════════════════════════════════════════════════════════

def _emergency_close_position(pk: str, coin: str, is_buy: bool, qty: float, mid: float):
    """
    Emergency close: if DB write fails after HL order filled,
    immediately send a reduce_only order to close the ghost position.
    """
    try:
        close_is_buy = not is_buy
        slip = SLIPPAGE_BPS / 10_000
        raw_price = mid * (1 + slip) if close_is_buy else mid * (1 - slip)
        price = _round_price(raw_price)

        result = execute_copy_trade(
            private_key=pk, coin=coin, is_buy=close_is_buy,
            size=qty, price=price, reduce_only=True,
        )
        filled, _, err = _parse_order_result(result)
        if filled:
            log.warning(f"  🛡️ Ghost position closed: {coin} qty={qty}")
        else:
            log.error(f"  🚨 GHOST POSITION REMAINS: {coin} qty={qty} — close failed: {err}")
    except Exception as e:
        log.error(f"  🚨 GHOST POSITION REMAINS: {coin} qty={qty} — emergency close error: {e}")


# ═══════════════════════════════════════════════════════════════
#  ★ REFERRAL HELPERS (inline — avoids circular import)
# ═══════════════════════════════════════════════════════════════

def _get_free_trades_remaining(db: Session, user_id: str) -> int:
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not getattr(user, "referral_code_used", None):
        return 0
    used = getattr(user, "free_copy_trades_used", 0) or 0
    return max(0, FREE_COPY_TRADES_LIMIT - used)


def _consume_free_trade(db: Session, user_id: str) -> bool:
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not getattr(user, "referral_code_used", None):
        return False
    used = getattr(user, "free_copy_trades_used", 0) or 0
    if used >= FREE_COPY_TRADES_LIMIT:
        return False
    try:
        user.free_copy_trades_used = used + 1
    except Exception:
        pass
    return True


# ═══════════════════════════════════════════════════════════════
#  1. SIGNAL PROCESSING → COPY TRADE EXECUTION
# ═══════════════════════════════════════════════════════════════

def process_new_signals(db: Session, coins: dict[str, int], mids: dict[str, float]):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=SIGNAL_MAX_AGE_SEC)
    signals = (
        db.query(Signal)
        .filter(Signal.status == "active", Signal.created_at >= cutoff)
        .order_by(Signal.created_at.asc())
        .limit(50)
        .all()
    )
    if not signals:
        return
    log.info(f"📡  {len(signals)} new signals to process")
    for sig in signals:
        try:
            _dispatch_signal(db, sig, coins, mids)
        except Exception as e:
            log.error(f"Signal {sig.id} dispatch error: {e}", exc_info=True)
        finally:
            if sig.status == "active":
                sig.status = "processed"
    db.commit()


def _dispatch_signal(db: Session, sig: Signal, coins: dict, mids: dict):
    coin = sig.ticker
    if coin not in coins:
        sig.status = "skipped"
        return
    mid = mids.get(coin)
    if not mid or mid <= 0:
        sig.status = "skipped"
        return
    if not sig.entry_price:
        sig.entry_price = mid

    followers = (
        db.query(Follow)
        .filter(
            Follow.trader_id == sig.trader_id,
            (Follow.is_copy_trading.is_(True)) | (Follow.is_counter_trading.is_(True)),
        )
        .all()
    )
    if not followers:
        return

    copy_count    = sum(1 for f in followers if f.is_copy_trading)
    counter_count = sum(1 for f in followers if f.is_counter_trading)
    log.info(
        f"  → {coin} {sig.direction} (trader {sig.trader_id[:8]}…) "
        f"→ {copy_count} copiers, {counter_count} counters"
    )

    for fol in followers:
        try:
            _execute_for_user(
                db, fol.user_id, sig, coin, mid, coins[coin],
                is_counter=fol.is_counter_trading,
            )
        except Exception as e:
            log.error(f"  ✗ user {fol.user_id[:8]}… : {e}")


def _execute_for_user(
    db: Session,
    user_id: str,
    sig: Signal,
    coin: str,
    mid: float,
    sz_decimals: int,
    is_counter: bool = False,
):
    """
    Execute a copy (or counter) trade for one user.
    ★ Same-ticker guard   → skip if user has ANY open trade on this coin.
    ★ Margin guard        → skip if withdrawable < MIN_TRADE_USD.
    ★ is_counter=True     → flips direction (long→short, short→long).
    ★ Referral boost      → first FREE_COPY_TRADES_LIMIT trades are fee-free.
    ★ Ghost prevention    → if DB fails after HL fill, closes position on HL.
    ★ Auto builder fee    → if HL rejects for missing approval, approve + retry.
    """

    # ── ★ P0: Same-ticker conflict guard ──────────────────
    existing_any = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.ticker == coin,
            Trade.status == "open",
        )
        .first()
    )
    if existing_any:
        log.info(
            f"  ⏭️ SKIP {coin} — user {user_id[:8]}… already has open "
            f"{existing_any.direction} {coin} (trader {existing_any.trader_username})"
        )
        return

    wallet = (
        db.query(UserWallet)
        .filter(UserWallet.user_id == user_id, UserWallet.is_active.is_(True))
        .first()
    )
    if not wallet:
        return

    # ── ★ Balance + margin check ──────────────────────────
    bal          = get_hl_balance(wallet.address)
    equity       = bal.get("equity", 0.0)
    withdrawable = bal.get("withdrawable", 0.0)

    if equity < EQUITY_SKIP_THRESHOLD:
        log.info(
            f"  ⏭️ SKIP user {user_id[:8]}… equity=${equity:.2f} "
            f"< ${EQUITY_SKIP_THRESHOLD} — insufficient balance"
        )
        return

    if withdrawable < MIN_TRADE_USD:
        log.info(
            f"  ⏭️ SKIP user {user_id[:8]}… withdrawable=${withdrawable:.2f} "
            f"< ${MIN_TRADE_USD} — no free margin"
        )
        return

    settings = (
        db.query(CopySetting)
        .filter(CopySetting.user_id == user_id, CopySetting.trader_id.is_(None))
        .first()
    )
    leverage  = settings.leverage if settings else 5.0
    max_pos   = settings.max_positions if settings else 10

    open_count = (
        db.query(Trade)
        .filter(Trade.user_id == user_id, Trade.status == "open")
        .count()
    )
    if open_count >= max_pos:
        log.info(
            f"  ⏭️ SKIP user {user_id[:8]}… max positions reached "
            f"({open_count}/{max_pos})"
        )
        return

    dup = db.query(Trade).filter(Trade.user_id == user_id, Trade.signal_id == sig.id).first()
    if dup:
        return

    trader_username = (
        db.query(Trader.username).filter(Trader.id == sig.trader_id).scalar()
    )
    existing_pos = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.ticker == coin,
        Trade.trader_username == trader_username,
        Trade.status == "open",
    ).first()
    if existing_pos:
        return

    # ── Size calculation ──
    if settings and settings.size_type == "fixed_usd":
        usd_alloc = min(settings.size_value, equity * 0.9)
    else:
        pct       = (settings.size_value if settings else 10.0) / 100.0
        usd_alloc = equity * pct
    usd_alloc = max(usd_alloc, MIN_TRADE_USD)

    # ★ Cap to 90% of equity AND 90% of withdrawable margin
    max_alloc = min(equity * 0.9, withdrawable * 0.9)
    if usd_alloc > max_alloc:
        usd_alloc = max_alloc
        log.info(
            f"  ⚠️ Capped allocation for user {user_id[:8]}… "
            f"→ ${usd_alloc:.2f} (equity=${equity:.2f}, withdrawable=${withdrawable:.2f})"
        )

    if usd_alloc < MIN_TRADE_USD:
        log.info(
            f"  ⏭️ SKIP user {user_id[:8]}… alloc=${usd_alloc:.2f} "
            f"< ${MIN_TRADE_USD} after cap — insufficient margin"
        )
        return

    notional = usd_alloc * leverage
    qty      = round(notional / mid, sz_decimals)
    if qty <= 0:
        return

    # ── Direction (flip if counter) ──
    original_is_buy    = sig.direction == "long"
    is_buy             = (not original_is_buy) if is_counter else original_is_buy
    effective_direction = "long" if is_buy else "short"

    # ── Price with slippage ──
    slip      = SLIPPAGE_BPS / 10_000
    raw_price = mid * (1 + slip) if is_buy else mid * (1 - slip)
    price     = _round_price(raw_price)

    # ── ★ Referral: check free-trade eligibility ──
    free_trades_left = _get_free_trades_remaining(db, user_id)
    is_fee_free      = free_trades_left > 0
    builder_bps      = 0 if is_fee_free else BUILDER_BPS_DEFAULT

    # ── ★ Ensure builder fee is approved before trading ──
    pk       = decrypt_key(wallet.encrypted_private_key)
    is_cross = (settings.margin_mode == "cross") if settings else True
    _hl_set_leverage(pk, coin, int(leverage), cross=is_cross)

    _ensure_builder_approved(pk, wallet.address)

    # ── Execute on HL (with auto-retry on builder fee error) ──
    result = execute_copy_trade(
        private_key=pk, coin=coin, is_buy=is_buy,
        size=qty, price=price, builder_bps=builder_bps,
    )
    filled, avg_px, err_msg = _parse_order_result(result)

    # ★ Auto-retry: if builder fee not approved, approve and retry once
    if not filled and "Builder fee has not been approved" in err_msg:
        log.warning(f"  ⚡ Builder fee not approved for {wallet.address[:10]}… — approving now")
        _approved_wallets.discard(wallet.address)
        approved = _ensure_builder_approved(pk, wallet.address)
        if approved:
            result = execute_copy_trade(
                private_key=pk, coin=coin, is_buy=is_buy,
                size=qty, price=price, builder_bps=builder_bps,
            )
            filled, avg_px, err_msg = _parse_order_result(result)

    if not filled:
        log.warning(f"  ✗ order not filled user {user_id[:8]}… {coin}: {err_msg}")
        return

    fill_price = avg_px if avg_px > 0 else mid

    # ── ★ Calculate fee (for affiliate revenue share tracking) ──
    size_usd = qty * fill_price
    fee_usd  = 0.0 if is_fee_free else round(size_usd * builder_bps / 10_000, 6)

    # ── ★ Persist trade (with ghost position prevention) ──
    trader = db.query(Trader).filter(Trader.id == sig.trader_id).first()
    try:
        trade = Trade(
            user_id         = user_id,
            signal_id       = sig.id,
            trader_username = trader.username if trader else None,
            ticker          = coin,
            direction       = effective_direction,
            entry_price     = fill_price,
            size_usd        = usd_alloc,
            size_qty        = qty,
            leverage        = leverage,
            status          = "open",
            source          = "counter" if is_counter else "copy",
            fee_usd         = fee_usd,
            is_fee_free     = is_fee_free,
        )
        db.add(trade)
        db.flush()
    except Exception as db_err:
        log.error(
            f"  🚨 DB WRITE FAILED after HL fill — closing ghost position: {db_err}"
        )
        db.rollback()
        _emergency_close_position(pk, coin, is_buy, qty, mid)
        return

    # ── ★ Consume free trade slot after successful fill + DB write ──
    if is_fee_free:
        _consume_free_trade(db, user_id)
        log.info(
            f"  🎁 FREE trade used — user {user_id[:8]}… "
            f"({FREE_COPY_TRADES_LIMIT - free_trades_left + 1}/{FREE_COPY_TRADES_LIMIT})"
        )

    log.info(
        f"  ✅ {'COUNTER' if is_counter else 'COPY'} {effective_direction.upper()} {coin} "
        f"user {user_id[:8]}… qty={qty} @ {fill_price:.2f} "
        f"fee={'FREE' if is_fee_free else f'${fee_usd:.4f}'}"
    )

    # ★ Accrue affiliate revenue share + update weekly points
    try:
        from backend.api.rewards import on_trade_placed
        on_trade_placed(db, user_id, fee_usd)
    except Exception as e:
        log.warning(f"  on_trade_placed hook failed: {e}")


# ═══════════════════════════════════════════════════════════════
#  2. POSITION MANAGEMENT  (PnL + TP/SL)
# ═══════════════════════════════════════════════════════════════

def update_positions(db: Session, mids: dict[str, float]):
    trades = db.query(Trade).filter(Trade.status == "open").all()
    if not trades:
        return
    by_user: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_user[t.user_id].append(t)
    for uid, user_trades in by_user.items():
        try:
            _manage_user_positions(db, uid, user_trades, mids)
        except Exception as e:
            log.error(f"Position mgmt {uid[:8]}…: {e}", exc_info=True)
    db.commit()


def _manage_user_positions(db: Session, user_id: str, trades: list, mids: dict):
    wallet = (
        db.query(UserWallet)
        .filter(UserWallet.user_id == user_id, UserWallet.is_active.is_(True))
        .first()
    )
    if not wallet:
        return
    state    = hl_clearinghouse(wallet.address)
    hl_pos   = hl_parse_positions(state)
    settings = (
        db.query(CopySetting)
        .filter(CopySetting.user_id == user_id, CopySetting.trader_id.is_(None))
        .first()
    )
    tp_pct = settings.tp_value if settings else 15.0
    sl_pct = settings.sl_value if settings else 50.0

    for trade in trades:
        mid = mids.get(trade.ticker)
        if not mid:
            continue
        if trade.direction == "long":
            pnl_pct = (mid - trade.entry_price) / trade.entry_price * 100
        else:
            pnl_pct = (trade.entry_price - mid) / trade.entry_price * 100
        pnl_usd        = pnl_pct / 100 * trade.size_usd * trade.leverage
        trade.pnl_pct  = round(pnl_pct, 2)
        trade.pnl_usd  = round(pnl_usd, 2)

        hp = hl_pos.get(trade.ticker)
        if not hp or abs(hp["szi"]) < 1e-10:
            trade.status    = "closed"
            trade.exit_price= mid
            trade.closed_at = datetime.now(timezone.utc)
            log.info(
                f"  ↩ {trade.ticker} closed externally, "
                f"user {user_id[:8]}… PnL ${trade.pnl_usd:.2f}"
            )
            continue

        reason = ""
        if pnl_pct >= tp_pct:
            reason = "TP"
        elif pnl_pct <= -sl_pct:
            reason = "SL"
        if reason:
            _close_trade(db, trade, wallet, mid, reason)


def _close_trade(db: Session, trade: Trade, wallet: UserWallet, mid: float, reason: str):
    try:
        pk       = decrypt_key(wallet.encrypted_private_key)
        is_buy   = trade.direction == "short"
        slip     = SLIPPAGE_BPS / 10_000
        raw_price= mid * (1 + slip) if is_buy else mid * (1 - slip)
        price    = _round_price(raw_price)

        result          = execute_copy_trade(
            private_key=pk, coin=trade.ticker, is_buy=is_buy,
            size=trade.size_qty, price=price, reduce_only=True,
        )
        filled, avg_px, _ = _parse_order_result(result)
        exit_px         = avg_px if (filled and avg_px > 0) else mid

        trade.status    = "closed"
        trade.exit_price= exit_px
        trade.closed_at = datetime.now(timezone.utc)

        if trade.direction == "long":
            trade.pnl_pct = round((exit_px - trade.entry_price) / trade.entry_price * 100, 2)
        else:
            trade.pnl_pct = round((trade.entry_price - exit_px) / trade.entry_price * 100, 2)
        trade.pnl_usd = round(trade.pnl_pct / 100 * trade.size_usd * trade.leverage, 2)

        log.info(
            f"  ✅ CLOSE {trade.ticker} ({reason}) "
            f"user {trade.user_id[:8]}… PnL {trade.pnl_pct:+.1f}%"
        )
    except Exception as e:
        log.error(f"  ✗ close {trade.ticker} failed: {e}")


# ═══════════════════════════════════════════════════════════════
#  3. ★ EQUITY PROTECTION — force-close all if equity too low
# ═══════════════════════════════════════════════════════════════

def check_equity_protection(db: Session, mids: dict[str, float]):
    open_trades = db.query(Trade).filter(Trade.status == "open").all()
    if not open_trades:
        return

    by_user: dict[str, list[Trade]] = defaultdict(list)
    for t in open_trades:
        by_user[t.user_id].append(t)

    for uid, user_trades in by_user.items():
        wallet = (
            db.query(UserWallet)
            .filter(UserWallet.user_id == uid, UserWallet.is_active.is_(True))
            .first()
        )
        if not wallet:
            continue
        try:
            bal = get_hl_balance(wallet.address)
            equity = bal.get("equity", 0.0)

            if equity < MIN_EQUITY_CLOSE_ALL:
                log.warning(
                    f"🛑 EQUITY PROTECTION: user {uid[:8]}… equity=${equity:.2f} "
                    f"< ${MIN_EQUITY_CLOSE_ALL} → force-closing {len(user_trades)} positions"
                )
                for trade in user_trades:
                    mid = mids.get(trade.ticker)
                    if mid:
                        _close_trade(db, trade, wallet, mid, "EQUITY_PROTECT")
                    else:
                        trade.status = "closed"
                        trade.exit_price = trade.entry_price
                        trade.closed_at = datetime.now(timezone.utc)
                        log.warning(
                            f"  ⚠️ {trade.ticker} no mid price available — "
                            f"closed at entry price ${trade.entry_price}"
                        )
                db.commit()
        except Exception as e:
            log.error(f"Equity protection check failed for {uid[:8]}…: {e}")


# ═══════════════════════════════════════════════════════════════
#  4. SIGNAL PRICE UPDATE  (for leaderboard pct_change)
# ═══════════════════════════════════════════════════════════════

def update_signal_prices(db: Session, mids: dict[str, float]):
    sig_time = func.coalesce(Signal.tweet_time, Signal.created_at)
    cutoff   = datetime.now(timezone.utc) - timedelta(days=30)

    signals  = (
        db.query(Signal)
        .filter(
            sig_time >= cutoff,
            Signal.status.in_(["active", "processed", "expired"]),
        )
        .all()
    )
    changed    = 0
    backfilled = 0

    for sig in signals:
        mid = mids.get(sig.ticker)
        if not mid:
            continue
        if not sig.entry_price:
            sig.entry_price = mid
            backfilled     += 1
        sig.current_price = mid
        if sig.entry_price:
            if sig.direction == "long":
                sig.pct_change = round((mid - sig.entry_price) / sig.entry_price * 100, 2)
            else:
                sig.pct_change = round((sig.entry_price - mid) / sig.entry_price * 100, 2)
        changed += 1

    if changed:
        db.commit()
    if backfilled:
        log.info(f"  ★ Backfilled entry_price for {backfilled} signals")


# ═══════════════════════════════════════════════════════════════
#  5. BALANCE SYNC
# ═══════════════════════════════════════════════════════════════

def sync_balances(db: Session):
    wallets = db.query(UserWallet).filter(UserWallet.is_active.is_(True)).all()
    if not wallets:
        return
    today  = datetime.now(timezone.utc).date()
    synced = 0
    for w in wallets:
        try:
            bal         = get_hl_balance(w.address)
            equity      = bal.get("equity", 0.0)
            withdrawable= bal.get("withdrawable", 0.0)
            positions_val = abs(bal.get("positions", 0.0))

            snap = (
                db.query(BalanceSnapshot)
                .filter(
                    BalanceSnapshot.user_id == w.user_id,
                    BalanceSnapshot.snapshot_date == today,
                )
                .first()
            )
            if snap:
                snap.balance   = equity
                snap.available = withdrawable
                snap.used      = positions_val
            else:
                prev = (
                    db.query(BalanceSnapshot)
                    .filter(BalanceSnapshot.user_id == w.user_id)
                    .order_by(BalanceSnapshot.snapshot_date.desc())
                    .first()
                )
                prev_bal = prev.balance if prev else equity
                snap = BalanceSnapshot(
                    user_id      = w.user_id,
                    balance      = equity,
                    available    = withdrawable,
                    used         = positions_val,
                    pnl_daily    = round(equity - prev_bal, 2),
                    snapshot_date= today,
                )
                db.add(snap)
            synced += 1
        except Exception as e:
            log.error(f"Balance sync {w.address[:10]}…: {e}")
    db.commit()
    if synced:
        log.info(f"💰  Synced {synced} wallet balances")


# ═══════════════════════════════════════════════════════════════
#  6. STATS RECOMPUTE
# ═══════════════════════════════════════════════════════════════

WINDOWS = {
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
}

_GRADE_TABLE = [
    (0.70, 10.0, "S+"),
    (0.60,  5.0, "S"),
    (0.55,  2.0, "A"),
    (0.45,  0.0, "B"),
]


def _profit_grade(wr: float, avg_ret: float) -> str:
    for min_wr, min_ret, g in _GRADE_TABLE:
        if wr >= min_wr and avg_ret >= min_ret:
            return g
    return "C"


def recompute_stats(db: Session):
    now     = datetime.now(timezone.utc)
    traders = db.query(Trader).all()
    if not traders:
        return

    sig_time       = func.coalesce(Signal.tweet_time, Signal.created_at)
    recent_cutoff  = now - timedelta(hours=48)

    for wname, delta in WINDOWS.items():
        cutoff = now - delta
        sigs   = (
            db.query(Signal)
            .filter(sig_time >= cutoff, Signal.pct_change.isnot(None))
            .all()
        )
        by_trader: dict[str, list[Signal]] = defaultdict(list)
        for s in sigs:
            by_trader[s.trader_id].append(s)

        scored: list[tuple[str, float, dict]] = []

        for trader in traders:
            tsigs = by_trader.get(trader.id, [])
            total = len(tsigs)

            if total == 0:
                scored.append((trader.id, 0.0, _empty_stats()))
                continue

            returns  = [s.pct_change for s in tsigs]
            win_n    = sum(1 for r in returns if r > 0)
            loss_n   = sum(1 for r in returns if r <= 0)
            wr       = win_n / total
            avg_ret  = sum(returns) / total
            total_profit = sum(returns)

            ordered = sorted(
                tsigs,
                key=lambda s: s.tweet_time or s.created_at or now,
                reverse=True,
            )
            streak = 0
            for s in ordered:
                if s.pct_change and s.pct_change > 0:
                    streak += 1
                else:
                    break

            grade = _profit_grade(wr, avg_ret)
            pts   = wr * 40 + min(avg_ret, 50) * 0.6 + min(total, 100) * 0.2

            copiers = (
                db.query(func.count(Follow.id))
                .filter(Follow.trader_id == trader.id, Follow.is_copy_trading.is_(True))
                .scalar()
            )

            recent_sigs    = [
                s for s in tsigs
                if (s.tweet_time or s.created_at or now) >= recent_cutoff
            ]
            recent_count   = len(recent_sigs)
            recent_returns = [s.pct_change for s in recent_sigs if s.pct_change is not None]
            recent_avg     = sum(recent_returns) / len(recent_returns) if recent_returns else 0.0

            trending = (
                recent_count * 3.0
                + streak      * 5.0
                + max(recent_avg, 0) * 2.0
                + wr          * 10.0
            )

            data = {
                "total_signals":   total,
                "win_count":       win_n,
                "loss_count":      loss_n,
                "win_rate":        round(wr, 3),
                "avg_return_pct":  round(avg_ret, 2),
                "total_profit_usd":round(total_profit, 2),
                "streak":          streak,
                "points":          round(pts, 1),
                "profit_grade":    grade,
                "copiers_count":   copiers or 0,
                "signal_to_noise": 0.0,
                "trending_score":  round(trending, 1),
            }
            scored.append((trader.id, pts, data))

        scored.sort(key=lambda x: x[1], reverse=True)
        rank_map = {tid: i + 1 for i, (tid, _, _) in enumerate(scored)}

        for trader_id, _, data in scored:
            existing = (
                db.query(TraderStats)
                .filter(
                    TraderStats.trader_id == trader_id,
                    TraderStats.window == wname,
                )
                .first()
            )
            if existing:
                for k, v in data.items():
                    setattr(existing, k, v)
                existing.rank        = rank_map.get(trader_id)
                existing.computed_at = now
            else:
                db.add(TraderStats(
                    trader_id=trader_id, window=wname,
                    rank=rank_map.get(trader_id), computed_at=now,
                    **data,
                ))

    db.commit()
    log.info(f"📊  Stats recomputed for {len(traders)} traders")


def _empty_stats() -> dict:
    return {
        "total_signals": 0, "win_count": 0, "loss_count": 0,
        "win_rate": 0, "avg_return_pct": 0, "total_profit_usd": 0,
        "streak": 0, "points": 0, "profit_grade": "C",
        "copiers_count": 0, "signal_to_noise": 0, "trending_score": 0,
    }


# ═══════════════════════════════════════════════════════════════
#  7. MARK STALE SIGNALS
# ═══════════════════════════════════════════════════════════════

def expire_old_signals(db: Session):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=SIGNAL_MAX_AGE_SEC)
    count  = (
        db.query(Signal)
        .filter(Signal.status == "active", Signal.created_at < cutoff)
        .update({"status": "expired"})
    )
    if count:
        db.commit()
        log.info(f"  ⏰ Expired {count} stale signals")


# ═══════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
        handlers=[logging.StreamHandler()],
    )
    log.info("🚀 HyperCopy Trading Engine starting…")

    coins = {}
    try:
        coins = hl_load_meta()
        log.info(f"HL coins loaded: {len(coins)}")
    except Exception as e:
        log.error(f"Failed to load HL meta (will retry): {e}")

    last_balance_sync    = 0.0
    last_stats_recompute = 0.0
    last_meta_refresh    = time.time()

    while True:
        loop_start = time.time()
        try:
            if not coins or (loop_start - last_meta_refresh >= META_REFRESH_INTERVAL):
                try:
                    coins             = hl_load_meta()
                    last_meta_refresh = loop_start
                except Exception:
                    pass

            mids = {}
            try:
                mids = hl_all_mids()
            except Exception as e:
                log.error(f"Failed to fetch mids: {e}")
                time.sleep(LOOP_SLEEP_SEC)
                continue

            db = SessionLocal()
            try:
                process_new_signals(db, coins, mids)
                expire_old_signals(db)
                update_positions(db, mids)
                check_equity_protection(db, mids)
                update_signal_prices(db, mids)

                if loop_start - last_balance_sync >= BALANCE_SYNC_INTERVAL:
                    sync_balances(db)
                    last_balance_sync = loop_start

                if loop_start - last_stats_recompute >= STATS_RECOMPUTE_INTERVAL:
                    recompute_stats(db)
                    recompute_kol_points(db)
                    run_weekly_distribution(db)
                    last_stats_recompute = loop_start
            finally:
                db.close()

        except Exception as e:
            log.error(f"Engine loop error: {e}", exc_info=True)

        elapsed = time.time() - loop_start
        time.sleep(max(0, LOOP_SLEEP_SEC - elapsed))


if __name__ == "__main__":
    run()