# Frontend Handoff — 2026-04-28 backend additions

This document is the contract for the four product features just landed in the backend. The frontend agent should treat these endpoints/payloads as authoritative and build UI against them.

Base URL: same FastAPI app on port 8000. All endpoints below require the existing `Authorization: Bearer <jwt>` header unless noted otherwise. SSE is the one exception — see §4.

---

## 1. Copy Next mode (one-shot copy)

When a user clicks **Copy** on a KOL, the modal that gathers risk-management settings should add a radio/checkbox group:

- **Copy All trades** (default) — current behavior.
- **Copy Next trade only** — copies exactly one upcoming signal, then auto-disables.

Both work for **Counter** as well — same `copy_mode` field controls counter behavior.

### `POST /api/follow`

Body — extended:
```json
{
  "trader_username": "alice",
  "is_copy_trading": true,
  "is_counter_trading": false,
  "copy_mode": "next"        // NEW: "all" | "next"  (default "all")
}
```
Validation: `copy_mode="next"` is rejected with 422 unless `is_copy_trading` or `is_counter_trading` is `true`.

Response — extended:
```json
{
  "id": "...",
  "trader_username": "alice",
  "is_copy_trading": true,
  "is_counter_trading": false,
  "copy_mode": "next",
  "remaining_copies": 1,        // NEW: 1 when copy_mode="next", null otherwise
  "created_at": "..."
}
```

### `PATCH /api/follow/{trader_username}/copy-mode` (NEW)

Body: `{ "copy_mode": "all" | "next" }`
Response: `{ copy_mode, remaining_copies, is_copy_trading, is_counter_trading }`

Use this when the user toggles between modes on an already-followed KOL without changing copy/counter on/off.

### Other follow endpoints

`GET /api/follows`, `GET /api/follow/check/{trader_username}`, `GET /api/follow/{trader_username}` all now include `copy_mode` and `remaining_copies` (`int | null`).

The **existing** `PATCH /api/follow/{x}/copy-trading` and `PATCH /api/follow/{x}/counter-trading` toggle endpoints reset `copy_mode='all'` and clear `remaining_copies` whenever they re-enable copying. So toggling copy off-and-on starts fresh — that's intentional.

### Lifecycle the frontend should expect

- User picks "Copy Next" → `remaining_copies = 1`, `copy_mode = "next"`, `is_copy_trading = true`.
- The trading engine fires one fill on the next eligible signal.
- After that fill: `is_copy_trading = false`, `copy_mode = "all"`, `remaining_copies = null`.
- Skipped signals (same-ticker conflict, insufficient margin, max positions) do **not** consume the slot — only successful fills do.

UI suggestion: in the follow row badge, show "Next: 1 left" when `copy_mode === "next"`.

---

## 2. Max gain after tweet — surfaced on explore

Already exposed on `/api/traders/{x}` and `/api/traders/{x}/signals` (no change). **New** on:

### `GET /api/explore/token/{ticker}`

Each row in `recent_signals` (a `TokenSignalRow`) now includes:
```json
{
  "max_gain_pct": 18.42,                 // peak favorable excursion since tweet, in %
  "max_gain_at": "2026-04-26T14:31:02Z"  // ISO timestamp of the peak
}
```

`null` when not yet computed (rare — the `max_gain_updater` worker runs every 5 min). Render as e.g. "Peak +18.4% · 6h after tweet".

---

## 3. Manual trading

Three new endpoints under the existing `trades` router. All accept the standard JWT.

### `POST /api/trades/manual` — open a position

Body:
```json
{
  "ticker": "BTC",
  "direction": "long",                // "long" | "short"
  "size_usd": 50,                     // > 0; backend caps to 90% of equity/withdrawable
  "leverage": 5.0,                    // 1.0 .. 50.0 (default 5)
  "order_type": "market",             // "market" | "limit"
  "limit_price": null,                // required when order_type="limit"
  "tp_pct": 20,                       // optional per-trade TP override (%)
  "sl_pct": 8                         // optional per-trade SL override (%)
}
```

Response: `TradeResponse` (same shape as elements of `GET /api/trades`).

Errors:
- 400 — `withdrawable < $10`, allocation < $10 after cap, qty rounds to zero, ticker not on HL.
- 409 — user already has an open position on the same ticker (engine and manual paths share the same-ticker guard).
- 502 — HL submission/fill failure. If a fill happens but DB persist fails, the backend auto-closes the ghost position and returns 500.

### `PATCH /api/trades/{trade_id}/tp-sl` — modify TP/SL

Body:
```json
{ "tp_pct": 25, "sl_pct": null }    // nulls clear the override
```
Response:
```json
{ "trade_id": "...", "tp_override_pct": 25.0, "sl_override_pct": null }
```

This is a pure DB update. TP/SL is enforced engine-side: the trading engine's `update_positions` loop reads `trade.tp_override_pct or settings.tp_value` (and same for SL) every 15 s. So changes take effect on the next tick — there's no HL trigger order to cancel/replace.

### `POST /api/trades/{trade_id}/partial-close?pct=N`

Query: `pct` ∈ (0, 100]. Closes that percent of the position.

- `pct >= ~100` (or rounding pushes us to full close) routes to the existing full-close path and returns the closed `TradeResponse` with `status="closed"`.
- Otherwise the row stays `status="open"` with reduced `size_qty` / `size_usd`, and `realized_pnl_usd` accumulates the realized PnL of the slice.

Response: `TradeResponse` for the (possibly still-open) trade.

UI suggestion: percent slider (25/50/75/100). The engine still owns lifecycle for partial trades, including TP/SL on the remainder.

### Existing `POST /api/trades/{trade_id}/close` — unchanged

Still works for full manual close. It now also publishes a `trade_closed` SSE event with `reason: "manual"`.

---

## 4. Trader network visualization

This is the biggest piece. Three pillars:

1. Initial graph snapshot (one HTTP call).
2. Click-through detail (one HTTP call per click).
3. Realtime pulses via SSE.

All animation, layout, and interactivity is frontend territory — the backend supplies state and events.

### 4a. `GET /api/network/graph` — initial draw

Returns one row per **(KOL, source) edge**. If a user both copies and counters the same KOL, two rows appear (one per source).

```json
[
  {
    "trader_username": "alice",
    "avatar_url": "https://...",
    "display_name": "Alice",
    "source": "copy",                 // "copy" | "counter"  →  edge color: green | red  (spec rule F)
    "is_copy_trading": true,
    "is_counter_trading": false,
    "copy_mode": "all",               // "all" | "next"  (badge if "next")
    "remaining_copies": null,
    "open_count": 3,                  // bars next to KOL  (spec rule D)
    "total_exposure_usd": 740.0,      // edge label (parallel to middle of line)  (spec rule C)
    "win_count": 12,
    "loss_count": 5,
    "win_rate": 0.706,                // backend-computed, 0..1
    "pnl_usd": 312.4,
    "trade_count": 17
  },
  ...
]
```

Sort order: highest `total_exposure_usd` first, then `open_count`, then alpha. Re-sort client-side if you prefer.

KOLs the user follows but hasn't traded yet still appear (with zeros) so the graph isn't empty on day one.

### 4b. `GET /api/network/trader/{trader_username}/detail` — click-through

Returns:
```json
{
  "aggregates": [ <NetworkEdge>, ... ],   // 1 or 2 entries (copy/counter)
  "open_trades": [
    {
      "id": "...",
      "ticker": "BTC",
      "direction": "long",
      "size_usd": 250.0,
      "size_qty": 0.0042,
      "leverage": 5.0,
      "entry_price": 87432.0,
      "current_pnl_usd": 12.40,
      "current_pnl_pct": 5.2,
      "opened_at": "..."
    }
  ]
}
```

Use `aggregates` for the panel header (totals + win rate) and `open_trades` for the list of in-flight positions. (Spec rule E.)

Returns 404 if the KOL doesn't exist; an empty `open_trades` array is normal when the user follows but hasn't traded.

### 4c. SSE realtime stream — pulses

Two-step handshake. EventSource can't send `Authorization` headers, so we mint a short-lived token first.

**Step 1**: `POST /api/auth/stream-token` (with the regular Bearer JWT).
Response:
```json
{ "token": "<60s-jwt>", "expires_in": 60 }
```
Refresh by calling again — there's no rate limit on this beyond the global 60/min.

**Step 2**: open EventSource:
```js
const es = new EventSource(`/api/events/stream?token=${token}` +
                            (lastId ? `&last_id=${lastId}` : ''));
```

Event payload (single envelope, JSON in `data:`):
```json
{
  "v": 1,
  "id": 91827,                       // monotonic; persist as lastId for reconnect backfill
  "type": "trade_opened",            // see table below
  "ts": "2026-04-26T14:31:02Z",
  "user_id": "<self>",
  "trader_username": "alice",        // null for source="manual" trades
  "ticker": "BTC",
  "direction": "long",               // "long" | "short"
  "source": "copy",                  // "copy" | "counter" | "manual"  →  drives pulse color
  "size_usd": 250.0,
  "pnl_usd": null,                   // set on close events; null on opens
  "reason": null                     // see below
}
```

Event types (use these to drive different animations):

| `type`            | When                                  | `reason` set?                                    |
| ----------------- | ------------------------------------- | ------------------------------------------------ |
| `trade_opened`    | Copy/counter/manual position opens    | null                                             |
| `trade_closed`    | Position closes (no special trigger)  | `"external"` \| `"manual"`                       |
| `tp_hit`          | Engine fired TP                       | `"tp"`                                           |
| `sl_hit`          | Engine fired SL                       | `"sl"`                                           |
| `equity_protect`  | Engine equity-protection swept positions | `"equity_protect"`                            |

UI mapping (spec rules A & B): on each event for a given `trader_username`, animate a pulse along the corresponding edge from KOL → user node. Color = green if `source="copy"`, red if `source="counter"` (matches edge color). For `source="manual"` (no KOL), pulse from the user node to itself or skip — your call.

After receiving an event, refetching `/api/network/graph` is the simplest way to update edge labels (`total_exposure_usd`, `open_count`, `win_rate`, etc.). Or apply deltas client-side for fewer roundtrips.

**Reconnect**: persist `id` of the last event you successfully processed; on reconnect pass `?last_id=<n>` to get a backfill of any events you missed (up to 500). The `: connected` SSE comment on attach is a useful way to tell the user "we're live".

**Heartbeat**: server emits `: ping` every 20 s as a comment line; EventSource ignores comments, but proxies see traffic.

### 4d. Alerts as a fallback

Same events also write `Alert` rows. The existing `GET /api/alerts?category=trades` and `/api/alerts/unread-count` will start returning data — useful for a notifications icon or as a backup when the SSE stream is down.

Alert types written: `trade_opened`, `trade_closed`, `take_profit`, `stop_loss`. Title/message are pre-formatted; `data_json` carries the full event envelope as a string if you want richer rendering.

---

## Required env / setup the frontend agent should know

- `CORS_ORIGINS` already includes `http://localhost:3000` and `:5173` by default. SSE uses standard CORS so a credentialed origin must be allowed.
- All new endpoints obey the standard 60 req/min rate limit. SSE itself doesn't count against this once connected.
- The trading engine and the API are separate processes. The SSE bridge uses Postgres LISTEN/NOTIFY, so events from engine-process closures (TP/SL/equity_protect) reach the API process automatically.

---

## Quick smoke-test checklist for the frontend agent

After wiring each piece, confirm:

1. **Copy Next** — follow a KOL with `copy_mode: "next"`, watch the badge show "Next: 1 left", trigger or wait for a signal, badge disappears and copy-trading flag flips off.
2. **Max gain** — open `/api/explore/token/BTC`, inspect any signal with non-null `max_gain_pct` and render it.
3. **Manual trade** — `POST /api/trades/manual` with `size_usd: 12, leverage: 5, ticker: "BTC", direction: "long"`. Verify the trade appears in `GET /api/trades` with `source: "manual"`. Modify TP/SL via the patch. Partial-close 50%.
4. **Network graph** — render the user node + KOL nodes from `GET /api/network/graph`. Edge color from `source`. Edge label from `total_exposure_usd`. Bars from `open_count`.
5. **SSE** — open the stream, manually trigger an event (e.g. POST a manual trade), confirm a `trade_opened` event arrives and the pulse animates. Disconnect the network briefly, reconnect with `last_id`, confirm backfill arrives.
