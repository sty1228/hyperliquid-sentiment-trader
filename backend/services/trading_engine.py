"""
HyperCopy Trading Engine
=========================
Background service handling the full trade lifecycle:
  1. Signal Processing   — new KOL signals → copy trades for followers
  2. Position Management — live PnL, TP/SL enforcement, auto-close
  3. Signal Price Update  — keep pct_change fresh for leaderboard
  4. Balance Sync         — HL equity → BalanceSnapshot
  5. Stats Recompute      — refresh TraderStats for leaderboard

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
from backend.services.wallet_manager import decrypt_key, execute_copy_trade, get_hl_balance

log = logging.getLogger("trading_engine")

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

SIGNAL_MAX_AGE_SEC = 300        # ignore signals older than 5 min
SLIPPAGE_BPS = 50               # 0.5 % slippage for IOC limit
MIN_TRADE_USD = 10.0            # minimum position notional
LOOP_SLEEP_SEC = 15             # main loop cadence
BALANCE_SYNC_INTERVAL = 300     # 5 min
STATS_RECOMPUTE_INTERVAL = 600  # 10 min
META_REFRESH_INTERVAL = 3600    # 1 hour


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
                "szi": float(p.get("szi", "0")),
                "entryPx": float(p.get("entryPx", "0")),
                "upnl": float(p.get("unrealizedPnl", "0")),
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


def _parse_order_result(result: dict) -> tuple[bool, float]:
    try:
        statuses = (
            result.get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        for st in statuses:
            if "filled" in st:
                return True, float(st["filled"].get("avgPx", 0))
            if "resting" in st:
                return True, 0.0
            if "error" in st:
                log.warning(f"Order error: {st['error']}")
                return False, 0.0
    except Exception:
        pass
    return False, 0.0


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

    # ★ Include both copy AND counter traders in one query
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

    copy_count = sum(1 for f in followers if f.is_copy_trading)
    counter_count = sum(1 for f in followers if f.is_counter_trading)
    log.info(
        f"  → {coin} {sig.direction} (trader {sig.trader_id[:8]}…) "
        f"→ {copy_count} copiers, {counter_count} counters"
    )

    for fol in followers:
        try:
            _execute_for_user(
                db, fol.user_id, sig, coin, mid, coins[coin],
                is_counter=fol.is_counter_trading,  # ★ NEW
            )
        except Exception as e:
            log.error(f"  ✗ user {fol.user_id[:8]}… : {e}")



def _execute_for_user(db, user_id, sig, coin, mid, sz_decimals, is_counter: bool = False):
    """
    Execute a copy trade for one user.
    ★ is_counter=True → flips the trade direction (long→short, short→long).
    """
    wallet = (
        db.query(UserWallet)
        .filter(UserWallet.user_id == user_id, UserWallet.is_active.is_(True))
        .first()
    )
    if not wallet:
        return

    bal = get_hl_balance(wallet.address)
    equity = bal.get("equity", 0.0)
    if equity < 5:
        return

    settings = (
        db.query(CopySetting)
        .filter(CopySetting.user_id == user_id, CopySetting.trader_id.is_(None))
        .first()
    )
    leverage = settings.leverage if settings else 8.0
    max_pos = settings.max_positions if settings else 10

    open_count = db.query(Trade).filter(Trade.user_id == user_id, Trade.status == "open").count()
    if open_count >= max_pos:
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

    if settings and settings.size_type == "fixed_usd":
        usd_alloc = min(settings.size_value, equity * 0.9)
    else:
        pct = (settings.size_value if settings else 10.0) / 100.0
        usd_alloc = equity * pct
    usd_alloc = max(usd_alloc, MIN_TRADE_USD)

    notional = usd_alloc * leverage
    qty = round(notional / mid, sz_decimals)
    if qty <= 0:
        return

    # ★ Determine direction — flip if counter trading
    original_is_buy = sig.direction == "long"
    is_buy = (not original_is_buy) if is_counter else original_is_buy
    effective_direction = ("short" if is_buy is False else "long")

    slip = SLIPPAGE_BPS / 10_000
    
    raw_price = mid * (1 + slip) if is_buy else mid * (1 - slip)
    if raw_price >= 10000:
        price = round(raw_price)        # BTC 等高价币，整数
    elif raw_price >= 100:
        price = round(raw_price, 1)     # SOL/ETH 等
    elif raw_price >= 1:
        price = round(raw_price, 2)
    else:
        price = round(raw_price, 4)     # 低价 meme 币

    pk = decrypt_key(wallet.encrypted_private_key)
    is_cross = (settings.margin_mode == "cross") if settings else True
    _hl_set_leverage(pk, coin, int(leverage), cross=is_cross)

    result = execute_copy_trade(private_key=pk, coin=coin, is_buy=is_buy, size=qty, price=price)
    filled, avg_px = _parse_order_result(result)
    if not filled:
        log.warning(f"  ✗ order not filled user {user_id[:8]}… {coin}")
        return

    fill_price = avg_px if avg_px > 0 else mid
    trader = db.query(Trader).filter(Trader.id == sig.trader_id).first()

    trade = Trade(
        user_id=user_id,
        signal_id=sig.id,
        trader_username=trader.username if trader else None,
        ticker=coin,
        direction=effective_direction,          # ★ actual executed direction
        entry_price=fill_price,
        size_usd=usd_alloc,
        size_qty=qty,
        leverage=leverage,
        status="open",
        source="counter" if is_counter else "copy",  # ★ distinguishable in trade history
    )
    db.add(trade)
    log.info(
        f"  ✅ {'COUNTER' if is_counter else 'COPY'} {effective_direction.upper()} {coin} "
        f"user {user_id[:8]}… qty={qty} @ {fill_price:.2f}"
    )

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


def _manage_user_positions(db, user_id, trades, mids):
    wallet = (
        db.query(UserWallet)
        .filter(UserWallet.user_id == user_id, UserWallet.is_active.is_(True))
        .first()
    )
    if not wallet:
        return
    state = hl_clearinghouse(wallet.address)
    hl_pos = hl_parse_positions(state)
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
        pnl_usd = pnl_pct / 100 * trade.size_usd * trade.leverage
        trade.pnl_pct = round(pnl_pct, 2)
        trade.pnl_usd = round(pnl_usd, 2)
        hp = hl_pos.get(trade.ticker)
        if not hp or abs(hp["szi"]) < 1e-10:
            trade.status = "closed"
            trade.exit_price = mid
            trade.closed_at = datetime.now(timezone.utc)
            log.info(f"  ↩ {trade.ticker} closed externally, user {user_id[:8]}… PnL ${trade.pnl_usd:.2f}")
            continue
        reason = ""
        if pnl_pct >= tp_pct:
            reason = "TP"
        elif pnl_pct <= -sl_pct:
            reason = "SL"
        if reason:
            _close_trade(db, trade, wallet, mid, reason)


def _close_trade(db, trade, wallet, mid, reason):
    try:
        pk = decrypt_key(wallet.encrypted_private_key)
        is_buy = trade.direction == "short"
        slip = SLIPPAGE_BPS / 10_000
        raw_price = mid * (1 + slip) if is_buy else mid * (1 - slip)
        if raw_price >= 10000:
            price = round(raw_price)        # BTC 等高价币，整数
        elif raw_price >= 100:
            price = round(raw_price, 1)     # SOL/ETH 等
        elif raw_price >= 1:
            price = round(raw_price, 2)
        else:
            price = round(raw_price, 4)     # 低价 meme 币
        result = execute_copy_trade(
            private_key=pk, coin=trade.ticker, is_buy=is_buy,
            size=trade.size_qty, price=price, reduce_only=True,
        )
        filled, avg_px = _parse_order_result(result)
        exit_px = avg_px if (filled and avg_px > 0) else mid
        trade.status = "closed"
        trade.exit_price = exit_px
        trade.closed_at = datetime.now(timezone.utc)
        if trade.direction == "long":
            trade.pnl_pct = round((exit_px - trade.entry_price) / trade.entry_price * 100, 2)
        else:
            trade.pnl_pct = round((trade.entry_price - exit_px) / trade.entry_price * 100, 2)
        trade.pnl_usd = round(trade.pnl_pct / 100 * trade.size_usd * trade.leverage, 2)
        log.info(f"  ✅ CLOSE {trade.ticker} ({reason}) user {trade.user_id[:8]}… PnL {trade.pnl_pct:+.1f}%")
    except Exception as e:
        log.error(f"  ✗ close {trade.ticker} failed: {e}")


# ═══════════════════════════════════════════════════════════════
#  3. SIGNAL PRICE UPDATE  (for leaderboard pct_change)
#  ★ FIX: backfill missing entry_price + use tweet_time
# ═══════════════════════════════════════════════════════════════

def update_signal_prices(db: Session, mids: dict[str, float]):
    """Keep current_price / pct_change fresh for recent signals.
    ★ Also backfills entry_price for signals that missed processing."""
    sig_time = func.coalesce(Signal.tweet_time, Signal.created_at)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    signals = (
        db.query(Signal)
        .filter(
            sig_time >= cutoff,
            Signal.status.in_(["active", "processed", "expired"]),
        )
        .all()
    )
    changed = 0
    backfilled = 0

    for sig in signals:
        mid = mids.get(sig.ticker)
        if not mid:
            continue

        # ★ Backfill entry_price for signals that missed process_new_signals
        if not sig.entry_price:
            sig.entry_price = mid
            backfilled += 1

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
#  4. BALANCE SYNC  (HL equity → BalanceSnapshot)
# ═══════════════════════════════════════════════════════════════

def sync_balances(db: Session):
    wallets = db.query(UserWallet).filter(UserWallet.is_active.is_(True)).all()
    if not wallets:
        return
    today = datetime.now(timezone.utc).date()
    synced = 0
    for w in wallets:
        try:
            bal = get_hl_balance(w.address)
            equity = bal.get("equity", 0.0)
            withdrawable = bal.get("withdrawable", 0.0)
            positions_val = abs(bal.get("positions", 0.0))
            snap = (
                db.query(BalanceSnapshot)
                .filter(BalanceSnapshot.user_id == w.user_id, BalanceSnapshot.snapshot_date == today)
                .first()
            )
            if snap:
                snap.balance = equity
                snap.available = withdrawable
                snap.used = positions_val
            else:
                prev = (
                    db.query(BalanceSnapshot)
                    .filter(BalanceSnapshot.user_id == w.user_id)
                    .order_by(BalanceSnapshot.snapshot_date.desc())
                    .first()
                )
                prev_bal = prev.balance if prev else equity
                snap = BalanceSnapshot(
                    user_id=w.user_id, balance=equity, available=withdrawable,
                    used=positions_val, pnl_daily=round(equity - prev_bal, 2),
                    snapshot_date=today,
                )
                db.add(snap)
            synced += 1
        except Exception as e:
            log.error(f"Balance sync {w.address[:10]}…: {e}")
    db.commit()
    if synced:
        log.info(f"💰  Synced {synced} wallet balances")


# ═══════════════════════════════════════════════════════════════
#  5. STATS RECOMPUTE  (TraderStats for leaderboard)
#  ★ FIX: use tweet_time (not created_at) for window filtering
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
    """Rebuild TraderStats for every trader × window.
    ★ Uses COALESCE(tweet_time, created_at) so historical tweets
      imported in bulk are bucketed by their actual tweet time,
      not when the ingestor inserted them."""
    now = datetime.now(timezone.utc)
    traders = db.query(Trader).all()
    if not traders:
        return

    # ★ Use tweet_time (actual publish time) for time-window bucketing
    sig_time = func.coalesce(Signal.tweet_time, Signal.created_at)

    # ★ Trending: last 48h based on tweet_time
    recent_cutoff = now - timedelta(hours=48)

    for wname, delta in WINDOWS.items():
        cutoff = now - delta

        # ★ Filter by actual tweet time, not DB insertion time
        sigs = (
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

            returns = [s.pct_change for s in tsigs]
            win_n  = sum(1 for r in returns if r > 0)
            loss_n = sum(1 for r in returns if r <= 0)
            wr = win_n / total
            avg_ret = sum(returns) / total
            total_profit = sum(returns)

            # streak: consecutive recent wins (by tweet_time)
            ordered = sorted(tsigs, key=lambda s: s.tweet_time or s.created_at or now, reverse=True)
            streak = 0
            for s in ordered:
                if s.pct_change and s.pct_change > 0:
                    streak += 1
                else:
                    break

            grade = _profit_grade(wr, avg_ret)
            pts = wr * 40 + min(avg_ret, 50) * 0.6 + min(total, 100) * 0.2

            copiers = (
                db.query(func.count(Follow.id))
                .filter(Follow.trader_id == trader.id, Follow.is_copy_trading.is_(True))
                .scalar()
            )

            # ★ Trending score — use tweet_time for recency
            recent_sigs = [
                s for s in tsigs
                if (s.tweet_time or s.created_at or now) >= recent_cutoff
            ]
            recent_count = len(recent_sigs)
            recent_returns = [s.pct_change for s in recent_sigs if s.pct_change is not None]
            recent_avg = sum(recent_returns) / len(recent_returns) if recent_returns else 0.0

            trending = (
                recent_count * 3.0
                + streak * 5.0
                + max(recent_avg, 0) * 2.0
                + wr * 10.0
            )

            data = {
                "total_signals": total,
                "win_count": win_n,
                "loss_count": loss_n,
                "win_rate": round(wr, 3),
                "avg_return_pct": round(avg_ret, 2),
                "total_profit_usd": round(total_profit, 2),
                "streak": streak,
                "points": round(pts, 1),
                "profit_grade": grade,
                "copiers_count": copiers or 0,
                "signal_to_noise": 0.0,
                "trending_score": round(trending, 1),
            }
            scored.append((trader.id, pts, data))

        scored.sort(key=lambda x: x[1], reverse=True)
        rank_map = {tid: i + 1 for i, (tid, _, _) in enumerate(scored)}

        for trader_id, _, data in scored:
            existing = (
                db.query(TraderStats)
                .filter(TraderStats.trader_id == trader_id, TraderStats.window == wname)
                .first()
            )
            if existing:
                for k, v in data.items():
                    setattr(existing, k, v)
                existing.rank = rank_map.get(trader_id)
                existing.computed_at = now
            else:
                db.add(TraderStats(
                    trader_id=trader_id, window=wname,
                    rank=rank_map.get(trader_id), computed_at=now, **data,
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
#  6. MARK STALE SIGNALS
# ═══════════════════════════════════════════════════════════════

def expire_old_signals(db: Session):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=SIGNAL_MAX_AGE_SEC)
    count = (
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

    last_balance_sync = 0.0
    last_stats_recompute = 0.0
    last_meta_refresh = time.time()

    while True:
        loop_start = time.time()
        try:
            if not coins or (loop_start - last_meta_refresh >= META_REFRESH_INTERVAL):
                try:
                    coins = hl_load_meta()
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