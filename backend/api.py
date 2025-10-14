
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import pandas as pd
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from backend.config import load_env, get_db_path

try:
    from execution.api_trade import router as trade_router
except Exception:
    try:
        from backend.api_trade import router as trade_router  
    except Exception:
        trade_router = None

_HAVE_EXEC = False
executions_router = None
_ExecBase = _ExecEngine = _exec_resume = None

try:
    from execution.lifecycle import (
        router as executions_router,
        resume_inflight_on_startup as _exec_resume,
        Base as _ExecBase,
        engine as _ExecEngine,
    )
    _HAVE_EXEC = True
except Exception as _e1:
    try:
        from backend.executions.lifecycle import (
            router as executions_router,
            resume_inflight_on_startup as _exec_resume,
            Base as _ExecBase,
            engine as _ExecEngine,
        )
        _HAVE_EXEC = True
    except Exception as _e2:
        print(f"[api] WARNING: executions routes not mounted ({_e1} | {_e2})")

load_env()
DB_PATH = get_db_path()

app = FastAPI(title="Crypto Sentiment API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if trade_router is not None:
    app.include_router(trade_router)
else:
    print("[api] WARNING: trade routes not mounted (execution/api_trade.py not found)")



def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _one(sql: str, params=()) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        cur = conn.execute(sql, params)
        r = cur.fetchone()
        return dict(r) if r else None

def _rows(sql: str, params=()) -> List[Dict[str, Any]]:
    with _connect() as conn:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

def _how_long_ago(iso_str: Optional[str]) -> Optional[str]:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        secs = (now - dt.astimezone(timezone.utc)).total_seconds()
        if secs < 60:
            return f"{int(secs)}s ago"
        mins = int(secs // 60)
        if mins < 60:
            return f"{mins}m ago"
        hrs = int(mins // 60)
        if hrs < 24:
            return f"{hrs}h ago"
        days = int(hrs // 24)
        return f"{days}d ago"
    except Exception:
        return None

def _grade_from_score(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    s = float(score)
    if s >= 0.35: return "S+"
    if s >= 0.25: return "S"
    if s >= 0.15: return "A"
    if s >= 0.05: return "B"
    return "C"

def _grade_single_from_ret(ret24h_pct: Optional[float]) -> Optional[str]:
    if ret24h_pct is None:
        return None
    x = float(ret24h_pct)
    if x >= 35: return "S+"
    if x >= 25: return "S"
    if x >= 15: return "A"
    if x >= 5:  return "B"
    return "C"

def _direction_from_sentiment(sentiment: Optional[str]) -> str:
    s = (sentiment or "").lower()
    if s == "bullish": return "long"
    if s == "bearish": return "short"
    return "none"

def _direction_int_from_sentiment(sentiment: Optional[str]) -> int:
    s = (sentiment or "").lower()
    if s == "bullish": return 1
    if s == "bearish": return -1
    return 0

@app.get("/api/health")
def health():
    return {"ok": True, "db": DB_PATH}

# user profile
@app.get("/api/user/{username}/summary")

# interface IKolItem {
#   id: string;
#   name: string;
#   // timestamp
#   latestUpdateTime: string;
#   tweetsCount: number;
#   grade: string;
#   points: number;
#   result: number;
#   streak: number;
#   rank: number;
# }

# type KolList = IKolItem[];

def user_summary(username: str, window_h: int = Query(168, ge=1, le=720)):
    """
    Prefer leaderboard_cache / user_daily_stats; fallback to quick estimate from tweets.
    """
    lb = _one(
        """
        SELECT id, profit_grade, points, rank, score, updated_at
        FROM leaderboard_cache
        WHERE username=? AND window_h=?
        LIMIT 1
        """,
        (username, window_h),
    )

    uds = _one(
        """
        SELECT results_pct_cum AS results_pct, streak
        FROM user_daily_stats
        WHERE username=?
        ORDER BY date DESC
        LIMIT 1
        """,
        (username,),
    )
    
    # find tweet count from the tweets table
    tweet_count = _one(
        """
        SELECT COUNT(*) AS cnt
        FROM tweets
        WHERE username=?
        """,
        (username,),
    )
    tweet_count = int((tweet_count or {}).get("cnt") or 0)

    if not lb:
        agg = _one(
            """
            WITH recent AS (
              SELECT price_change_percent
              FROM tweets
              WHERE username=?
                AND price_change_percent IS NOT NULL
                AND datetime(tweet_time) > datetime('now','-168 hours')
            )
            SELECT AVG(price_change_percent)/100.0 AS rough_score_component
            FROM recent
            """,
            (username,),
        )
        score = float((agg or {}).get("rough_score_component") or 0.0) * 0.4
        lb = {"profit_grade": _grade_from_score(score), "points": 0.0, "rank": None, "score": score}

    streak = int((uds or {}).get("streak") or 0)
    if streak >= 5:
        streak_color, streak_emoji = "red", "🔥"
    elif streak >= 3:
        streak_color, streak_emoji = "orange", "🔥"
    elif streak >= 1:
        streak_color, streak_emoji = "green", ""
    else:
        streak_color, streak_emoji = "gray", ""

    return {
        "id": (lb or {}).get("id"),
        "name": username,
        "latestUpdateTime": (lb or {}).get("updated_at"),
        "tweetsCount": tweet_count,
        "profit_grade": (lb or {}).get("profit_grade") or _grade_from_score((lb or {}).get("score")),
        "points": float((lb or {}).get("points") or 0.0),
        "results_pct": (uds or {}).get("results_pct"),
        "streak": streak,
        "streak_color": streak_color,
        "streak_emoji": streak_emoji,
        "rank": (lb or {}).get("rank"),
    }

# interface IKolSignalItem {
#   // timestamp
#   updateTime: string;
#   // -1: bearish, 0: neutral, 1: bullish
#   emotionType: 0 | 1 | -1;
#   // 这里会涉及到一些关键词的解析，比如 BTC +6.92 +34.65%
#   // 在实际返回时可能要对这些字符加上标签，便于前端解析
#   content: string;
#   commentsCount: number;
#   retweetsCount: number;
#   likesCount: number;
#   token: string;
#   change: number;
# }

# type KolSignals = {
#   id: string;
#   name: string;
#   tweetsCount: number;
#   signals: IKolSignalItem[];
# };

@app.get("/api/user/{username}/signals")
def user_signals(username: str, limit: int = Query(50, ge=1, le=200)):
    """
    Recent tweets as signals (direction inferred from sentiment).
    """
    base = _rows(
        """
        SELECT id as tweet_id, username, sentiment, ticker, tweet_text,
               entry_price, current_price, price_change_percent, tweet_time
        FROM tweets
        WHERE username=?
          AND ticker IS NOT NULL
          AND ticker NOT IN ('NOISE','MARKET')
        ORDER BY datetime(tweet_time) DESC
        LIMIT ?
        """,
        (username, limit),
    )

    uds_latest = _one(
        """
        SELECT id, streak FROM user_daily_stats
        WHERE username=?
        ORDER BY date DESC
        LIMIT 1
        """,
        (username,),
    )
    user_streak = int((uds_latest or {}).get("streak") or 0)

    uds_7d = _rows(
        """
        SELECT results_pct_cum, date
        FROM user_daily_stats
        WHERE username=? AND date >= date('now','-6 days')
        ORDER BY date ASC
        """,
        (username,),
    )
    user_week_total_pct = None
    if uds_7d:
        try:
            user_week_total_pct = float(uds_7d[-1]["results_pct_cum"])
        except Exception:
            user_week_total_pct = None

    out = []
    for r in base:
        h = _one(
            """
            SELECT ret_close, ret_high
            FROM performance_horizons
            WHERE tweet_id=? AND horizon_h=24
            LIMIT 1
            """,
            (r["tweet_id"],),
        )
        ret24h_pct = (float(h["ret_close"]) * 100.0) if (h and h.get("ret_close") is not None) else None
        grade_single = _grade_single_from_ret(ret24h_pct)

        # progress = (cur - entry) / (max_since_entry - entry)
        progress_fraction = None
        if r.get("entry_price") is not None and r.get("ticker"):
            ph = _one(
                """
                SELECT MAX(price) AS maxp
                FROM price_history
                WHERE symbol=? AND datetime(timestamp) >= datetime(?)
                """,
                (r["ticker"], r["tweet_time"]),
            )
            try:
                entry = float(r["entry_price"])
                cur = float(r["current_price"]) if r.get("current_price") is not None else None
                maxp = float(ph["maxp"]) if (ph and ph.get("maxp") is not None) else None
                if cur is None:
                    ph2 = _one(
                        """
                        SELECT price AS lastp
                        FROM price_history
                        WHERE symbol=? AND datetime(timestamp) >= datetime(?)
                        ORDER BY datetime(timestamp) DESC
                        LIMIT 1
                        """,
                        (r["ticker"], r["tweet_time"]),
                    )
                    cur = float(ph2["lastp"]) if ph2 and ph2.get("lastp") is not None else entry
                if maxp is not None and maxp > entry and cur is not None:
                    progress_fraction = max(0.0, min(1.0, (cur - entry) / (maxp - entry)))
                else:
                    progress_fraction = 0.0
            except Exception:
                progress_fraction = None

        out.append(
            {
                "x_handle": r["username"],
                "profit_grade": grade_single,
                "signal_id": r.get("tweet_id"),
                "entry_price": r.get("entry_price"),
                "win_streak": user_streak,
                "progress_bar": progress_fraction,
                "user_week_total_pct": user_week_total_pct,
                "ticker": r.get("ticker"),
                "bull_or_bear": r.get("sentiment"),
                #
                "emotionType": _direction_int_from_sentiment(r.get("sentiment")),
                "updateTime": _how_long_ago(r.get("tweet_time")),
                "content": r.get("tweet_text"),
                "commentsCount": 0, #place holder
                "retweetsCount": 0, #place holder
                "likesCount": 0, #place holder
                "token": "", #place holder
                "change_since_tweet": r.get("price_change_percent"),
            }
        )
    return {
        "id": uds_latest.get("id") if uds_latest else None,
        "name": username,
        "tweetsCount": len(out),
        "signals": out,
    }

# leaderboard
@app.get("/api/leaderboard")
def leaderboard(window_h: int = Query(168, ge=1, le=720),
                limit: int = Query(100, ge=1, le=500)):
    rows = _rows(
        """
        SELECT
          L.username,
          L.hit_rate,
          L.calls,
          L.signal_noise,
          L.profit_grade,
          L.points,
          L.score,
          L.rank,
          U.results_pct_cum AS results_pct
        FROM leaderboard_cache L
        LEFT JOIN user_daily_stats U
          ON U.username = L.username
         AND U.date = (SELECT MAX(date) FROM user_daily_stats WHERE username = L.username)
        WHERE L.window_h = ?
        ORDER BY L.rank ASC, L.score DESC
        LIMIT ?
        """,
        (window_h, limit),
    )

    if rows:
        out = []
        for row in rows:
            latest = _one(
                """
                SELECT ticker, sentiment, price_change_percent, tweet_time
                FROM tweets
                WHERE username=?
                  AND ticker IS NOT NULL
                  AND ticker NOT IN ('NOISE','MARKET')
                ORDER BY datetime(tweet_time) DESC
                LIMIT 1
                """,
                (row["username"],),
            )
            out.append(
                {
                    "x_handle": row["username"],
                    "bull_or_bear": (latest or {}).get("sentiment"),
                    "win_rate": float(row.get("hit_rate") or 0.0),
                    "total_tweets": int(row.get("calls") or 0),
                    "signal_to_noise": float(row.get("signal_noise") or 0.0),
                    "results_pct": row.get("results_pct"),
                    "ticker": (latest or {}).get("ticker"),
                    "direction": _direction_from_sentiment((latest or {}).get("sentiment")),
                    "how_long_ago": _how_long_ago((latest or {}).get("tweet_time")),
                    "tweet_performance": (latest or {}).get("price_change_percent"),
                    "copy_button": True,
                    "counter_button": True,
                }
            )
        return out

    window_clause = f"-{int(window_h)} hours"
    agg = _rows(
        """
        SELECT username,
               COUNT(*) AS n,
               AVG(price_change_percent) AS avg_perf,
               SUM(CASE WHEN price_change_percent > 0 THEN 1 ELSE 0 END) AS wins
        FROM tweets
        WHERE price_change_percent IS NOT NULL
          AND ticker IS NOT NULL
          AND ticker NOT IN ('NOISE','MARKET')
          AND datetime(tweet_time) > datetime('now', ?)
        GROUP BY username
        HAVING n > 0
        ORDER BY avg_perf DESC, n DESC
        LIMIT ?
        """,
        (window_clause, limit),
    )

    out = []
    for row in agg:
        latest = _one(
            """
            SELECT ticker, sentiment, price_change_percent, tweet_time
            FROM tweets
            WHERE username=?
              AND ticker IS NOT NULL
              AND ticker NOT IN ('NOISE','MARKET')
            ORDER BY datetime(tweet_time) DESC
            LIMIT 1
            """,
            (row["username"],),
        )
        n = int(row["n"])
        win_rate = (float(row["wins"]) / n) if n else 0.0
        results_pct = float(row["avg_perf"]) if row["avg_perf"] is not None else None
        out.append(
            {
                "x_handle": row["username"],
                "bull_or_bear": (latest or {}).get("sentiment"),
                "win_rate": win_rate,
                "total_tweets": n,
                "signal_to_noise": 1.0,
                "results_pct": results_pct,
                "ticker": (latest or {}).get("ticker"),
                "direction": _direction_from_sentiment((latest or {}).get("sentiment")),
                "how_long_ago": _how_long_ago((latest or {}).get("tweet_time")),
                "tweet_performance": (latest or {}).get("price_change_percent"),
                "copy_button": True,
                "counter_button": True,
            }
        )
    return out

# summary
@app.get("/api/summary")
def summary(hours: int = Query(120, ge=1, le=720), eps: float = 2.0):
    with _connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT
                ticker,
                sentiment,
                COUNT(*) AS tweet_count,
                AVG(price_change_percent) AS avg_performance,
                MIN(price_change_percent) AS min_performance,
                MAX(price_change_percent) AS max_performance,
                COUNT(CASE WHEN price_change_percent >  ? THEN 1 END) AS positive_count,
                COUNT(CASE WHEN price_change_percent < -? THEN 1 END) AS negative_count,
                COUNT(CASE WHEN ABS(price_change_percent) <= ? THEN 1 END) AS zero_count
            FROM tweets
            WHERE ticker IS NOT NULL
              AND ticker NOT IN ('NOISE','MARKET')
              AND price_change_percent IS NOT NULL
              AND datetime(tweet_time) > datetime('now', ?)
            GROUP BY ticker, sentiment
            """,
            conn,
            params=[eps, eps, eps, f"-{hours} hours"],
        )

    if df.empty:
        return {
            "overall": {
                "total_tweets": 0,
                "avg_performance": 0.0,
                "positive": 0,
                "negative": 0,
                "zero": 0,
                "hours": hours,
                "eps": eps,
            },
            "by_sentiment": [],
            "top_tickers": [],
        }

    total = int(df["tweet_count"].sum())
    overall_avg = float((df["avg_performance"] * df["tweet_count"]).sum() / total) if total else 0.0
    overall = {
        "total_tweets": total,
        "avg_performance": overall_avg,
        "positive": int(df["positive_count"].sum()),
        "negative": int(df["negative_count"].sum()),
        "zero": int(df["zero_count"].sum()),
        "hours": hours,
        "eps": eps,
    }

    by_sentiment = []
    for s in ["bullish", "bearish", "neutral"]:
        sd = df[df["sentiment"] == s]
        n = int(sd["tweet_count"].sum())
        if n > 0:
            avg = float((sd["avg_performance"] * sd["tweet_count"]).sum() / n)
            by_sentiment.append({"sentiment": s, "avg_performance": avg, "tweet_count": n})

    top = df.sort_values("avg_performance", ascending=False).head(10)
    top_tickers = [
        {
            "ticker": r["ticker"],
            "sentiment": r["sentiment"],
            "avg_performance": float(r["avg_performance"]),
            "tweet_count": int(r["tweet_count"]),
        }
        for _, r in top.iterrows()
    ]
    return {"overall": overall, "by_sentiment": by_sentiment, "top_tickers": top_tickers}

@app.get("/api/horizon")
def horizon(h: int = Query(24, ge=1, le=168), eps: float = Query(0.02), topn: int = 10):
    with _connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT t.username, t.ticker, t.sentiment, t.tweet_time,
                   h.ret_close, h.ret_high, h.ret_low, h.ret_close_alpha
            FROM performance_horizons h
            JOIN tweets t ON t.id = h.tweet_id
            WHERE h.horizon_h = ?
            """,
            conn,
            params=[int(h)],
        )

    if df.empty:
        return {"dist": None, "winners": [], "losers": []}

    rc = df["ret_close"].astype(float)
    pos = int((rc > eps / 100.0).sum())
    neg = int((rc < -eps / 100.0).sum())
    zer = int((rc.abs() <= eps / 100.0).sum())
    n = int(len(df))

    rc_pct = rc * 100.0
    dist = {
        "samples": n,
        "hit": pos,
        "miss": neg,
        "zero": zer,
        "mean_pct": float(rc_pct.mean()),
        "median_pct": float(rc_pct.median()),
        "p25_pct": float(rc_pct.quantile(0.25)),
        "p75_pct": float(rc_pct.quantile(0.75)),
        "h": h,
        "eps_pct": eps,
    }

    def _pack(rows: pd.DataFrame):
        out = []
        for _, r in rows.iterrows():
            out.append(
                {
                    "username": r["username"],
                    "ticker": r["ticker"],
                    "sentiment": r["sentiment"],
                    "tweet_time": r["tweet_time"],
                    "ret_close": None if pd.isna(r["ret_close"]) else float(r["ret_close"]),
                    "ret_high": None if pd.isna(r["ret_high"]) else float(r["ret_high"]),
                    "ret_low": None if pd.isna(r["ret_low"]) else float(r["ret_low"]),
                    "alpha": None if pd.isna(r["ret_close_alpha"]) else float(r["ret_close_alpha"]),
                }
            )
        return out

    winners = df.sort_values("ret_close", ascending=False).head(topn)
    losers = df.sort_values("ret_close", ascending=True).head(topn)
    return {"dist": dist, "winners": _pack(winners), "losers": _pack(losers)}

@app.get("/api/leaderboard_legacy")
def leaderboard_legacy(
    hours: int = Query(168, ge=1, le=720),
    eps: float = 0.02,
    min_calls: int = Query(3, ge=1),
    topn: int = Query(50, ge=1, le=200),
):
    with _connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT username,
                   COUNT(*) AS tweet_count,
                   AVG(price_change_percent) AS avg_perf,
                   SUM(CASE WHEN price_change_percent >  ? THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN price_change_percent < -? THEN 1 ELSE 0 END) AS negative,
                   SUM(CASE WHEN ABS(price_change_percent) <= ? THEN 1 ELSE 0 END) AS zero
            FROM tweets
            WHERE price_change_percent IS NOT NULL
              AND username IS NOT NULL AND username != ''
              AND datetime(tweet_time) > datetime('now', ?)
            GROUP BY username
            HAVING tweet_count >= ?
            ORDER BY avg_perf DESC, tweet_count DESC
            LIMIT ?
            """,
            conn,
            params=[eps, eps, eps, f"-{hours} hours", int(min_calls), int(topn)],
        )

    rows = []
    for _, r in df.iterrows():
        n = int(r["tweet_count"])
        pos = int(r["positive"])
        neg = int(r["negative"])
        zero = int(r["zero"])
        hit_rate = (pos / n) * 100.0 if n else 0.0
        rows.append(
            {
                "username": r["username"],
                "avg_perf": float(r["avg_perf"]),
                "tweet_count": n,
                "positive": pos,
                "negative": neg,
                "zero": zero,
                "hit_rate": hit_rate,
            }
        )
    return {"rows": rows, "hours": hours, "eps": eps, "min_calls": min_calls}

@app.get("/api/best")
def best(
    limit: int = Query(20, ge=1, le=200),
    sentiment: Optional[str] = Query(None),
    max_abs_pct: float = Query(5000.0),
):
    where_sent = "" if not sentiment else "AND sentiment = ?"
    params = [max_abs_pct] + ([sentiment] if sentiment else [])

    with _connect() as conn:
        df = pd.read_sql_query(
            f"""
            SELECT username, ticker, sentiment, tweet_text, tweet_time,
                   entry_price, current_price, price_change_percent
            FROM tweets
            WHERE price_change_percent IS NOT NULL
              AND entry_price IS NOT NULL
              AND current_price IS NOT NULL
              AND ABS(price_change_percent) <= ?
              {where_sent}
            ORDER BY price_change_percent DESC
            LIMIT {int(limit)}
            """,
            conn,
            params=params,
        )

    rows = []
    for _, r in df.iterrows():
        rows.append(
            {
                "username": r["username"],
                "ticker": r["ticker"],
                "sentiment": r["sentiment"],
                "tweet_text": r["tweet_text"],
                "tweet_time": r["tweet_time"],
                "entry_price": float(r["entry_price"]),
                "current_price": float(r["current_price"]),
                "delta_pct": float(r["price_change_percent"]),
            }
        )
    return {"rows": rows}

from fastapi import APIRouter
debug_router = APIRouter(prefix="/api", tags=["debug"])
@debug_router.get("/debug/routes")
def list_routes():
    return [r.path for r in app.router.routes]

app.include_router(debug_router)
