"""
Apify (apidojo/tweet-scraper) fetch path for the ingestor.

This module owns:
  • ApifyTweet dataclass — typed shape of one normalized item
  • normalize(raw)       — actor item → ApifyTweet (or None on bad row)
  • fetch_tweets_for_handles(handles, since, max_total) — one batched POST
  • daily-cost guard via the apify_budget table in ingestor_state.sqlite

Authorization is `Authorization: Bearer <APIFY_TOKEN>` — never URL ?token=.
Cost: $0.0004 per tweet, no per-run charge (validated 2026-05-03).
"""
from __future__ import annotations

import logging, os, re, time, random, sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger("ingestor")


def _env(k: str, d: str = "") -> str:
    return os.environ.get(k, d)


# ── config ─────────────────────────────────────────────────────────────
APIFY_TOKEN     = _env("APIFY_TOKEN")
APIFY_ACTOR_ID  = "apidojo~tweet-scraper"
APIFY_ENDPOINT  = (
    f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}"
    "/run-sync-get-dataset-items"
)
APIFY_TIMEOUT_S            = int(_env("APIFY_TIMEOUT_S", "300"))
APIFY_COLDSTART_LOOKBACK_H = int(_env("APIFY_COLDSTART_LOOKBACK_H", "6"))
APIFY_SINCE_BUFFER_S       = 1
APIFY_PER_TWEET_USD        = 0.0004

# Per-tier batch caps. Worst-case cycle cost @ $0.0004/tweet:
#   HOT 5000 → $2.00, WARM 3000 → $1.20, COLD 2000 → $0.80
HOT_BATCH_MAX_TWEETS  = int(_env("APIFY_HOT_BATCH_MAX",  "5000"))
WARM_BATCH_MAX_TWEETS = int(_env("APIFY_WARM_BATCH_MAX", "3000"))
COLD_BATCH_MAX_TWEETS = int(_env("APIFY_COLD_BATCH_MAX", "2000"))

# Daily cost guard. WARN at 80%, pause until next UTC day at 100%.
APIFY_DAILY_BUDGET_TWEETS = int(_env("APIFY_DAILY_BUDGET_TWEETS", "50000"))


# ── data ───────────────────────────────────────────────────────────────
_AUTHOR_FROM_URL_RE = re.compile(r"^https?://(?:x|twitter)\.com/([^/]+)/status/")
_TWITTER_TIME_FMT   = "%a %b %d %H:%M:%S %z %Y"


@dataclass(frozen=True)
class ApifyTweet:
    """Normalized shape — produced by normalize(), consumed via to_pipeline_dict()."""
    tweet_id: str
    text: str
    created_at: datetime         # tz-aware UTC
    images: list[str] = field(default_factory=list)
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    author_username: str = ""    # lowercased at construction
    is_quote: bool = False
    is_retweet: bool = False     # caller drops; not in pipeline dict

    def to_pipeline_dict(self) -> dict:
        """Shape consumed by _label_tweets / _write_user_signals.
        Verified key-by-key against backend/ingestor/main.py:1279-1289."""
        return {
            "tweet_id":        self.tweet_id,
            "text":            self.text,
            "created_at":      self.created_at,
            "images":          list(self.images),
            "likes":           self.likes,
            "retweets":        self.retweets,
            "replies":         self.replies,
            "author_username": self.author_username,
            "is_quote":        self.is_quote,
        }


def _author_from_url(url: str) -> str:
    m = _AUTHOR_FROM_URL_RE.match(url or "")
    return (m.group(1).lstrip("@").lower() if m else "")


def _parse_created_at(s: str) -> datetime:
    try:
        dt = datetime.strptime(s, _TWITTER_TIME_FMT)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _extract_images(raw: dict) -> list[str]:
    """Best-effort media extraction. The actor's public docs don't pin down
    the field name; try the common shapes and return empty if none fire.
    PR1 probe (scripts/probe_apify_media.py) confirms which one is real."""
    out: list[str] = []
    media = raw.get("media")
    if isinstance(media, list):
        for m in media:
            if isinstance(m, dict):
                u = m.get("media_url_https") or m.get("url") or m.get("expanded_url")
                if u:
                    out.append(u)
            elif isinstance(m, str):
                out.append(m)
    ext = raw.get("extendedEntities") or {}
    if isinstance(ext, dict):
        for m in ext.get("media", []) or []:
            if isinstance(m, dict):
                u = m.get("media_url_https") or m.get("url")
                if u:
                    out.append(u)
    photos = raw.get("photos")
    if isinstance(photos, list):
        for p in photos:
            if isinstance(p, dict):
                u = p.get("url") or p.get("media_url_https")
                if u:
                    out.append(u)
            elif isinstance(p, str):
                out.append(p)
    seen = set()
    deduped = []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def normalize(raw: dict) -> Optional[ApifyTweet]:
    """Map one actor item → ApifyTweet. Returns None on missing required
    fields (drop, don't crash the batch)."""
    if not isinstance(raw, dict):
        return None
    tweet_id = str(raw.get("id") or "").strip()
    text     = (raw.get("text") or "").strip()
    url      = raw.get("url") or ""
    if not tweet_id or not text or not url:
        return None
    author = _author_from_url(url)
    if not author:
        return None
    return ApifyTweet(
        tweet_id=tweet_id,
        text=text,
        created_at=_parse_created_at(raw.get("createdAt", "")),
        images=_extract_images(raw),
        likes=int(raw.get("likeCount") or 0),
        retweets=int(raw.get("retweetCount") or 0),
        replies=int(raw.get("replyCount") or 0),
        author_username=author,
        is_quote=bool(raw.get("isQuote") is True),
        is_retweet=bool(raw.get("isRetweet") is True),
    )


# ── HTTP ──────────────────────────────────────────────────────────────
def _apify_headers() -> dict:
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN missing — set in .env (INGESTOR_SOURCE=apify)")
    return {
        "Authorization": f"Bearer {APIFY_TOKEN}",
        "Content-Type":  "application/json",
        "User-Agent":    "HyperCopy/1.0",
    }


def _apify_post_with_retry(body: dict, max_retries: int = 2) -> list[dict]:
    """One retry on 5xx / ConnectionError with exponential backoff + jitter.
    URL never carries the token; body is logged at DEBUG (no secrets)."""
    last_err: object = None
    for attempt in range(max_retries):
        try:
            r = requests.post(
                APIFY_ENDPOINT,
                headers=_apify_headers(),
                json=body,
                timeout=APIFY_TIMEOUT_S,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
                log.warning(f"Apify returned non-list payload: {type(data).__name__}")
                return []
            if r.status_code in (500, 502, 503, 504) and attempt < max_retries - 1:
                wait = 2.0 * (2 ** attempt) + random.uniform(0, 1.0)
                log.warning(f"Apify {r.status_code} — retry in {wait:.1f}s")
                time.sleep(wait)
                last_err = f"Apify {r.status_code}"
                continue
            r.raise_for_status()
        except requests.exceptions.ConnectionError as e:
            last_err = e
            if attempt < max_retries - 1:
                wait = 2.0 * (2 ** attempt) + random.uniform(0, 1.0)
                log.warning(f"Apify connection error — retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise
        except requests.exceptions.RequestException:
            raise
    raise RuntimeError(f"Apify failed after {max_retries} attempts: {last_err}")


@dataclass
class FetchStats:
    raw: int = 0
    kept: int = 0
    dropped_retweet: int = 0
    dropped_unparseable: int = 0


def fetch_tweets_for_handles(
    handles: list[str],
    since: Optional[datetime],
    max_total: int,
) -> tuple[list[ApifyTweet], FetchStats]:
    """One batched POST to the Apify actor. Returns (items, stats) where
    items has pure retweets already dropped; order is whatever the actor
    returns (typically newest-first when sort=Latest). `stats.raw` is the
    cost-bearing count (Apify charges per result, including the RTs we
    drop locally)."""
    if not handles:
        return [], FetchStats()
    body: dict = {
        "twitterHandles": list(handles),
        "maxItems":       int(max_total),
        "sort":           "Latest",
    }
    if since is not None:
        adj = since - timedelta(seconds=APIFY_SINCE_BUFFER_S)
        body["onlyTweetsNewer"] = adj.strftime("%Y-%m-%dT%H:%M:%SZ")

    raw_items = _apify_post_with_retry(body)
    out: list[ApifyTweet] = []
    stats = FetchStats(raw=len(raw_items))
    for raw in raw_items:
        t = normalize(raw)
        if t is None:
            stats.dropped_unparseable += 1
            continue
        if t.is_retweet:
            stats.dropped_retweet += 1
            continue
        out.append(t)
    stats.kept = len(out)
    log.info(
        f"  Apify batch: handles={len(handles)} since={body.get('onlyTweetsNewer','coldstart')} "
        f"raw={stats.raw} kept={stats.kept} dropped_rt={stats.dropped_retweet} "
        f"dropped_unparseable={stats.dropped_unparseable}"
    )
    return out, stats


# ── budget table ──────────────────────────────────────────────────────
def init_budget_table(con: sqlite3.Connection) -> None:
    """Idempotent. Called from _state_db_connect alongside user_state init."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS apify_budget (
            utc_date    TEXT PRIMARY KEY,
            tweets_used INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.commit()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def budget_used_today(con: sqlite3.Connection) -> int:
    row = con.execute(
        "SELECT tweets_used FROM apify_budget WHERE utc_date=?", (_today_utc(),)
    ).fetchone()
    return int(row[0]) if row else 0


def budget_increment(con: sqlite3.Connection, tweets_used: int) -> None:
    if tweets_used <= 0:
        return
    today = _today_utc()
    con.execute(
        """INSERT INTO apify_budget (utc_date, tweets_used) VALUES (?, ?)
           ON CONFLICT(utc_date) DO UPDATE SET tweets_used = tweets_used + excluded.tweets_used""",
        (today, int(tweets_used)),
    )
    con.commit()


def budget_check(con: sqlite3.Connection) -> tuple[bool, int, float]:
    """Returns (allowed, tweets_used_today, pct_used). Logs WARN at >=80%."""
    used = budget_used_today(con)
    pct = (used * 100.0 / APIFY_DAILY_BUDGET_TWEETS) if APIFY_DAILY_BUDGET_TWEETS > 0 else 0.0
    if used >= APIFY_DAILY_BUDGET_TWEETS:
        return False, used, pct
    if pct >= 80.0:
        cost = used * APIFY_PER_TWEET_USD
        log.warning(
            f"💰 Apify daily budget at {pct:.0f}% "
            f"({used}/{APIFY_DAILY_BUDGET_TWEETS} tweets, ~${cost:.2f})"
        )
    return True, used, pct
