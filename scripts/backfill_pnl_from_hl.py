"""
One-shot backfill: rewrite trades.pnl_usd / fee_usd from HyperLiquid as
the source of truth. Use to repair historical inflation caused by the old
local PnL formula (`pnl_pct/100 * size_usd * leverage`), which silently
multiplied by leverage when size_usd had drifted from "margin" semantics.

Strategy:
  1. For every UserWallet, pull HL userFills (paginated by time if needed).
  2. For OPEN trades on this user/ticker — overwrite pnl_usd from HL
     clearinghouseState.assetPositions[].position.unrealizedPnl.
  3. For CLOSED trades on this user/ticker — find all closing fills
     (closedPnl != 0) within [opened_at, closed_at + slack], sum
     closedPnl into pnl_usd and HL `fee` into fee_usd.

Usage:
  python -m scripts.backfill_pnl_from_hl                  # dry-run, prints diffs
  python -m scripts.backfill_pnl_from_hl --apply          # writes to DB
  python -m scripts.backfill_pnl_from_hl --user 66af6a30  # restrict to one user (id prefix or full uuid)

Verification target (the user that prompted this backfill):
  user_id 66af6a30-2b78-4358-8440-15b33e89ba3f
  HL addr 0x7E7d38187C2AF3C53731fa9Fcec044835F604A58
  HL allTime PnL = +$422.85   ← DB SUM(pnl_usd, status='closed') should approach this
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import timedelta

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models.trade import Trade
from backend.models.wallet import UserWallet
from backend.services.trading_engine import (
    hl_user_fills,
    hl_clearinghouse,
    hl_parse_positions,
    _aggregate_close_pnl,
)

log = logging.getLogger("backfill_pnl")
CLOSE_SLACK_SEC = 60        # tolerance for matching close fills to closed_at
OPEN_SLACK_SEC = 30         # tolerance on the opened_at lower bound


def _ms(dt) -> int:
    return int(dt.timestamp() * 1000)


def _backfill_user(
    db: Session, wallet: UserWallet, apply: bool, totals: dict
) -> None:
    user_id = wallet.user_id
    addr = wallet.address

    trades = (
        db.query(Trade)
        .filter(Trade.user_id == user_id)
        .order_by(Trade.opened_at.asc())
        .all()
    )
    if not trades:
        return

    # ── Closed trades: pull all fills since the earliest open_at, attribute by (ticker, time window).
    closed = [t for t in trades if t.status == "closed" and t.closed_at is not None]
    open_t = [t for t in trades if t.status == "open"]

    fills: list[dict] = []
    if closed:
        earliest = min(t.opened_at for t in closed) - timedelta(seconds=OPEN_SLACK_SEC)
        try:
            fills = hl_user_fills(addr, since_ms=_ms(earliest))
        except Exception as e:
            log.warning(f"[{user_id[:8]}] userFills fetch failed: {e}")
            fills = []

    log.info(
        f"[{user_id[:8]} addr={addr[:10]}…] {len(open_t)} open, {len(closed)} closed, "
        f"{sum(1 for f in fills if float(f.get('closedPnl') or 0) != 0)} closing fills"
    )

    # ── Closed trades: aggregate closedPnl + fee per (ticker, window).
    for t in closed:
        since_ms = _ms(t.opened_at) - OPEN_SLACK_SEC * 1000
        until_ms = _ms(t.closed_at) + CLOSE_SLACK_SEC * 1000
        new_pnl, new_fee = _aggregate_close_pnl(fills, t.ticker, since_ms, until_ms)
        old_pnl = float(t.pnl_usd or 0.0)
        old_fee = float(t.fee_usd or 0.0)

        delta_pnl = new_pnl - old_pnl
        delta_fee = new_fee - old_fee

        if abs(delta_pnl) > 0.01 or abs(delta_fee) > 1e-6:
            log.info(
                f"  CLOSED {t.id[:8]} {t.ticker:>6} "
                f"pnl ${old_pnl:+9.2f} → ${new_pnl:+9.2f} (Δ {delta_pnl:+8.2f})  "
                f"fee ${old_fee:.4f} → ${new_fee:.4f}"
            )
            totals["closed_changed"] += 1
            totals["pnl_old"] += old_pnl
            totals["pnl_new"] += new_pnl
            if apply:
                t.pnl_usd = new_pnl
                if new_fee > 0:
                    t.fee_usd = round(new_fee, 6)
        else:
            totals["closed_unchanged"] += 1

    # ── Open trades: pull current unrealizedPnl from clearinghouseState.
    if open_t:
        try:
            state = hl_clearinghouse(addr)
            hl_pos = hl_parse_positions(state)
        except Exception as e:
            log.warning(f"[{user_id[:8]}] clearinghouseState fetch failed: {e}")
            hl_pos = {}

        for t in open_t:
            hp = hl_pos.get(t.ticker)
            if not hp:
                # Position no longer on HL — likely closed externally; engine will sweep on next tick.
                log.info(f"  OPEN   {t.id[:8]} {t.ticker:>6} no HL position (will close on next engine tick)")
                totals["open_orphan"] += 1
                continue
            upnl = hp.get("upnl")
            if upnl is None:
                log.info(f"  OPEN   {t.id[:8]} {t.ticker:>6} HL omitted unrealizedPnl — skipped")
                totals["open_skipped_no_upnl"] += 1
                continue
            new_pnl = round(float(upnl), 2)
            old_pnl = float(t.pnl_usd or 0.0)
            if abs(new_pnl - old_pnl) > 0.01:
                log.info(
                    f"  OPEN   {t.id[:8]} {t.ticker:>6} "
                    f"pnl ${old_pnl:+9.2f} → ${new_pnl:+9.2f} (Δ {(new_pnl - old_pnl):+8.2f})"
                )
                totals["open_changed"] += 1
                if apply:
                    t.pnl_usd = new_pnl
            else:
                totals["open_unchanged"] += 1

    if apply:
        db.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write changes to DB (default: dry-run).")
    parser.add_argument("--user", default=None, help="Restrict to a user_id (prefix or full).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s  %(message)s",
    )

    db = SessionLocal()
    try:
        q = db.query(UserWallet).filter(UserWallet.is_active.is_(True))
        if args.user:
            # Allow either full UUID or a prefix.
            q = q.filter(UserWallet.user_id.like(f"{args.user}%"))
        wallets = q.all()

        if not wallets:
            log.error("No matching active wallets found.")
            return 1

        log.info(
            f"Backfill {'APPLY' if args.apply else 'DRY-RUN'} mode — {len(wallets)} wallet(s)"
        )

        totals: dict = defaultdict(int)
        totals["pnl_old"] = 0.0
        totals["pnl_new"] = 0.0
        for w in wallets:
            try:
                _backfill_user(db, w, apply=args.apply, totals=totals)
            except Exception as e:
                log.error(f"[{w.user_id[:8]}] backfill failed: {e}", exc_info=True)

        log.info("───────────────────────────────────────────")
        log.info(f"Closed trades changed:    {totals['closed_changed']}")
        log.info(f"Closed trades unchanged:  {totals['closed_unchanged']}")
        log.info(f"Open trades changed:      {totals['open_changed']}")
        log.info(f"Open trades unchanged:    {totals['open_unchanged']}")
        log.info(f"Open trades orphaned:     {totals['open_orphan']}")
        log.info(
            f"Closed pnl_usd: old ${totals['pnl_old']:+,.2f} → new ${totals['pnl_new']:+,.2f} "
            f"(Δ ${(totals['pnl_new'] - totals['pnl_old']):+,.2f})"
        )
        if not args.apply:
            log.info("Dry-run only. Re-run with --apply to write.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
