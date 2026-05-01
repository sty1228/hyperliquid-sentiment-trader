# Rewards system audit — 2026-05-02

Read-only inventory of every existing rewards / referral / fee-share code path before retrofitting against the boss-approved Points Program v1.0 spec.

> Scope note: `hyper-copy-points-program-spec.md` was not found in the repo (`grep -ril "points program" .` returns nothing under git-tracked paths). The spec is presumably in the Claude.ai project knowledge base, which is not visible to this audit. All "spec requirements" rows in §B below are inferred from concrete clues in the audit prompt itself — phase multiplier, hold duration, attribution weight, 2-tier referrer fees, per-trade points, etc. The next implementation pass should re-confirm requirements directly against the spec before writing code.

---

## A. Current State (what's actually in the code)

### A.1 `backend/services/rewards_engine.py` (228 LOC)

Two top-level functions, called from `trading_engine.py` every 10 minutes via `recompute_kol_points` and `run_weekly_distribution`.

**`recompute_kol_points(db)`** — recomputes `KOLReward.current_week_points` for every user whose `twitter_username` matches a `Trader.username` (a "KOL"). Per-KOL formula:

```
copy_vol      = SUM(Trade.size_usd) WHERE trader_username=KOL AND source='copy' AND opened_at in [w_start, w_end)
copy_pts      = copy_vol / 100                                                # 1 pt per $100 copied
n_sigs / wins = signals from this KOL this week with non-null entry_price
win_rate      = wins / n_sigs (only counted if n_sigs ≥ 3)
quality_pts   = int(win_rate * 100)
x_boost       = 1.5 if x_account_handle else 1.0                              # X_LINKED_BOOST = 1.5
n_shares      = COUNT(ShareEvent) for this user this week
share_boost   = 1.0 + min(n_shares * 0.10, 1.0)                               # capped at 2.0x
total_points  = int( (copy_pts + quality_pts) * x_boost * share_boost )
```

After loop, ranks all KOLs by total points desc and writes `KOLReward.rank`.

**`run_weekly_distribution(db)`** — idempotent: skips if a `KOLDistribution` row already exists for `prev_week`. Uses **all platform-wide trade size_usd** for the previous week to compute total fees:

```
total_vol  = SUM(Trade.size_usd) across the platform for prev_week
total_fees = total_vol * 0.001                                                # BUILDER_FEE_BPS = 10 bps
kol_pool   = total_fees * 0.60                                                # BETA_FEE_SHARE_PCT = 60%
share_usd  = (this KOL's points / sum-of-all-points) * kol_pool
```

Writes one `KOLDistribution` row per KOL with `share_usd`, then resets `current_week_points = 0` and accumulates `total_points` and `claimable_fee_share`. Status is set to `paid` immediately — **no actual USDC transfer happens here** (paid-on-paper only; the real transfer is on user-initiated `claim-fee-share`).

**Where points get stored**: `KOLReward.current_week_points` (live), `KOLReward.total_points` (lifetime), `KOLDistribution.total_points` (per-week historic).

### A.2 `backend/api/rewards.py` (539 LOC)

Mounted at `/api/kol`. Endpoints:

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/kol/rewards` | User's `RewardsResponse`: phase, week, total/current points, rank, total/claimable fee share, X-linked, boost multiplier, **plus** `referralBoostActive`, `freeTradesRemaining`, `affiliateEarned` |
| GET | `/api/kol/distributions?limit=N` | User's `DistributionsResponse`: per-week records with breakdown (copy_volume_points, own_trading_points, signal_quality_bonus, x_boost, sf_boost, fee_share_usdc, status, referralBoost) |
| POST | `/api/kol/share` | Logs a `ShareEvent` row (pnl_card / leaderboard) and returns `{success, shareId, message}`. Used as multiplier input for points. |
| POST | `/api/kol/claim-fee-share` | Initiates a USDC transfer of `claimable_fee_share` from master wallet to the user's `withdraw_address` via `master_transfer_usdc` in a background thread. Deducts immediately, refunds on failure. |

Internal helpers worth noting:

- `_compute_weekly_points(db, user_id, week_start)` — **a SECOND, DIFFERENT points formula** in addition to `rewards_engine.recompute_kol_points`. Called from `on_trade_placed` after every trade. Formula:
  ```
  copy_volume_pts      = (subquery: trader_username from this user's trades) → SUM(Trade.fee_usd) → / 0.10  # buggy; see §C
  own_trading_pts      = SUM(Trade.size_usd WHERE user_id=me, source IN copy/counter) / 5.0
  base_pts             = copy_volume_pts + own_trading_pts
  signal_quality_bonus = win_rate * 50 if matching trader has signals
  x_boost              = 1.2 if twitter_username else 1.0                    # NOTE 1.2 here vs 1.5 in engine
  sf_boost             = min(1.0 + smart_follower_count * 0.1, 5.0)          # cap 5.0 vs 2.0 in engine
  total_before_ref     = (base_pts + signal_quality_bonus) * x_boost * sf_boost
  total_pts            = total_before_ref * 1.15 if user is referred         # REFERRAL_POINTS_BOOST
  ```

- `_accrue_affiliate_revenue(db, user_id, fee_usd)` — when a referred user pays a fee, credits 20% (`AFFILIATE_REVENUE_SHARE = 0.20`) to the **direct referrer's** `claimable_fee_share`. **One tier only** — there is no `referrer_of_referrer` lookup anywhere in the codebase (confirmed by grep).

- `on_trade_placed(db, user_id, fee_usd)` — exported hook called from `trading_engine._execute_for_user`. Does two things: accrues affiliate share, and overwrites `current_week_points` via `_compute_weekly_points`. **This collides with `recompute_kol_points`** — see §C.

### A.3 `backend/api/referral_api.py` (279 LOC)

Loaded conditionally by `backend/main.py` (graceful skip if missing). It exists. Mounted at `/api`. Endpoints:

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/referral/info` | yes | User's `ReferralInfoResponse`: code, link, invited/active counts, `earned_usd` (= 20% of referred users' total fees), `free_trades_remaining`, `invited_by`, global slot counts, affiliate application status |
| GET | `/api/referral/public-slots?code=X` | no | Public landing-page data: total slots, slots used, free-tier full flag, inviter info, `code_valid` |
| POST | `/api/referral/apply-code` | yes | Applies a code to the current user; creates `ReferralUse` row (`is_active=True` directly), sets `users.referral_code_used`, returns `{ok, free_trades_granted: 10}` |
| POST | `/api/referral/affiliate-apply` | yes | Creates an `AffiliateApplication` row with `status='pending'` |
| GET | `/api/referral/validate-code/{code}` | no | Returns `{valid: True, inviter_username}` or 404 |

**2-tier referral**: not supported. The model has only `Referral` (user → code) and `ReferralUse` (referrer_user_id → referred_user_id) — flat one-hop. There is no schema for "referrer of the referrer". The accrual code (`_accrue_affiliate_revenue`) reads exactly one `ReferralUse` row and credits exactly one parent.

**Tier 1/2 fee split — no existing logic**: the only fee distribution mechanism is the 60% pool in `run_weekly_distribution` (split among all KOLs by points share) plus the flat 20% direct-referrer accrual in `_accrue_affiliate_revenue`. There is no path that splits a single trade's builder fee across multiple recipient addresses.

### A.4 Tables

`backend/models/rewards.py` (KOL-side, 110 LOC) and `backend/models/referral.py` (referral-side, 30 LOC). **None of these tables have alembic migrations** — they were created via the inline `Base.metadata.create_all(...)` instruction in the `rewards.py` header comment, lines 7-9. This is an instance of the same model/migration drift previously flagged.

**`kol_rewards`** — one row per user, lifetime + current state.

| Column | Type | Spec-relevant? |
| --- | --- | --- |
| `id` | String(36) PK | — |
| `user_id` | String(36) UNIQUE | — |
| `total_points` | Integer | lifetime points |
| `current_week_points` | Integer | live week points (overwritten by both engines, see §C) |
| `rank` | Integer nullable | week-level rank from `recompute_kol_points` |
| `total_fee_share` | Float | lifetime USDC earned |
| `claimable_fee_share` | Float | unclaimed USDC balance |
| `smart_follower_count` | Integer | proxy for `n_shares` in engine; **NOT a real "smart follower" count** |
| `boost_multiplier` | Float | precomputed combined boost (x_boost × share_boost from engine) |
| `x_account_linked` | Boolean | mirrors User.twitter_username presence |
| `x_account_handle` | String(100) | denormalized X handle |
| `current_phase` | String(20) | `'beta'` only — no advancement code; **could carry phase multiplier later** |
| `created_at` / `updated_at` | DateTime | — |

**`kol_distributions`** — one row per (user, week). Status enum: `pending | paid | failed`.

| Column | Type | Spec-relevant? |
| --- | --- | --- |
| `id`, `user_id` | String(36) | — |
| `week_number` | Integer | — |
| `distribution_date` | DateTime | — |
| `total_points` | Integer | — |
| `copy_volume_points` | Integer | breakdown |
| `own_trading_points` | Integer | breakdown |
| `signal_quality_bonus` | Integer | breakdown |
| `x_account_boost` | Float | applied multiplier |
| `smart_follower_boost` | Float | applied multiplier |
| `fee_share_usdc` | Float | weekly $ USDC payout |
| `status` | enum | always `paid` today (paid-on-paper) |
| `created_at` | DateTime | — |

Schema lacks: `phase_multiplier`, `hold_duration_avg`, `attribution_weight`, `referral_boost` (response field reads via `getattr(r, "referral_boost", 1.0)` → currently always falls back to 1.0).

**`share_events`** — append-only log of PnL-card / leaderboard shares.

| Column | Type | Spec-relevant? |
| --- | --- | --- |
| `id`, `user_id` | String(36) | — |
| `share_type` | enum (pnl_card / leaderboard) | — |
| `target_platform` | String(20) default 'x' | — |
| `reference_id` | String(100) nullable | trade_id or leaderboard snapshot |
| `created_at` | DateTime | drives `n_shares` / `share_boost` calc |

**`referrals`** — one per user (UNIQUE on `user_id`).

| Column | Type | Notes |
| --- | --- | --- |
| `id` | String(36) PK | — |
| `user_id` | String(36) FK users UNIQUE | — |
| `code` | String(20) UNIQUE | random 7-char prefix from twitter_username + 2 digits |
| `created_at` | DateTime | — |

**`referral_uses`** — one per referred user (UNIQUE on `referred_user_id`).

| Column | Type | Notes |
| --- | --- | --- |
| `id` | String(36) PK | — |
| `referrer_user_id` | String(36) FK users | the parent |
| `referred_user_id` | String(36) FK users UNIQUE | the child |
| `code` | String(20) | snapshot of used code |
| `created_at` | DateTime | — |
| `is_active` | Boolean default False | set to True on apply (line 246 of referral_api.py); never toggled back |

No 2-tier fields. No grandparent reference. No fee-allocation columns.

**`affiliate_applications`** — one per applicant.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | String(36) PK | — |
| `user_id` | String(36) FK users UNIQUE | — |
| `status` | String(20) default 'pending' | `pending | approved | rejected` |
| `notes` | Text nullable | manual review notes |
| `created_at` | DateTime | — |

**No automatic transition logic** exists for `affiliate_applications.status` — it stays `pending` until manually changed in the DB.

### A.5 `FREE_COPY_TRADES_LIMIT = 10` mechanic

Defined twice (`trading_engine.py:75` and `api/referral_api.py:16`). Triggered from `_execute_for_user` (`trading_engine.py:537-538`):

```python
free_trades_left = _get_free_trades_remaining(db, user_id)   # = max(0, 10 - users.free_copy_trades_used)
is_fee_free      = free_trades_left > 0
builder_bps      = 0 if is_fee_free else BUILDER_BPS_DEFAULT  # = 10 normally
```

Counter is `User.free_copy_trades_used`. Only granted to users who have used a referral code (`User.referral_code_used` non-null). When a copy/counter trade fires:
- If user is referred and used < 10 → `builder_bps` passed to HL is **literally 0** (the actual order has no builder fee), and `Trade.is_fee_free=True`, `Trade.fee_usd=0.0`.
- After successful fill, `_consume_free_trade(db, user_id)` increments the counter (line 605-609).

Manual trades (`POST /api/trades/manual`) **do not** consume the free-trade quota and **do not** get the fee-free path — they always pay full builder fees. This is intentional per the manual-trading PR (commit `2cd2c52`).

### A.6 Master wallet money flow (`backend/services/wallet_manager.py`)

```
User pays builder fee on a trade
        │
        ▼
HL builder address (HL_BUILDER_ADDRESS env)  ←  literally a HyperLiquid address; HL's order
                                                 placement debits this on each fill
        │
        ▼
(currently: stays on the HL platform's books for that builder address)
        │
        ▼
Manual sweep step (NOT IMPLEMENTED in code)  →  master Arb wallet (GAS_STATION_ADDRESS)
        │
        ▼
master_transfer_usdc(dest, amount)  →  user's withdraw_address
                                        (called from claim-fee-share endpoint)
```

**There is no code path that automatically moves USDC from the HL builder address into the master wallet, nor any code path that splits a single incoming USDC payment across multiple recipients.** Distribution today is purely accounting:
- `kol_pool` is computed from total volume × fee bps (paper number).
- Per-KOL `fee_share_usdc` is written to `KOLDistribution.fee_share_usdc` (paper number).
- USDC actually leaves the master wallet only when a user calls `POST /api/kol/claim-fee-share` (single recipient via `master_transfer_usdc`).

The single transfer primitive `master_transfer_usdc(dest, amount)` is one-to-one (one recipient per call). For Tier 1/Tier 2 simultaneous payouts you'd loop over recipients and emit N transfers, or pre-batch into a multicall — neither exists today.

---

## B. Spec Requirements vs Current State (gap table)

Inferred spec requirements (the spec doc itself is not visible in the repo). **Re-confirm against the actual spec before implementation.**

| Spec area (inferred) | Current code | Gap |
| --- | --- | --- |
| Per-trade points (volume / count / P&L / fees) | Two separate formulas: `recompute_kol_points` uses `size_usd`; `_compute_weekly_points` uses fee + size mixed | **Major.** Pick one formula. Current dual-engine setup is a bug (§C-1). |
| Phase multiplier on points | `KOLReward.current_phase` exists but is just a label; no `phase_multiplier` applied | **Missing.** Add `phase_multiplier` column on `KOLReward` and/or `KOLDistribution`; pipe through both engines. |
| Hold-duration weighting on points | No column tracks hold duration; trades have `opened_at`/`closed_at` so it's derivable but not currently used in any points calc | **Missing.** Compute on-demand in points formula or denormalize a new column. |
| Attribution-weight (KOL accuracy) | `signal_quality_bonus` from win-rate is the closest proxy; no general-purpose attribution column | **Partial.** Existing `quality_pts` / `signal_quality_bonus` may suffice, depending on spec definition. |
| Current points balance for user | `GET /api/kol/rewards` returns `currentWeekPoints` + `totalPoints` + `rank` | **Met.** No new endpoint needed. |
| Weekly distribution log endpoint | `GET /api/kol/distributions` returns `DistributionItem[]` with full breakdown | **Met.** May need new fields per spec. |
| 2-tier referral fee split | One-hop only (`_accrue_affiliate_revenue` reads single `ReferralUse`); no grandparent lookup | **Missing.** Need a recursive lookup OR a denormalized `level_2_referrer_user_id` on `ReferralUse`. |
| Tier 1/Tier 2 USDC payout | No code path splits incoming USDC to multiple recipients | **Missing.** Either (a) accumulate to `claimable_fee_share` for both tiers and let each claim independently (cheapest), or (b) batch transfer at distribution time. |
| Builder-fee → platform → splits | Master wallet sweep from HL builder address is undocumented + unimplemented | **Missing.** Need a periodic sweep job that moves accrued builder fees off HL into the master Arb wallet before any payout can happen. |
| Affiliate application auto-approve | `AffiliateApplication.status` stays `pending` forever — no auto-transition | **Missing or intentional.** Spec may require manual approval; if not, add a threshold-based promoter. |

**Already-met spec areas worth flagging so we don't redo them:**
- `KOLReward.total_fee_share` + `claimable_fee_share` (lifetime + unclaimed USDC).
- `ShareEvent` log driving a multiplier (currently `share_boost`).
- Per-week breakdown response shape (`DistributionBreakdown`).
- `User.referral_code_used` (durable mark of "this user was referred", drives all referred-user logic).
- `Trade.is_fee_free` + `Trade.fee_usd` (already tracking which trades did and didn't pay builder fees).

---

## C. Blockers / Open Questions

### C-1. Two separate points formulas race-write the same field

`KOLReward.current_week_points` is overwritten by **both**:

- `recompute_kol_points` every 10 min (engine path).
- `_compute_weekly_points` after every trade via `on_trade_placed` (API path).

The two formulas disagree on every component (different pt-per-$ rate, different x_boost magnitude, different cap on smart-follower boost, different signal-quality multiplier). Last-writer-wins. This is almost certainly a bug — at minimum a merge conflict from two parallel implementations that never got reconciled. **Pick one canonical formula** before adding any spec retrofits, or both will continue to scribble over each other.

Recommendation: keep `recompute_kol_points` as the canonical engine and delete `_compute_weekly_points` (or have `on_trade_placed` only do the affiliate accrual, not the points overwrite). The engine's once-every-10-min cadence is enough and avoids the race.

### C-2. `_compute_weekly_points` copy-volume-points subquery is wrong

The query at `api/rewards.py:249-258`:

```python
copy_fees = db.query(func.sum(Trade.fee_usd)).filter(
    Trade.trader_username == (
        db.query(Trade.trader_username)
        .filter(Trade.user_id == user_id)
        .limit(1)
        .scalar()
    ),
    ...
)
```

Picks an arbitrary `trader_username` from any of this user's recent trades, then sums all platform-wide fees on that trader_username. This conflates "the user's own KOL signal fees" with "fees from anyone copying that trader". For a non-KOL user, the subquery returns the username of someone they copied — and we then attribute that KOL's entire fee stream to our user's points. This will badly inflate `copy_volume_pts` for any heavy copier.

Worth fixing whether we keep the dual-engine setup or collapse to one — this query is wrong on its own terms.

### C-3. No alembic migrations for any rewards/referral tables

`kol_rewards`, `kol_distributions`, `share_events`, `referrals`, `referral_uses`, `affiliate_applications` — all six tables exist in prod via the `Base.metadata.create_all()` line in the `rewards.py` model header comment (and presumably equivalent for referral). No alembic revision exists for any of them. A fresh-clone deploy via `alembic upgrade head` will not create these tables.

This needs to be captured into alembic before any spec retrofit ships, otherwise:
- Adding a new column (e.g. `phase_multiplier`) via alembic on prod will fail because alembic doesn't know the table exists.
- Or the new migration silently autogen-detects "tables don't exist, create them" and tries to run `CREATE TABLE` against an already-populated prod DB.

Fix is straightforward: write one alembic revision that uses `op.execute("CREATE TABLE IF NOT EXISTS ...")` (mirroring the `e60adfb8d09b` pattern from this morning) for each table. Once that lands, schema drift on these tables can be managed normally.

### C-4. Distribution status is always `paid` — no actual on-chain proof of payment

`run_weekly_distribution` writes `status=DistributionStatus.paid` immediately, regardless of whether any USDC has actually moved. The "real" payment happens when the user calls `claim-fee-share`. So `KOLDistribution.status='paid'` does NOT mean USDC is in the user's wallet — it means "we've credited the points-to-USDC math to this user's `claimable_fee_share`".

Spec may want an `accrued | claimed | paid_on_chain` status progression with separate columns/timestamps. Today there is no record of when the user actually claimed.

### C-5. KOL pool is computed from total platform volume, not from realized fees

`run_weekly_distribution`:

```python
total_vol  = SUM(Trade.size_usd) for the week
total_fees = total_vol * 10 / 10_000     # 10 bps
kol_pool   = total_fees * 0.60
```

This ignores `Trade.is_fee_free` (the FREE_COPY_TRADES_LIMIT path produces trades with `fee_usd=0` but `size_usd>0`). So fee-free trades inflate the pool — we'd be paying USDC for fees we never actually collected. Easy fix: `total_fees = SUM(Trade.fee_usd)` (the column exists and is accurate per-trade).

### C-6. `smart_follower_count` is misnamed

In `recompute_kol_points` (engine), `smart_follower_count` is set to `n_shares` (the count of `ShareEvent`s this week). It's not actually a follower count — it's a share count. The label leaks into `RewardsResponse.smartFollowerCount` and the corresponding `boost_multiplier` is also a share-derived value, not a follower-derived one. Whoever wrote the API path knew this (`_compute_weekly_points` calls it `sf_boost` and uses the same field), so it's a consistent rename but still misleading. Spec may actually want a real "smart follower" concept (e.g. high-XP / high-volume followers); if so, that's a new concept entirely.

### C-7. Open spec questions that the audit can't answer

Things only the spec doc can clarify. Listing for the implementation pass:

1. Is "phase multiplier" applied to (a) points, (b) fee-share USDC payout, or (c) both?
2. Does "hold duration" weight reward longer-held trades more (loyalty) or shorter ones (discipline)?
3. For 2-tier referrals, what's the split? Common patterns: 70/30 / 80/20 / 90/10 of the affiliate share. Need a number.
4. Tier 2 cutoff — flat across all users, or only for "approved affiliates" (the existing `affiliate_applications.status='approved'` flag)?
5. Does the spec touch the `FREE_COPY_TRADES_LIMIT = 10` mechanic at all? It's an existing referral-side promotion that may or may not survive the new program.
6. Does the spec define a "claim cooldown" / minimum-claim-amount? Today it's any-time, any-amount > 0.

---

## D. Suggested Implementation Order

Order minimizes risk by paying down the existing-tech-debt blockers before stacking spec features on top. Each step is a separate PR with its own validation gate.

1. **Resolve the dual-engine collision (C-1) and the buggy subquery (C-2).** Delete `_compute_weekly_points` (or strip it down to "affiliate accrual only"). Make `recompute_kol_points` the canonical points engine. Add a unit test that asserts a single trade increments the engine's expected points by the engine's documented rate. **Blocks everything below.**
2. **Capture all six rewards/referral tables into alembic (C-3).** One migration with `CREATE TABLE IF NOT EXISTS` so prod (which has them already) is a no-op. Mirror the `e60adfb8d09b` idempotency pattern shipped this morning.
3. **Fix the pool-inflation bug (C-5).** Change `run_weekly_distribution` to `total_fees = SUM(Trade.fee_usd)` instead of computing from `total_vol`. One-line fix; matters for accuracy of the pool.
4. **Decide and pin the canonical points formula** (re-read spec). Update `recompute_kol_points` to match. Migrate any new columns required by the formula (`phase_multiplier`, `hold_duration_avg`, `attribution_weight`, etc.) via alembic — now safe because step 2 captured the tables.
5. **Add 2-tier referral support.** New column on `ReferralUse`: `level_2_referrer_user_id` (nullable, FK users). Backfill for existing rows where the parent's parent is non-null. Update `_accrue_affiliate_revenue` to credit both tiers per the spec's split. New endpoint or extend `/api/referral/info` with `tier1_earned_usd` + `tier2_earned_usd`.
6. **Implement the master-wallet sweep (C / §A.6 gap).** Periodic worker that moves accrued builder fees from the HL builder address into the master Arb wallet — a prerequisite for actually paying out anything beyond what's already in master. Run on the same cadence as `deposit_monitor`.
7. **Distribution status state machine (C-4).** Split `DistributionStatus.paid` into `accrued | claimed | paid_on_chain`; add `claimed_at` and `tx_hash` columns to `KOLDistribution`. Wire `claim-fee-share` to advance the status per row.
8. **(Optional, depending on spec) Smart-follower rename.** If the spec uses "smart followers" as a different concept, rename `smart_follower_count` to `share_count` in models + API responses, and add a separate `smart_follower_count` derived from a real follower-quality signal.
9. **Phase advancement code.** `KOLReward.current_phase` is currently `'beta'` forever. Whatever cuts over to `season1` per `PHASE_CONFIG` needs to be written.

Steps 1-3 are pure tech-debt cleanup and should land before any spec-feature commits — they remove existing-state ambiguity that would otherwise block testing.

Steps 4-7 are the actual spec retrofit. Each is independent enough to ship behind its own validation.

Step 8-9 are polish; defer if scope is tight.

---

*Audit generated 2026-05-02 by reading the listed files only. No code modified. No spec-keeper invocation. Deliverable: this file at the repo root, then a single commit.*
