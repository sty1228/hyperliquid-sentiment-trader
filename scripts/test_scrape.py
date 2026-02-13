"""
Quick smoke test — fetches a few tweets from 1-2 users via X API v2.
Run:  python3 scripts/test_scrape.py
"""
import os, sys, time, requests
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
if not X_BEARER_TOKEN:
    print("❌ X_BEARER_TOKEN not set in .env")
    sys.exit(1)

API = "https://api.twitter.com/2"
HEADERS = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}

def resolve_user(username):
    r = requests.get(f"{API}/users/by/username/{username}", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"  ❌ HTTP {r.status_code}: {r.text[:200]}")
        return None
    return r.json().get("data", {}).get("id")

def fetch_tweets(user_id, username, max_days=3):
    start = (datetime.now(timezone.utc) - timedelta(days=max_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "max_results": 10,
        "start_time": start,
        "tweet.fields": "created_at,attachments",
        "expansions": "attachments.media_keys",
        "media.fields": "url,preview_image_url,type",
        "exclude": "retweets,replies",
    }
    r = requests.get(f"{API}/users/{user_id}/tweets", headers=HEADERS, params=params, timeout=15)
    if r.status_code != 200:
        print(f"  ❌ HTTP {r.status_code}: {r.text[:200]}")
        return []
    data = r.json()
    media_map = {}
    for m in data.get("includes", {}).get("media", []):
        k = m.get("media_key", "")
        url = m.get("url") or m.get("preview_image_url") or ""
        if k and url:
            media_map[k] = url
    results = []
    for tw in data.get("data", []):
        text = tw.get("text", "")
        created = tw.get("created_at", "")
        imgs = [media_map[mk] for mk in tw.get("attachments", {}).get("media_keys", []) if mk in media_map]
        results.append({"text": text, "created_at": created, "images": imgs})
    return results

TEST_USERS = ["MizerXBT", "MomentumKevin"]

for username in TEST_USERS:
    print(f"\n{'='*50}")
    print(f"Testing @{username}")
    print(f"{'='*50}")
    uid = resolve_user(username)
    if not uid:
        print(f"  ❌ Could not resolve @{username}")
        continue
    print(f"  ✅ User ID: {uid}")
    tweets = fetch_tweets(uid, username, max_days=3)
    print(f"  📊 {len(tweets)} tweets in last 3 days\n")
    for i, tw in enumerate(tweets):
        t = tw["text"]
        print(f"  [{i+1}] {tw['created_at']}")
        print(f"      {t[:120]}{'…' if len(t)>120 else ''}")
        if tw["images"]:
            print(f"      📷 {len(tw['images'])} image(s)")
    if not tweets:
        print("  ⚠️  No tweets found — they might not have posted recently")
    time.sleep(0.5)

print("\n✅ Test complete")
