"""
PR1 probe: confirm whether apidojo/tweet-scraper exposes media URLs in its
output, and (if yes) which field carries them.

Usage:
  APIFY_TOKEN=apify_api_... python -m scripts.probe_apify_media
  # or set in .env first

Picks a small set of handles known to post chart screenshots (HsakaTrades,
CryptoCred, AltcoinPsycho), pulls 10 tweets, and prints:

  1. The full set of top-level keys present in the raw item.
  2. Whether any of these media-shaped fields appear: media, extendedEntities,
     photos, attachments, mediaUrls, includes.
  3. If found, a verbatim dump of the first such tweet's media field so we
     can see the structure end-to-end.
  4. The result of normalize() on each item — whether ApifyTweet.images is
     populated.

Cost: ~$0.004 (10 tweets * $0.0004).
"""
from __future__ import annotations

import json, os, sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

if not os.environ.get("APIFY_TOKEN"):
    print("ERROR: APIFY_TOKEN missing. Set it in .env or env.", file=sys.stderr)
    sys.exit(2)

# Import after env is loaded so module-level config picks up the token.
from backend.ingestor.apify_source import (  # noqa: E402
    _apify_post_with_retry,
    normalize,
    APIFY_PER_TWEET_USD,
)


HANDLES = ["HsakaTrades", "CryptoCred", "AltcoinPsycho", "DonAlt", "balajis"]
SUSPECTED_MEDIA_FIELDS = (
    "media", "extendedEntities", "extended_entities", "photos", "attachments",
    "mediaUrls", "media_urls", "includes",
)


def main() -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "twitterHandles": HANDLES,
        "maxItems": 10,
        "onlyTweetsNewer": since,
        "sort": "Latest",
    }
    print(f"POST apidojo/tweet-scraper handles={HANDLES} maxItems=10 onlyTweetsNewer={since}")
    items = _apify_post_with_retry(body)
    print(f"Received {len(items)} items (~${len(items) * APIFY_PER_TWEET_USD:.4f})\n")

    if not items:
        print("No items returned. Try a wider window or different handles.")
        return 0

    # 1. Top-level key frequency
    key_counter: Counter[str] = Counter()
    for it in items:
        if isinstance(it, dict):
            key_counter.update(it.keys())
    print("=== Top-level keys (count out of {}) ===".format(len(items)))
    for k, n in key_counter.most_common():
        print(f"  {n:>3} {k}")

    # 2. Media-shaped fields
    print("\n=== Suspected media-shaped fields present ===")
    found_any = False
    for f in SUSPECTED_MEDIA_FIELDS:
        n = sum(1 for it in items if isinstance(it, dict) and f in it)
        if n:
            found_any = True
            print(f"  {f}: {n}/{len(items)} items")
    if not found_any:
        print("  (none)")

    # 3. Verbatim dump of first item with any suspected field
    print("\n=== First item with media-shaped field (verbatim) ===")
    for it in items:
        if not isinstance(it, dict):
            continue
        for f in SUSPECTED_MEDIA_FIELDS:
            if f in it:
                print(f"id={it.get('id')} url={it.get('url')}")
                print(f"  {f} =")
                print(json.dumps(it[f], indent=2, default=str)[:2000])
                break
        else:
            continue
        break
    else:
        print("  (no item carried any suspected field)")

    # 4. normalize() outcome
    print("\n=== normalize() result per item ===")
    norm_with_images = 0
    norm_total = 0
    for it in items:
        t = normalize(it)
        if t is None:
            continue
        norm_total += 1
        if t.images:
            norm_with_images += 1
        print(f"  id={t.tweet_id} author=@{t.author_username} images={len(t.images)} qt={t.is_quote} rt={t.is_retweet}")
    pct = (100.0 * norm_with_images / norm_total) if norm_total else 0.0
    print(f"\n=== Summary ===")
    print(f"  normalized: {norm_total}/{len(items)}")
    print(f"  with images: {norm_with_images}/{norm_total} ({pct:.1f}%)")
    if norm_with_images == 0 and not found_any:
        print("\n  ⚠ Vision regression: no media URLs detected. Document in PR description.")
        print("    See backend/ingestor/apify_source.py::_extract_images for the shapes attempted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
