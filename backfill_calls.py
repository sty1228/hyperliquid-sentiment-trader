
import os, sqlite3, hashlib, sys
from datetime import datetime
DB = "data/crypto_tracker.db"

def sid(u, t, k):
    base = f"{u}|{t}|{k}".encode("utf-8", "ignore")
    return hashlib.sha1(base).hexdigest()[:24]

with sqlite3.connect(DB) as conn:
    cur = conn.cursor()

    cur.execute("""
    UPDATE tweets
       SET is_call = 1
     WHERE COALESCE(TRIM(ticker),'') <> ''
       AND UPPER(ticker) NOT IN ('NOISE','MARKET');
    """)
    cur.execute("""
    UPDATE tweets
       SET action = CASE
            WHEN LOWER(COALESCE(sentiment,''))='bullish' THEN 'long'
            WHEN LOWER(COALESCE(sentiment,''))='bearish' THEN 'short'
            ELSE 'none' END
     WHERE is_call=1;
    """)

    cur.execute("""UPDATE tweets SET confidence = COALESCE(confidence, 0.7) WHERE is_call=1;""")

    rows = conn.execute("""
      SELECT id, username, tweet_time, ticker
      FROM tweets
      WHERE is_call=1 AND (signal_id IS NULL OR signal_id='')
    """).fetchall()
    for _id, u, tt, k in rows:
        conn.execute("UPDATE tweets SET signal_id=? WHERE id=?", (sid(u or "", tt or "", k or ""), _id))

    conn.commit()
print("backfill done")
