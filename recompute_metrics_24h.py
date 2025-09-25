
import os
import csv
import time
import sqlite3
import datetime as _dt
from collections import defaultdict
from typing import Dict, Iterable, Optional, List, Tuple

WINDOW = "24h"

# scoring
def _composite_score(return_pct: float, win_rate: float, call_noise: float) -> float:
    return round(0.6 * return_pct + 0.3 * win_rate + 0.1 * call_noise, 2)

def _profit_grade(score: float) -> str:
    if score >= 85: return "S+"
    if score >= 70: return "S"
    if score >= 55: return "A"
    if score >= 40: return "B"
    if score >= 25: return "C"
    return "D"

def _table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    r = cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;", (name,)).fetchone()
    return bool(r)

def _col_exists(cur: sqlite3.Cursor, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table});")
    return any(row[1] == col for row in cur.fetchall())

def _add_column_if_missing(cur: sqlite3.Cursor, table: str, col: str, decl: str):
    cur.execute(f"PRAGMA table_info({table});")
    names = {row[1] for row in cur.fetchall()}
    if col not in names:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl};")

def _ensure_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_window_stats (
      user_id TEXT NOT NULL,
      handle TEXT NOT NULL,
      window TEXT NOT NULL,
      period_start_ts INTEGER NOT NULL,
      period_end_ts INTEGER NOT NULL,

      return_pct_w REAL NOT NULL,
      win_rate REAL NOT NULL,
      total_calls INTEGER NOT NULL,
      total_tweets INTEGER NOT NULL,
      call_to_noise_ratio REAL NOT NULL,
      avg_holding_hours REAL NOT NULL,

      week_return_pct REAL,
      month_return_pct REAL,
      weekly_calls INTEGER,
      monthly_calls INTEGER,

      composite_score REAL NOT NULL,
      profit_grade TEXT NOT NULL,
      points REAL NOT NULL,
      rank INTEGER,
      rank_change_24h INTEGER,

      win_streak INTEGER NOT NULL,
      lose_streak INTEGER NOT NULL,

      most_recent_call_age_sec INTEGER NOT NULL,
      activity_score REAL,
      follow_count INTEGER,

      snapshot_ts INTEGER NOT NULL,

      PRIMARY KEY (user_id, window)
    );""")
    _add_column_if_missing(cur, "user_window_stats", "most_recent_call_utc", "TEXT")

    cur.execute("DROP VIEW IF EXISTS user_summary_24h;")
    cur.execute("""
    CREATE VIEW user_summary_24h AS
    SELECT
      user_id,
      return_pct_w       AS return_pct,
      win_rate,
      total_calls,
      total_tweets,
      call_to_noise_ratio,
      avg_holding_hours,
      week_return_pct,
      month_return_pct,
      weekly_calls,
      monthly_calls,
      composite_score,
      profit_grade,
      points,
      rank              AS rank_global,
      rank_change_24h,
      win_streak,
      lose_streak,
      most_recent_call_utc,
      activity_score,
      follow_count,
      '24h'             AS window,
      snapshot_ts
    FROM user_window_stats
    WHERE window = '24h';
    """)
    conn.commit()

def _parse_time_to_epoch(s: Optional[str]) -> Optional[int]:
    if not s: return None
    s = str(s).strip()
    try:
        if s.endswith("Z"):
            dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = _dt.datetime.fromisoformat(s)
        return int(dt.timestamp())
    except Exception:
        pass
    fmts = ["%Y-%m-%d %H:%M:%S%z","%Y-%m-%d %H:%M:%S","%Y/%m/%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S","%Y-%m-%dT%H:%M:%S.%fZ"]
    for fmt in fmts:
        try:
            dt = _dt.datetime.strptime(s, fmt)
            return int(dt.timestamp())
        except Exception:
            continue
    return None

def _epoch_to_utc_iso(ts: Optional[int]) -> Optional[str]:
    if ts is None: return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))  

def _detect_ph_cols(cur: sqlite3.Cursor) -> Tuple[str, str, str]:
    cur.execute("PRAGMA table_info(price_history);")
    cols = {row[1].lower() for row in cur.fetchall()}
    for name in ("symbol","ticker","pair","asset","instrument"):
        if name in cols: ph_sym = name; break
    else:
        raise RuntimeError("price_history missing symbol-like column")
    for name in ("timestamp","ts","time","t"):
        if name in cols: ph_ts = name; break
    else:
        raise RuntimeError("price_history missing timestamp-like column")
    for name in ("price","c","close","last","close_price"):
        if name in cols: ph_px = name; break
    else:
        raise RuntimeError("price_history missing price-like column")
    return ph_sym, ph_ts, ph_px

def _fetch_prices_for_tickers(cur: sqlite3.Cursor, tickers: Iterable[str]) -> List[sqlite3.Row]:
    tks = [t for t in set(tickers) if t]
    if not tks: return []
    ph_sym, _, _ = _detect_ph_cols(cur)
    q = ",".join("?" for _ in tks)
    return cur.execute(f"SELECT * FROM price_history WHERE {ph_sym} IN ({q})", tks).fetchall()

def _latest_prices(cur: sqlite3.Cursor, tickers: Iterable[str]) -> Dict[str, float]:
    rows = _fetch_prices_for_tickers(cur, tickers)
    if not rows: return {}
    ph_sym, ph_ts, ph_px = _detect_ph_cols(cur)
    latest: Dict[str, Tuple[int,float]] = {}
    for r in rows:
        sym = r[ph_sym]
        ts_epoch = _parse_time_to_epoch(r[ph_ts])
        if ts_epoch is None: continue
        px = float(r[ph_px]) if r[ph_px] is not None else None
        if px is None: continue
        prev = latest.get(sym)
        if (prev is None) or (ts_epoch > prev[0]):
            latest[sym] = (ts_epoch, px)
    return {k: v[1] for k, v in latest.items()}

def _entry_price_from_prices(cur: sqlite3.Cursor, ticker: str, entry_ts: int) -> Optional[float]:
    rows = _fetch_prices_for_tickers(cur, [ticker])
    if not rows: return None
    _, ph_ts, ph_px = _detect_ph_cols(cur)
    best_ts = None; best_px = None
    for r in rows:
        ts_epoch = _parse_time_to_epoch(r[ph_ts])
        if ts_epoch is None or ts_epoch < entry_ts: continue
        px = float(r[ph_px]) if r[ph_px] is not None else None
        if px is None: continue
        if (best_ts is None) or (ts_epoch < best_ts):
            best_ts = ts_epoch; best_px = px
    return best_px

# recompute
def recompute_user_summary_24h(conn: sqlite3.Connection):
    _ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    now_ts = int(time.time())
    start_ts = now_ts - 24 * 3600

    if _table_exists(cur, "signals"):
        base = "signals"
    elif _table_exists(cur, "tweets"):
        base = "tweets"
    else:
        cur.execute("DELETE FROM user_window_stats WHERE window='24h';")
        conn.commit()
        return

    has_created_at = _col_exists(cur, base, "created_at_ts")
    has_entry_ts   = _col_exists(cur, base, "entry_ts")
    has_entry_px   = _col_exists(cur, base, "entry_price")
    has_valid      = _col_exists(cur, base, "is_valid_call")
    has_conf       = _col_exists(cur, base, "confidence")
    has_tweet_time = _col_exists(cur, base, "tweet_time")

    def _get_ticker_from_rowkeys(keys) -> Optional[str]:
        for k in ("ticker","symbol","pair","asset","instrument","primary_ticker"):
            if k in keys: return k
        return None

    if has_created_at:
        base_rows = cur.execute(f"SELECT * FROM {base} WHERE created_at_ts BETWEEN ? AND ?", (start_ts, now_ts)).fetchall()
    elif has_entry_ts:
        base_rows = cur.execute(f"SELECT * FROM {base} WHERE entry_ts BETWEEN ? AND ?", (start_ts, now_ts)).fetchall()
    else:
        base_rows = cur.execute(f"SELECT * FROM {base}").fetchall()


    def _get(r, k, default=None): return r[k] if k in r.keys() else default
    def user_id(r):   return _get(r, "user_id") or _get(r, "username")
    def ticker_field_name(r) -> Optional[str]: return _get_ticker_from_rowkeys(r.keys())
    def ticker_val(r) -> Optional[str]:
        fn = ticker_field_name(r); return r[fn] if fn and fn in r.keys() else None
    def direction_val(r) -> Optional[str]:
        if "direction" in r.keys(): return r["direction"]
        s = (_get(r, "sentiment", "") or "").strip().lower()
        if s in ("bull","bullish","+","long","buy"):  return "BULL"
        if s in ("bear","bearish","-","short","sell"):return "BEAR"
        return None
    def entry_ts_val(r) -> Optional[int]:
        if has_entry_ts and _get(r, "entry_ts") is not None: return int(_get(r, "entry_ts"))
        if has_created_at and _get(r, "created_at_ts") is not None: return int(_get(r, "created_at_ts"))
        if has_tweet_time: return _parse_time_to_epoch(_get(r, "tweet_time"))
        return None
    def entry_px_val(r) -> Optional[float]:
        return float(r["entry_price"]) if has_entry_px and r["entry_price"] is not None else None
    def valid_call(r) -> bool:
        if has_valid: return bool(r["is_valid_call"])
        t = ticker_val(r); d = direction_val(r)
        conf_ok = True
        if has_conf and r["confidence"] is not None:
            try: conf_ok = float(r["confidence"]) >= 0.6
            except Exception: conf_ok = True
        return bool(t) and bool(d) and conf_ok

    needed_tickers = set()
    for r in base_rows:
        ets_tmp = entry_ts_val(r)
        if ets_tmp is None or not (start_ts <= ets_tmp <= now_ts): continue
        if valid_call(r):
            tk = ticker_val(r)
            if tk: needed_tickers.add(tk)
    last_price_map = _latest_prices(cur, needed_tickers)

    users = defaultdict(lambda: {
        "total_tweets": 0,
        "total_calls": 0,
        "valid_returns": [],     
        "holding_hours": [],
        "most_recent_call_ts": None,
        "win_loss_seq": []      
    })

    now_now = int(time.time())
    for r in base_rows:
        ets = entry_ts_val(r)
        if ets is None or not (start_ts <= ets <= now_ts): continue
        uid = user_id(r)
        if not uid: continue
        users[uid]["total_tweets"] += 1
        if not valid_call(r): continue
        tk = ticker_val(r); dr = direction_val(r); ep = entry_px_val(r)
        if not tk or not dr: continue
        if ep is None: ep = _entry_price_from_prices(cur, tk, ets)
        lp = last_price_map.get(tk)
        if ep is None or lp is None: continue
        sign = 1 if dr == "BULL" else -1
        ret = sign * (lp - ep) / ep * 100.0
        users[uid]["total_calls"] += 1
        users[uid]["valid_returns"].append((ets, ret))
        users[uid]["holding_hours"].append(max(0, (now_now - ets)) / 3600.0)
        users[uid]["win_loss_seq"].append((ets, 1 if ret > 0 else 0))
        if (users[uid]["most_recent_call_ts"] is None) or (ets > users[uid]["most_recent_call_ts"]):
            users[uid]["most_recent_call_ts"] = ets

    results = []
    for uid, d in users.items():
        tcalls   = d["total_calls"]
        ttweets  = d["total_tweets"]
        rtn      = sum(x for _, x in d["valid_returns"]) / tcalls if tcalls else 0.0
        win      = (sum(w for _, w in d["win_loss_seq"]) / tcalls * 100.0) if tcalls else 0.0
        c2n      = (tcalls / ttweets * 100.0) if ttweets else 0.0
        hold     = (sum(d["holding_hours"]) / len(d["holding_hours"])) if d["holding_hours"] else 0.0
        most_ts  = d["most_recent_call_ts"]
        most_utc = _epoch_to_utc_iso(most_ts)

        seq = sorted(d["win_loss_seq"], key=lambda x: x[0], reverse=True)
        win_streak = 0
        lose_streak = 0
        for _, w in seq:
            if w == 1:
                if lose_streak == 0: win_streak += 1
                else: break
            else:
                if win_streak == 0: lose_streak += 1
                else: break

        cs = _composite_score(rtn, win, c2n)
        pg = _profit_grade(cs)

        results.append({
            "user_id": uid,
            "return_pct_w": round(rtn, 4),
            "win_rate": round(win, 2),
            "total_calls": tcalls,
            "total_tweets": ttweets,
            "call_to_noise_ratio": round(c2n, 2),
            "avg_holding_hours": round(hold, 2),
            "most_recent_call_utc": most_utc or "",
            "composite_score": cs,
            "profit_grade": pg,
            "points": 0.0,              # <-- points now 0
            "win_streak": win_streak,
            "lose_streak": lose_streak,
        })

    # rank vs previous
    prev = {
        row["user_id"]: row["rank"]
        for row in cur.execute("SELECT user_id, rank FROM user_window_stats WHERE window='24h';").fetchall()
    }

    results.sort(key=lambda x: (-x["composite_score"], -x["return_pct_w"], -x["win_rate"]))
    for i, r in enumerate(results, start=1):
        old = prev.get(r["user_id"])
        r["rank"] = i
        r["rank_change_24h"] = (old - i) if old else 0

    for r in results:
        cur.execute("""
        INSERT INTO user_window_stats (
          user_id, handle, window, period_start_ts, period_end_ts,
          return_pct_w, win_rate, total_calls, total_tweets, call_to_noise_ratio, avg_holding_hours,
          week_return_pct, month_return_pct, weekly_calls, monthly_calls,
          composite_score, profit_grade, points, rank, rank_change_24h,
          win_streak, lose_streak, most_recent_call_age_sec, activity_score, follow_count, snapshot_ts,
          most_recent_call_utc
        ) VALUES (?,?,?,?,?,
                  ?,?,?,?,?,?,
                  NULL,NULL,NULL,NULL,
                  ?,?,?,?,?,
                  ?,?, 0, NULL, NULL, ?,  -- age = 0
                  ?
        )
        ON CONFLICT(user_id, window) DO UPDATE SET
          handle='',
          period_start_ts=excluded.period_start_ts,
          period_end_ts=excluded.period_end_ts,
          return_pct_w=excluded.return_pct_w,
          win_rate=excluded.win_rate,
          total_calls=excluded.total_calls,
          total_tweets=excluded.total_tweets,
          call_to_noise_ratio=excluded.call_to_noise_ratio,
          avg_holding_hours=excluded.avg_holding_hours,
          composite_score=excluded.composite_score,
          profit_grade=excluded.profit_grade,
          points=excluded.points,
          rank=excluded.rank,
          rank_change_24h=excluded.rank_change_24h,
          win_streak=excluded.win_streak,
          lose_streak=excluded.lose_streak,
          most_recent_call_age_sec=0,
          most_recent_call_utc=excluded.most_recent_call_utc,
          snapshot_ts=excluded.snapshot_ts
        ;
        """, (
            r["user_id"], "", WINDOW,  # handle blank
            int(time.time()) - 24*3600, int(time.time()),
            r["return_pct_w"], r["win_rate"], r["total_calls"], r["total_tweets"],
            r["call_to_noise_ratio"], r["avg_holding_hours"],
            r["composite_score"], r["profit_grade"], r["points"], r["rank"], r["rank_change_24h"],
            r["win_streak"], r["lose_streak"], int(time.time()),
            r["most_recent_call_utc"],
        ))

    conn.commit()

def export_user_summary_csv(conn: sqlite3.Connection,
                            out_dir: str = "data",
                            filename: str = "user_summary_24h.csv",
                            also_timestamped: bool = True):
    cur = conn.cursor()
    try:
        rows = cur.execute("""
            SELECT user_id, return_pct, win_rate, total_calls, total_tweets,
                   call_to_noise_ratio, avg_holding_hours,
                   composite_score, profit_grade, points,
                   rank_global, rank_change_24h,
                   win_streak, lose_streak,
                   most_recent_call_utc,
                   window, snapshot_ts
            FROM user_summary_24h
            ORDER BY rank_global ASC
        """).fetchall()
        cols = [d[0] for d in cur.description]
    except Exception:
        rows = cur.execute("""
            SELECT
              user_id,
              return_pct_w AS return_pct,
              win_rate, total_calls, total_tweets,
              call_to_noise_ratio, avg_holding_hours,
              composite_score, profit_grade, points,
              rank AS rank_global, rank_change_24h,
              win_streak, lose_streak,
              most_recent_call_utc,
              '24h' AS window, snapshot_ts
            FROM user_window_stats
            WHERE window='24h'
            ORDER BY rank ASC
        """).fetchall()
        cols = [d[0] for d in cur.description]

    data = [tuple(r) for r in rows]

    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, filename)
    tmp = dest + ".tmp"

    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(cols); w.writerows(data)
    os.replace(tmp, dest)

    if also_timestamped:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        dest_ts = os.path.join(out_dir, f"user_summary_24h_{ts}.csv")
        with open(dest_ts + ".tmp", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(cols); w.writerows(data)
        os.replace(dest_ts + ".tmp", dest_ts)
