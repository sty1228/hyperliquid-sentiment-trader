"""
Fix missing / broken avatars for all traders.

Steps:
  1. Find traders where avatar_url IS NULL  (never fetched)
  2. Find traders where avatar_url 404s     (stale CDN link)
  3. Batch-fetch from Twitter API v2
  4. Update DB

Usage:
  cd /opt/hypercopy
  source venv/bin/activate
  python scripts/fix_avatars.py
"""

import os
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv("/opt/hypercopy/.env")

sys.path.insert(0, "/opt/hypercopy")
from backend.database import SessionLocal
from backend.models.trader import Trader

BEARER = os.getenv("X_BEARER_TOKEN")
TWITTER_API = "https://api.twitter.com/2/users/by"
CHECK_TIMEOUT = 4      # seconds per HEAD request
TWITTER_BATCH = 100    # max usernames per API call
RATE_SLEEP = 1.5       # seconds between Twitter batches


def check_url_ok(url: str) -> bool:
    """HEAD request to see if CDN link is still alive."""
    try:
        r = requests.head(url, timeout=CHECK_TIMEOUT, allow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False


def fetch_twitter_avatars(usernames: list[str]) -> dict[str, str]:
    """
    Twitter API v2 batch lookup.
    Returns {username_lower: avatar_url}
    """
    if not BEARER:
        print("ERROR: X_BEARER_TOKEN not set in .env")
        return {}

    result = {}
    headers = {"Authorization": f"Bearer {BEARER}"}

    for i in range(0, len(usernames), TWITTER_BATCH):
        batch = usernames[i : i + TWITTER_BATCH]
        params = {
            "usernames": ",".join(batch),
            "user.fields": "profile_image_url",
        }
        try:
            r = requests.get(TWITTER_API, headers=headers, params=params, timeout=10)
            if r.status_code == 429:
                print("  ⚠ Rate limited — sleeping 60s")
                time.sleep(60)
                r = requests.get(TWITTER_API, headers=headers, params=params, timeout=10)

            data = r.json()
            for user in data.get("data", []):
                raw = user.get("profile_image_url", "")
                if raw:
                    # Replace _normal (48px) with _400x400 (400px)
                    big = raw.replace("_normal.", "_400x400.")
                    result[user["username"].lower()] = big

            errors = data.get("errors", [])
            if errors:
                for e in errors:
                    print(f"  Twitter error: {e.get('detail','?')} — {e.get('value','?')}")

        except Exception as exc:
            print(f"  Request failed: {exc}")

        if i + TWITTER_BATCH < len(usernames):
            time.sleep(RATE_SLEEP)

    return result


def main():
    db = SessionLocal()
    try:
        all_traders = db.query(Trader).all()
        print(f"Total traders in DB: {len(all_traders)}")

        # ── Step 1: NULL avatars ──
        null_traders = [t for t in all_traders if not t.avatar_url]
        print(f"  NULL avatar_url : {len(null_traders)}")

        # ── Step 2: check existing URLs for 404 ──
        has_url = [t for t in all_traders if t.avatar_url]
        print(f"  Has avatar_url  : {len(has_url)} — checking for 404s...")

        broken = []
        for idx, t in enumerate(has_url):
            ok = check_url_ok(t.avatar_url)
            status = "✓" if ok else "✗ 404"
            print(f"  [{idx+1}/{len(has_url)}] @{t.username} {status}", end="\r")
            if not ok:
                broken.append(t)
        print()  # newline after \r loop
        print(f"  Broken (404)    : {len(broken)}")

        # ── Combine targets ──
        targets = {t.username.lower(): t for t in null_traders + broken}
        if not targets:
            print("✅ All avatars look good — nothing to fix.")
            return

        print(f"\nFetching {len(targets)} avatars from Twitter API...")
        avatar_map = fetch_twitter_avatars(list(targets.keys()))

        # ── Step 3: Update DB ──
        updated = 0
        not_found = []
        for uname_lower, trader in targets.items():
            new_url = avatar_map.get(uname_lower)
            if new_url:
                trader.avatar_url = new_url
                updated += 1
                print(f"  ✓ @{trader.username} → {new_url[:60]}...")
            else:
                not_found.append(trader.username)

        db.commit()
        print(f"\n✅ Updated {updated} avatars.")

        if not_found:
            print(f"⚠ Still missing ({len(not_found)}) — account suspended/deleted or API error:")
            for u in not_found:
                print(f"   @{u}")

    finally:
        db.close()


if __name__ == "__main__":
    main()