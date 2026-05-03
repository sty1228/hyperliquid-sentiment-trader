"""
PR1 probe (per design-doc note A): coarse-bound the raw image-bearing rate
of tweets returned by apidojo/tweet-scraper, separate from
"tweets vision successfully labeled" (the 68.2% lifetime DB number).

Pulls ~100 raw items from a 24h window across alpha-leaning handles, then
applies the heuristic:
  raw image-bearing iff text contains 'pic.twitter.com/' OR
  (contains 'https://t.co/' AND likeCount > 50)

The t.co clause is a coarse upper bound — t.co URLs are mostly link previews,
but for a popular tweet (likes>50) the marginal probability that the t.co
target is an image is high enough that this overcounts only mildly.

Output:
  raw_total = N
  image_bearing_upper_bound = M  (P% of N)
  image_bearing_pic_only = K     (Q% of N)  — strict lower bound

Compare P% vs the 68.2% lifetime number documented in the PR1 description.

Cost: ~$0.04 (100 tweets * $0.0004).
"""
from __future__ import annotations

import os, sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

if not os.environ.get("APIFY_TOKEN"):
    print("ERROR: APIFY_TOKEN missing.", file=sys.stderr)
    sys.exit(2)

from backend.ingestor.apify_source import _apify_post_with_retry, APIFY_PER_TWEET_USD  # noqa: E402
from backend.ingestor.seed_handles import APIFY_SEED_HANDLES  # noqa: E402


def main() -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "twitterHandles": APIFY_SEED_HANDLES[:30],
        "maxItems": 100,
        "onlyTweetsNewer": since,
        "sort": "Latest",
    }
    print(f"POST handles={len(body['twitterHandles'])} maxItems=100 onlyTweetsNewer={since}")
    items = _apify_post_with_retry(body)
    cost = len(items) * APIFY_PER_TWEET_USD
    print(f"Received {len(items)} items (~${cost:.4f})")

    pic_only = 0
    upper_bound = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        text = it.get("text") or ""
        likes = int(it.get("likeCount") or 0)
        has_pic_twitter = "pic.twitter.com/" in text
        has_tco = "https://t.co/" in text
        if has_pic_twitter:
            pic_only += 1
            upper_bound += 1
        elif has_tco and likes > 50:
            upper_bound += 1

    n = len(items)
    pct_lower = (100.0 * pic_only / n) if n else 0.0
    pct_upper = (100.0 * upper_bound / n) if n else 0.0
    print(f"\nimage_bearing_pic_only         = {pic_only}/{n} ({pct_lower:.1f}%) — strict lower bound")
    print(f"image_bearing_upper_bound      = {upper_bound}/{n} ({pct_upper:.1f}%) — coarse upper bound")
    print(f"\nDB lifetime baseline (signals.tweet_image_url IS NOT NULL) = 68.2%")
    print(f"  — that figure measures vision-labeled signals, not raw tweets.")
    print(f"  Compare upper_bound vs 68.2% to size the regression risk if Apify exposes no media.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
