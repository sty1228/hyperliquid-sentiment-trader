# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 1. Overview

HyperCopy is a copy-trading platform on top of HyperLiquid perp DEX. A Twitter/X ingestor turns KOL tweets into LLM-labeled trading signals, and a trading engine fans those signals out as real perp orders on Hyperliquid for every follower's custodial wallet. A FastAPI app serves auth, leaderboard, follow toggles, copy settings, portfolio, deposit/withdraw, and rewards to web/mobile clients.

## 2. Architecture

**FastAPI app** (`backend/main.py`, port 8000): mounts the routers in §3, wires Sentry, slowapi rate-limiter (60/min default), and CORS from `CORS_ORIGINS`.

**Five long-running processes** (each its own `python -m …` entrypoint, intended to be one systemd unit per process):

| Process | Module | Purpose |
| --- | --- | --- |
| API | `backend.main:app` (via `run.py` / uvicorn) | HTTP API |
| Trading engine | `backend.services.trading_engine` | signals → trades, TP/SL, equity guard, balance/stats sync — 15s loop |
| Ingestor | `backend.ingestor.main` (or `backend.services.ingestor_loop`) | X scrape → LLM label → `signals` rows |
| Deposit monitor | `backend.services.deposit_monitor` | watches user wallets for USDC, bridges to HL, processes withdrawals — 15s loop |
| Max-gain updater | `backend.services.max_gain_updater` | recomputes `signals.max_gain_pct` from HL klines — 5 min loop or `--once` |

**Data flow:** X API → ingestor (LLM + HL token whitelist + confidence gate) → `signals` (Postgres) → trading engine reads `signals` + `follows` → submits orders via `wallet_manager.execute_copy_trade` → `trades` rows + HL fills → engine reconciles position state from HL `clearinghouseState` each tick.

**Two execution paths exist; only one is live.** `backend/services/` is production (Postgres + SQLAlchemy + HL SDK). `execution/` is an older SQLite path (`data/execution.sqlite`) kept for reference and listed in `.claudeignore` — do not extend it.

**Supporting service modules** (not long-running, used by the workers above):

- `bybit_price_tracker.py` — HL-first price tracker with Bybit fallback for klines; used by signal price updates.
- `enhanced_price_database.py` — SQLite-backed price cache (`data/crypto_tracker.db`); stores and retrieves OHLCV data.
- `hyperliquid_broker.py` — FastAPI router (`/api/hl`) wrapping the HL SDK; handles order placement and account queries.
- `price_source_base.py` — abstract `PriceSource` interface implemented by concrete sources.
- `rewards_engine.py` — KOL points computation and weekly fee-share distribution; called by the trading engine every 10 min.
- `sources/` — price-source implementations: `bybit_source.py` (Bybit REST) and `hyperliquid_sdk_source.py` (HL allMids).

## 3. API surface

All routers live in `backend/api/`. Prefix is `/api` unless noted.

- `health.py` — `/health` DB + HL + master wallet liveness (no prefix).
- `auth.py` — `/api/auth/*` wallet-connect → JWT issuance, dual-account merge by `twitter_username`. When the merge deactivates an orphan user owning the same wallet, that orphan's `wallet_address` is overwritten with `deact_<uuid-hex>` (38 bytes, preserves the UNIQUE constraint, no payload encoded — the merge target is recoverable from logs). Resilient to the `uq_users_twitter_username` partial unique index (added by manual SQL on prod 2026-05-02): if a twitter_username collision raises `IntegrityError` at the Step 2 attach or the Step 3 INSERT, the handler rolls back, resolves the **oldest active** user with that twitter_username as canonical, and issues the JWT for that user (no data mutation; secondary wallet stays external; logs a `TWITTER_CONFLICT` WARN with both wallet addresses). Helpers: `_is_twitter_username_conflict`, `_resolve_canonical_by_twitter`.
- `leaderboard.py` — KOL leaderboard reads from `trader_stats`.
- `trader.py` — KOL profile, signals list, radar score, follow context.
- `follow.py` — follow/unfollow + `is_copy_trading` / `is_counter_trading` (mutually exclusive).
- `settings.py` — copy-trade defaults and per-trader overrides (size, leverage, TP/SL, max positions).
- `portfolio.py` — balance, open positions, PnL curve, per-KOL realised PnL. `pnl-history` percentage uses cost basis (net deposits − withdrawals from `balance_events`) as the ROI denominator, not current balance — current-balance denominators shrink artificially as PnL grows. ALL-range queries clamp the chart start to the user's first `BalanceEvent` or `Trade` rather than 2020-01-01. Response includes `cost_basis: float`; returns 0 when the user has no deposit history (frontend treats 0 as "—").
- `trades.py` — trade history + manual close.
- `alerts.py` — in-app notifications (trades / social / system).
- `wallet.py` — dedicated-wallet address, on-chain balance, withdraw initiation (multi-chain via Stargate V2).
- `deposit.py` — **deprecated** (returns 410); legacy ledger endpoints replaced by wallet flow.
- `explore.py` — token sentiment, token detail, rising traders, search.
- `rewards.py` — KOL points, weekly distributions, share events, fee-share claim.
- `referral_api.py` — referral codes, free-trade allotment, affiliate revenue share. Loaded conditionally; missing file is non-fatal.

## 4. Data model

**Postgres (canonical store, `hypercopy` db).** Tables, with the columns/constraints worth knowing:

- `users` — `id` (uuid str), `wallet_address` (VARCHAR(128), unique — wider than EOA hex to accommodate `deact_<uuid>` deactivation markers from dual-account merges), `twitter_username` (indexed; partial UNIQUE index `uq_users_twitter_username` WHERE `twitter_username IS NOT NULL` enforces one active row per username — added by manual SQL 2026-05-02 alongside a one-time cleanup of 3 dual-account duplicates), `referral_code_used`, `free_copy_trades_used`.
- `traders` — KOLs. `username` unique. `avatar_url`, `is_verified`, follower counts.
- `trader_stats` — pre-computed leaderboard rows. Unique `(trader_id, window)` where `window ∈ {24h, 7d, 30d}`. Recomputed every 10 min.
- `signals` — one row per labeled tweet. `(trader_id, ticker, direction, sentiment)` core; `entry_price` / `current_price` / `pct_change` updated every tick; `max_gain_pct` + `max_gain_at` monotonic peak-favorable-excursion. `tweet_id` unique. `tweet_image_url` (Text, nullable) — attached image URL passed to the vision pass. `status ∈ {active, processed, expired, skipped}`. **Always order by `coalesce(tweet_time, created_at)`** — tweet_time is preferred but nullable.
- `follows` — unique `(user_id, trader_id)`. `is_copy_trading` and `is_counter_trading` are mutually exclusive (validated in API and DB defaults).
- `trades` — one row per opened position. `signal_id` nullable (manual trades). `status ∈ {open, closed}`, `source ∈ {copy, counter, manual}`, `fee_usd` + `is_fee_free` for affiliate accounting. `pnl_usd` is **HL-authoritative** — set from `clearinghouseState.assetPositions[].position.unrealizedPnl` while open, and from `userFills[].closedPnl` summed over closing fills at close time; never locally computed. `pnl_pct` is asset-price % change used for TP/SL threshold comparison only, not USD-PnL-derived. Note: a `realized_pnl_usd` column existed briefly (2026-04-28 → 2026-05-01) as a partial-close accumulator; dropped (migration `acb0b86b0ff2`) because HL fill history is the source of truth.
- `copy_settings` — unique `(user_id, trader_id)` with `trader_id NULL` = the user's default. `size_type ∈ {percent, fixed_usd}`, `margin_mode ∈ {cross, isolated}`, `tp_value` / `sl_value` in percent.
- `balance_snapshots` — daily equity per user (unique on `snapshot_date`); written by `sync_balances`.
- `balance_events` — intraday deposit/withdraw events with `balance_after` for charting.
- `user_wallets` — one per user. `address` unique, `encrypted_private_key` (Fernet), `withdraw_address`, `is_active`, `withdraw_pending`.
- `wallet_deposits` — append-only ledger of detected on-chain USDC + outbound bridges.
- `alerts` — user notifications; `is_read` flag.
- `referrals`, `referral_uses`, `affiliate_applications` — referral code issuance and use.
- `kol_rewards`, `kol_distributions`, `share_events` — KOL rewards programme.

**SQLite ingestor state (`data/`, not in git):**

- `ingestor_state.sqlite` — per-user `since_id` for incremental X polling, `last_polled_at`, `avg_tweets_per_day`, `empty_polls`, `consecutive_errors`. Drives the 3-tier polling cadence.
- `label_cache.sqlite` — content-addressed cache of LLM labels keyed by `_stable_tweet_hash(text)`. Prevents paying OpenAI twice for the same tweet.
- `execution.sqlite` — used by the dormant `execution/` path only.

Migrations live under `alembic/versions/`. Every model change requires `alembic revision --autogenerate -m "…"` then `alembic upgrade head`.

## 5. Trading engine

`backend/services/trading_engine.py::run` — main loop, `LOOP_SLEEP_SEC = 15`. Order matters:

1. `process_new_signals` — `signals` where `status='active'` AND `created_at >= now-5min`, batched 50, dispatched to all matching `follows`.
2. `expire_old_signals` — anything `active` older than 5 min → `expired`.
3. `update_positions` — pull HL `clearinghouseState` once per user; for each open trade set `pnl_usd = position.unrealizedPnl` (HL-authoritative, no local math) and `pnl_pct = (mid - entry) / entry * 100` for TP/SL threshold comparison only. On detected external close (HL position size ~0), set `pnl_usd` from `userFills[].closedPnl` aggregated since `opened_at` — one extra HL call per close event, never per tick. Fire TP/SL via `_close_trade`, which uses the same `userFills` lookup post-fill. Helpers: `hl_user_fills`, `_aggregate_close_pnl`, `_fetch_close_pnl_for_trade`.
4. `check_equity_protection` — force-close all of a user's positions if HL equity < `MIN_EQUITY_CLOSE_ALL` (`$2`).
5. `update_signal_prices` — refresh `current_price` and `pct_change` on signals from last 30d. Backfills missing `entry_price` from current mid.
6. `sync_balances` — every 5 min, upsert `BalanceSnapshot` for the day.
7. `recompute_stats` + `recompute_kol_points` + `run_weekly_distribution` — every 10 min.

**Open conditions** (`_execute_for_user`, all must pass): user has no open trade on the same ticker (any source), `equity ≥ EQUITY_SKIP_THRESHOLD = $5`, `withdrawable ≥ MIN_TRADE_USD = $10`, open count `< max_positions`, no duplicate by `signal_id`, ticker is in HL meta. Allocation = `size_value` (USD or % of equity), capped to `min(equity*0.9, withdrawable*0.9)`. Counter trades flip direction. First `FREE_COPY_TRADES_LIMIT = 10` trades for a user with `referral_code_used` set are fee-free (`builder_bps = 0`).

**Close conditions:** `pnl_pct ≥ tp_value` (TP), `pnl_pct ≤ -sl_value` (SL), HL position size goes to ~0 externally (treated as closed at current mid), or equity protection triggers (closes everything).

**Edge cases — preserve these when editing:**

- **Same-ticker conflict guard.** A user may have only one open trade per coin regardless of which trader it came from. HL nets positions, so two opposing trades on the same coin would silently cancel and a later reduce-only close would fail.
- **Withdrawable cap.** HL rejects orders where required margin exceeds free margin even if equity is sufficient. Always cap by `min(equity*0.9, withdrawable*0.9)`.
- **Ghost-position prevention.** If the HL order fills but the SQLAlchemy commit fails, `_emergency_close_position` immediately fires a reduce-only close on HL using the same key. Do not move the `db.flush()` after the HL call without preserving this safety net.
- **Builder-fee auto-approve.** First trade on a wallet may fail with `"Builder fee has not been approved"`; the engine calls `approve_builder_fee_for_wallet` and retries once. Approved wallets are cached in process memory (`_approved_wallets`) — restart clears the cache.
- **Price rounding.** HL requires 5 significant figures; use `_round_price`, not `round(x, n)`.
- **PnL is HL-authoritative.** Never set `trade.pnl_usd` from a local formula. Open: read `clearinghouseState.assetPositions[].position.unrealizedPnl`. Closed: sum `userFills[].closedPnl` for that ticker over the trade's lifetime. The previous local formula `pnl_pct/100 × size_usd × leverage` multiplied by leverage one extra time relative to HL's accounting, inflating values by a factor of ~leverage (observed 5.4× on one user). `size_usd` is and has always been margin, not notional — the bug was in the formula, not the schema.
- **Partial close PnL is best-effort log-only.** When `POST /api/trades/{id}/partial-close` fires, the slice's realized PnL is queried from `userFills` within a 30-second lookback window for logging and the SSE event payload, but it is **not persisted** to the trade row. `trade.pnl_usd` stays as the live unrealizedPnl of the *remaining* open position; the next `update_positions` tick refreshes it from HL after the partial close completes. The 30s window can occasionally miss a fill on slow propagation — accepted trade-off, since the next-tick refresh and HL fill history together remain authoritative.

For deeper detail on order-result parsing, leverage updates, and HL meta refresh, read `trading_engine.py` end-to-end — it's ~1000 lines and self-documenting.

## 6. Ingestor

`backend/ingestor/main.py::run_daemon` — long-running. Per-cycle, per-user pipeline:

1. **Tier-based polling decision.** `_get_user_tier_interval` reads `avg_tweets_per_day` from `ingestor_state.sqlite`: HOT (>20 tw/d, every 3h), WARM (6–20, 8h), COLD (≤5, 24h). `force_first_cycle=True` polls everyone on startup.
2. **Incremental fetch.** Use stored `since_id` to fetch only new tweets via the X API v2 (`X_BEARER_TOKEN`).
3. **Cheap pre-filter.** Several gates run in order, all before any LLM call:
   - **RT/QT detection** (`_detect_retweet_quote`): pure retweets are dropped at fetch time (never reach LLM, never hit label cache, never accrue cost). `referenced_tweets` is included in `tweet.fields` so the X v2 response carries the signal; falls back to `text.startswith("RT @")`. The `is_quote` flag is propagated to downstream steps.
   - **QT commentary gate**: quote tweets whose author commentary — isolated by `_qt_commentary(text)` (strips trailing `t.co` preview link) — is shorter than `MIN_QT_COMMENTARY_CHARS` (env-tunable, default 15) are dropped as bare reposts. `skipped_qt_short` counter incremented.
   - **Whale-alert filter**: `_is_noise_tweet` gains a 3-condition AND check: (a) `WHALE_FLOW_RE` matches a token-amount + flow verb (`transferred|moved|deposited|withdrawn|withdrew|sent`), AND (b) tweet text contains an exchange/custodian name from `EXCHANGE_NAMES` (`binance, coinbase, kraken, okx, bybit, cumberland, wintermute, robinhood, gemini, bitfinex, upbit, bitstamp, htx, kucoin`), AND (c) tweet contains 🚨 or 3+ emoji-ish characters (`_has_three_plus_emoji`). All three must fire. `skipped_whale_alert` counter incremented.
   - Existing `NOISE_PATTERNS` + `_has_explicit_trade_language` heuristics remain.
   - Per-cycle observability: `_filter_counters` is reset at the top of `_run_cycle_inner` and logged at the bottom as a single line: `Filters this cycle — skipped_retweet=N, skipped_qt_short=N, skipped_whale_alert=N`.
4. **Label cache lookup.** Hash tweet text with `_stable_tweet_hash`; if hit in `label_cache.sqlite`, reuse the label.
5. **LLM call** (`LLM_MODEL`, default `gpt-4o-mini`) returns `{is_signal, ticker, sentiment, direction, confidence}`. Few-shot examples bundled inline. Optional vision pass (`VISION_ENABLED`, `VISION_MODEL`) on attached images. The system prompt explicitly instructs the model that factual observations about on-chain flows, exchange transfers, liquidations, listings, or news events are NOT signals unless the author adds clear directional commentary, and that a bare retweet or quote without the author's own opinion is never a signal. Three reinforcing few-shot examples: (i) factual whale-alert → `is_signal: false`, (ii) `RT @whale_alert: ...` → `is_signal: false`, (iii) QT with explicit directional commentary → `is_signal: true`. Two further guardrails (2026-05-02): (a) **liquidation-news rejection** — two paraphrases of prod misses cover both directions (`"$270M of short positions liquidated"` is a bullish event report, not a call; `"$850M long liquidation cascade"` is a bearish event report, not a call); (b) **close-not-open rejection** — `"Full TP on our $ETH short"` and `"Scaled out of my $SOL position"` style announcements are closes, not new entries. A bullish-vibes positive (`"$HYPE to the moon…"`) was added defensively so the new negatives don't bleed into natural directional language. One new system-prompt line: reports of past trades, liquidations, or position closes describe events that already happened — they are not new trade calls.
6. **Confidence gate** — store only when `is_signal=true` AND `confidence ≥ CONFIDENCE_THRESHOLD` (default 60) AND `sentiment != neutral` AND `ticker ∈ HL meta whitelist` (refreshed hourly from `/info type=meta` + `spotMeta`; falls back to `COMMON_CRYPTO_FALLBACK` if HL is unreachable).
7. **Per-user atomic commit.** Each labeled signal is written immediately; one user's failure never loses another's data. Exponential backoff on transients; circuit breaker after `MAX_CONSECUTIVE_FAILURES` (default 10).
8. **Graceful shutdown.** SIGTERM/SIGINT sets a flag; the current user finishes, then exit.

**`--dry-run` CLI flag.** Runs a single cycle (like `--once`), logs every labeling decision per tweet with the verdict and reason, but suppresses all `Signal` row writes and does not advance `last_polled_at` or `since_id` in `ingestor_state.sqlite`. Use before deploying filter-config changes to validate that the new thresholds behave as expected without touching prod data.

**Prompt-change validation.** `scripts/audit_signal_labeler.py` runs the real `llm_batch_label` against a fixture of 13 cases covering both the liquidation-news and close-not-open failure modes, anti-regression positives, and unambiguous noise. Output is a confusion matrix. Use to validate any prompt or few-shot change before re-enabling the ingestor systemd unit.

Tightening `EXPLICIT_TRADE_PHRASES`, `CONFIDENCE_THRESHOLD`, `MIN_QT_COMMENTARY_CHARS`, or the noise filter directly trades signal volume against signal quality — change with intent.

## 7. Wallet management

`backend/services/wallet_manager.py` is the only module that handles keys.

- **Dedicated user wallet.** On first use we generate an EOA, encrypt the private key with Fernet using `WALLET_ENCRYPTION_KEY`, and persist `(address, encrypted_private_key, withdraw_address)` in `user_wallets`. The address is the deposit destination on Arbitrum; the trading engine signs HL orders with the decrypted key in-memory only.
- **Master wallet** (`GAS_STATION_KEY` / `GAS_STATION_ADDRESS`). Two roles: (1) gas station — tops user wallets up with ETH on Arbitrum so they can pay for the HL bridge tx (`ensure_gas`); (2) USDC liquidity pool for low-fee withdrawals — `hl_internal_transfer` moves USDC from user's HL account to master's HL account (free, instant), then `master_transfer_usdc` sends Arbitrum USDC out to the user's external wallet. If master Arbitrum USDC is short, fall back to `withdraw_from_hl` ($1 HL fee).
- **Multi-chain withdraw** via Stargate V2 (`stargate_bridge_out`) — destinations in `CHAIN_ID_TO_LZ_EID` (ETH, OP, Polygon, Base, Avalanche, Mantle, Scroll).
- **Builder fee** — every new wallet must `approve_builder_fee_for_wallet(pk)` before the first trade. `BUILDER_ADDRESS` receives `HL_DEFAULT_BUILDER_BPS` (default 10 bps = 0.10%) on every trade. Trading engine auto-approves on the first failure and caches success in process.
- **Encryption**: `WALLET_ENCRYPTION_KEY` must be a 32-byte urlsafe base64 Fernet key. Rotating it without a re-encrypt step bricks every existing wallet — never overwrite without a migration.

## 8. Deploy & ops

### Commands

```bash
# Postgres (local dev)
docker-compose up -d                       # postgres:16-alpine on :5432

# Migrations
alembic upgrade head
alembic revision --autogenerate -m "msg"

# API (port 8000, autoreload)
python run.py
# equiv: uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# Workers (one process each; run under systemd in prod)
python -m backend.services.trading_engine
python -m backend.ingestor.main           # or: python -m backend.services.ingestor_loop
python -m backend.services.deposit_monitor
python -m backend.services.max_gain_updater          # loop
python -m backend.services.max_gain_updater --once   # cron mode

# One-off scripts
python -m scripts.seed_and_sync           # seed KOL list + import CSV
python -m scripts.compute_stats           # recompute trader_stats outside engine
python -m scripts.backfill_prices         # backfill entry/current price on signals
```

There is no test runner wired up (`tests/` is empty) and no linter config.

### Env vars (names only — values live in `.env`, never commit)

- DB / auth: `DATABASE_URL`, `JWT_SECRET`, `JWT_ALGO`, `JWT_EXPIRE_HOURS`, `CORS_ORIGINS`.
- Hyperliquid: `HL_MAINNET`, `HL_BASE_URL`, `HL_ACCOUNT_ADDRESS`, `HL_API_SECRET_KEY`, `HL_BUILDER_ADDRESS`, `HL_DEFAULT_LEVERAGE`, `HL_DEFAULT_BUILDER_BPS`.
- Wallet system: `WALLET_ENCRYPTION_KEY`, `GAS_STATION_KEY`, `GAS_STATION_ADDRESS`.
- Ingestor: `X_BEARER_TOKEN`, `OPENAI_API_KEY`, `LLM_MODEL`, `VISION_MODEL`, `VISION_ENABLED`, `CONFIDENCE_THRESHOLD`, `CYCLE_INTERVAL_S`, `MAX_CONSECUTIVE_FAILURES`, `SCRAPE_USERS`.
- Paths: `DATA_DIR`, `LOG_DIR`.

### Deploy workflow

- Production runs the API + four workers as separate systemd units (`hypercopy-api`, `hypercopy-engine`, `hypercopy-ingestor`, `hypercopy-monitor`, `hypercopy-maxgain` — exact unit names per the deploy host). `deposit_monitor.py`'s docstring is the canonical reference for the `hypercopy-monitor` unit.
- Project root on the deploy host is `/opt/hypercopy` (referenced in `scripts/seed_and_sync.py`).
- Sentry is initialized at import time in `backend/main.py` with a hardcoded DSN — that's intentional. Gate with env if you need silence in a non-prod context, don't remove the call.
- After model edits: write migration → `alembic upgrade head` on the host before restarting the API or trading-engine units.

### Recovering from migration state drift

Symptom: `alembic upgrade head` crashes with `DuplicateColumn` or `DuplicateTable` because a column was added to prod outside of Alembic (hot-fix, manual `ALTER TABLE`, etc.) and `alembic_version` still points at the preceding revision.

Fix:
```bash
psql $DATABASE_URL -c "SELECT * FROM alembic_version;"   # see where prod thinks it is
alembic stamp <revision_id>                              # mark revision as applied without re-running it
alembic upgrade head                                     # continue from there
```

Prevention: when writing migrations for additive column changes, `op.execute("ALTER TABLE foo ADD COLUMN IF NOT EXISTS bar ...")` is more forgiving of hand-modified prod schemas than `op.add_column(...)`. Soft preference — not required.

### Secrets

`.env` contains live keys (HL API wallet, Fernet `WALLET_ENCRYPTION_KEY`, Arbitrum gas-station private key, OpenAI, X bearer). It is in `.gitignore` and `.claudeignore`. Never print, paste, commit, or move these values; reference variable names only.

## 9. Diagnostics

```bash
# API liveness (DB + HL + master wallet)
curl -s http://localhost:8000/health | jq

# Postgres
psql postgresql://hypercopy:hypercopy_dev_2024@localhost:5432/hypercopy
# Common queries
#   SELECT status, count(*) FROM signals GROUP BY 1;
#   SELECT * FROM trades WHERE status='open' ORDER BY opened_at DESC LIMIT 20;
#   SELECT user_id, balance, snapshot_date FROM balance_snapshots ORDER BY snapshot_date DESC LIMIT 10;
#   SELECT * FROM trader_stats WHERE window='7d' ORDER BY rank LIMIT 25;

# Worker logs (prod, systemd)
journalctl -u hypercopy-engine -f
journalctl -u hypercopy-ingestor -f --since "1 hour ago"
journalctl -u hypercopy-monitor -f
journalctl -u hypercopy-api --since today

# Ingestor SQLite state
sqlite3 data/ingestor_state.sqlite "SELECT username, last_polled_at, since_id, avg_tweets_per_day, empty_polls FROM user_state ORDER BY last_polled_at DESC LIMIT 20;"
sqlite3 data/label_cache.sqlite    "SELECT count(*) FROM label_cache;"

# HL spot-checks
curl -s -X POST https://api.hyperliquid.xyz/info -H 'Content-Type: application/json' -d '{"type":"meta"}' | jq '.universe[].name' | head
curl -s -X POST https://api.hyperliquid.xyz/info -H 'Content-Type: application/json' -d '{"type":"clearinghouseState","user":"0x..."}' | jq

# Local app logs
tail -f logs/*.log

# API won't start: "[Errno 98] Address already in use"
# Orphaned uvicorn from a previous run is still holding port 8000.
sudo lsof -i :8000                                  # find actual listener PID
systemctl status hypercopy-api                      # confirm Main PID differs
sudo pkill -9 -f "uvicorn backend.main:app"
sudo systemctl reset-failed hypercopy-api
sudo systemctl start hypercopy-api
```

## 10. Changelog

- 2026-05-02 — `auth.py` is now resilient to the new `uq_users_twitter_username` partial unique index. Two commit sites (Step 2 attach, Step 3 INSERT race) wrapped in `try/IntegrityError`; on collision, JWT issued for the oldest active canonical user, no data mutation. Helpers: `_is_twitter_username_conflict`, `_resolve_canonical_by_twitter`. Manual SQL prerequisite: index creation + dedup of MomentumKevin/Ameliachenssmy rows (5 → 2).
- 2026-05-02 — Ingestor LLM guardrails: 4 negative + 1 positive few-shot examples for liquidation-news and close-not-open semantic inversions; one-line system-prompt clarifier; `scripts/audit_signal_labeler.py` for prompt-change validation. No threshold change, no regex change.
- 2026-05-02 — Migration `e60adfb8d09b` captures `uq_users_twitter_username` (the partial UNIQUE index on `users(twitter_username) WHERE twitter_username IS NOT NULL`) into alembic. Idempotent (`IF NOT EXISTS`); prod is a no-op since the index was added there manually earlier in the day. Down-migration drops via `IF EXISTS`.
- 2026-05-01 — Ingestor: drop pure retweets at fetch time; gate quote tweets on `MIN_QT_COMMENTARY_CHARS` (env-tunable, default 15); 3-condition AND whale-alert filter (token+flow + exchange name + 🚨/emoji); LLM prompt + 3 few-shots reinforcing factual-observation rule. `--dry-run` CLI flag suppresses DB writes and state advances.
- 2026-05-01 — PROD HOTFIX: `users.wallet_address` widened VARCHAR(42) → VARCHAR(128) (migration `2225cbed80a6`). Dual-account merge marker shortened from `merged-into-<uuid>-<wallet>` (96B) to `deact_<uuid-hex>` (38B). Login was breaking with `StringDataRightTruncation` whenever the merge path fired.
- 2026-05-01 — `/api/portfolio/pnl-history`: switched ROI denominator to cost basis (net deposits − withdrawals); clamped ALL-range to first user activity; added `cost_basis` field to response. Helpers: `_calc_cost_basis`, `_earliest_activity` in `backend/api/portfolio.py`.
- 2026-05-01 — PnL switched to HL-authoritative (engine `_manage_user_positions` + `_close_trade`, manual/partial close in `trades.py`); dropped `trades.realized_pnl_usd`; added `scripts/backfill_pnl_from_hl.py`. Migration `acb0b86b0ff2`. Bug attribution corrected: the formula `pnl_pct/100 × size_usd × leverage` multiplied by leverage one extra time — `size_usd` was always margin, not notional; nothing drifted in the schema.
- 2026-04-26 — Initial spec generated by /init.
- 2026-04-26 — Added §8 "Recovering from migration state drift" (alembic stamp procedure) and §9 port-8000 orphan uvicorn recovery commands; both surfaced during prod deploy of commits `2cd2c52` + `2e7b387`.
