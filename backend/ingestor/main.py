from __future__ import annotations
import os, re, time, json, hashlib, random, sqlite3, requests
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict, Any

import openai
from dotenv import load_dotenv
load_dotenv()

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

# ── paths & secrets ────────────────────────────────────────────────────
DATA_DIR = _env("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
LABEL_CACHE_PATH = os.path.join(DATA_DIR, "label_cache.sqlite")
STATE_DB_PATH    = os.path.join(DATA_DIR, "ingestor_state.sqlite")

OPENAI_API_KEY = _env("OPENAI_API_KEY")
X_BEARER_TOKEN = _env("X_BEARER_TOKEN")

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in .env")
if not X_BEARER_TOKEN:
    raise RuntimeError("Missing X_BEARER_TOKEN in .env")

openai.api_key = OPENAI_API_KEY
SCRAPE_USERS_ENV = _env("SCRAPE_USERS", "")

# ── LLM model config ──────────────────────────────────────────────────
LLM_MODEL = _env("LLM_MODEL", "gpt-4o-mini")

# ── regex / constants ──────────────────────────────────────────────────
ALNUM_RE         = re.compile(r"[^A-Z0-9]")
DOLLAR_TICKER_RE = re.compile(r"\$([A-Za-z0-9]{2,15})\b")
HASH_TICKER_RE   = re.compile(r"#([A-Za-z0-9]{2,15})\b")
PLAIN_TICKER_RE  = re.compile(r"\b([A-Z]{2,10})\b")

COMMON_CRYPTO = {
    "BTC","ETH","SOL","XRP","BNB","ADA","AVAX","DOT","LINK","TON",
    "TRX","ATOM","NEAR","UNI","LTC","ETC","FIL","ICP","HBAR",
    "ARB","OP","MATIC","APT","SUI","SEI","TIA","INJ","STX","STRK",
    "MANTA","ZK","ZRO","BLAST","METIS",
    "AAVE","DYDX","JUP","ONDO","ENA","PENDLE","MKR","CRV","GMX",
    "ETHFI","EIGEN","MORPHO",
    "FET","TAO","RNDR","ARKM","WLD","VIRTUAL","AI16Z","ARC","GRIFFAIN",
    "DOGE","PEPE","WIF","BONK","FLOKI","SHIB","TRUMP","MELANIA",
    "MOG","TURBO","POPCAT","GOAT","FARTCOIN","SPX","GIGA","BRETT",
    "IMX","BEAM","GALA","SAND","MANA","RONIN",
    "ORDI","RUNE","SATS","STX",
    "DYDX","PYTH","KAS","JTO","NOT","HYPE",
    "SUI","FTM","ROSE","AR","MINA","HNT","CHZ",
}

# ── sentiment phrases (context-aware) ─────────────────────────────────
POS_PHRASES = [
    "longed","longing","going long","opened a long","opening long",
    "entered long","long position","long here","long from",
    "buying","bought","loaded","loading up","adding","added more",
    "accumulating","accumulated","bid","bidding",
    "bullish","bull case","bull flag","bull run","bull market",
    "breakout","breaking out","broke out",
    "pumping","ripping","flying","sending it","mooning",
    "rally","rallying","surging","soaring","up only",
    "new high","higher high","ath",
    "bounce","bouncing","recovery","reversal to the upside",
    "bottomed","bottom is in","dip buy","buying the dip","btfd",
    "undervalued","outperform","strong","holding strong",
    "🚀","✅","🔥","📈","🟢","💰","💎","⬆️",
]

NEG_PHRASES = [
    "shorted","shorting","going short","opened a short","opening short",
    "entered short","short position","short here","short from",
    "selling","sold","exited","closed my","taking profit","tp hit",
    "fading","faded","cutting",
    "bearish","bear case","bear flag","bear market",
    "breakdown","breaking down","broke down",
    "dumping","dumped","crashing","crashed","nuked","tanking","drilling",
    "plunging","plunged",
    "rejection","rejected","lower low","new low",
    "topped","top signal","top is in",
    "overvalued","weak","dead cat","bull trap",
    "rekt","liquidated","down bad",
    "rug","rugged","rugpull","scam",
    "🟥","🔻","📉","⬇️","💀","🪦",
]

# Phrases that LOOK directional but AREN'T trade signals
FALSE_POS_PATTERNS = [
    "short term","short-term","short squeeze","short covering",
    "in short","short summary","shorts getting","shorts are",
    "long term","long-term","long way","long time","long run",
    "as long as","so long","how long","before long","long story",
    "not long","no longer",
]

# ── direction detection phrases ───────────────────────────────────────
LONG_DIRECTION_PHRASES = [
    "longed","longing","going long","opened a long","opening long",
    "entered long","long position","long here","long from","long entry",
    "buying","bought","loaded","loading","adding","accumulated",
    "dip buy","buying the dip","btfd",
    "short squeeze","shorts getting liquidated","shorts getting rekt",
]

SHORT_DIRECTION_PHRASES = [
    "shorted","shorting","going short","opened a short","opening short",
    "entered short","short position","short here","short from","short entry",
    "selling","sold","fading","faded","cutting losses",
]

# ── ticker normalization ──────────────────────────────────────────────
PAIR_SEPARATORS = ["/", "-", "_", ":"]
STABLE_SUFFIXES = ["USDT", "USDC", "USD"]
PERP_SUFFIXES   = ["-PERP","PERP","-PERPETUAL","PERPETUAL","_PERP",".P"]

ALIAS_TO_CANON: Dict[str, str] = {
    "XBT":"BTC","WBTC":"BTC","BTCB":"BTC","BITCOIN":"BTC",
    "BTC-PERP":"BTC","BTCUSDT":"BTC","BTCUSD":"BTC",
    "WETH":"ETH","ETH2":"ETH","ETHEREUM":"ETH",
    "ETH-PERP":"ETH","ETHUSDT":"ETH","ETHUSD":"ETH",
    "SOLANA":"SOL","SOL-PERP":"SOL","SOLUSDT":"SOL",
    "ARBITRUM":"ARB","ARBIT":"ARB","ARB-USD":"ARB",
    "OPTIMISM":"OP","OP-USD":"OP",
    "POL":"MATIC",
    "CELESTIA":"TIA",
    "STARKNET":"STRK",
    "BCC":"BCH","BCHABC":"BCH","BCHSV":"BSV",
    "RIPPLE":"XRP","XRP-USD":"XRP",
    "XDG":"DOGE","DOG":"DOGE","DOGECOIN":"DOGE",
    "SHIBAINU":"SHIB","SHIBA":"SHIB",
    "TETHER":"USDT","USDCOIN":"USDC",
    "LNK":"LINK","CHAINLINK":"LINK",
    "TRON":"TRX","TRX-USD":"TRX",
    "POLKADOT":"DOT","DOT-USD":"DOT",
    "COSMOS":"ATOM",
    "RENDER":"RNDR",
    "WORLDCOIN":"WLD",
    "FANTOM":"FTM","SONIC":"FTM",
    "IOTA-USD":"IOTA","MIOTA":"IOTA",
    "NAN0":"NANO",
    "BTTOLD":"BTT","BTTNEW":"BTT",
    "OCEAN":"FET","AGIX":"FET",
    "ONDO-PERP":"ONDO","PEPE-PERP":"PEPE","DOGE-PERP":"DOGE",
    "ARB-PERP":"ARB","SUI-PERP":"SUI","APT-PERP":"APT",
    "AVAX-PERP":"AVAX","LINK-PERP":"LINK","INJ-PERP":"INJ",
}

STRICT_CANON = False
SUPPORTED_CANON: set[str] = set(ALIAS_TO_CANON.values()) | COMMON_CRYPTO

TICKER_BLACKLIST = {
    "THE","AND","FOR","ARE","BUT","NOT","YOU","ALL","CAN","HER","WAS","ONE",
    "OUR","OUT","HAS","HIS","HOW","ITS","MAY","NEW","NOW","OLD","SEE","WAY",
    "WHO","DID","GET","HIM","LET","SAY","SHE","TOO","USE","DAD","MOM","USA",
    "CEO","CTO","COO","CFO","IMO","TBH","LMAO","HODL","NGMI","WAGMI","DYOR",
    "NFA","PSA","FYI","ATH","ATL","API","ETF","SEC","USD","EUR","JPY","GBP",
    "IPO","ICO","IDO","CEO","DEX","CEX","TVL","APR","APY","ROI","PNL","OTC",
    "NFT","DAO","DCA","KOL","RWA","GM","GN","CT","RT","DM","PM","AM",
    "JUST","THIS","THAT","WITH","FROM","THEY","BEEN","HAVE","WILL","YOUR",
    "WHEN","WHAT","MORE","MAKE","LIKE","TIME","VERY","THAN","LOOK","ONLY",
    "COME","OVER","ALSO","BACK","SOME","THEM","MOST","INTO","YEAR","TAKE",
    "LONG","SHORT","HIGH","LOW","UP","DOWN","BIG","TOP","GOOD","BEST",
    "FREE","REAL","FULL","HARD","EASY","FAST","LAST","NEXT","OPEN","RISK",
    "HUGE","MOVE","PUMP","DUMP","SEND","MOON","HOLD",
    "LIVE","JUST","HERE","STILL","EVERY","MUCH","EVEN",
    "WIN","LOSS","CALL","PUT","STOP","ENTRY","EXIT",
    "WEEK","DAILY","TODAY","SOON",
}

# ── LLM few-shot examples ─────────────────────────────────────────────
LLM_FEW_SHOT_EXAMPLES = [
    {"tweet": "$BTC looking strong here, longed at 67.5k. TP 72k, SL 65k 🚀",
     "label": {"ticker": "BTC", "sentiment": "bullish", "direction": "long"}},
    {"tweet": "Shorted $ETH at 2450. This is going to 2200. Bear flag on 4H.",
     "label": {"ticker": "ETH", "sentiment": "bearish", "direction": "short"}},
    {"tweet": "GM CT! What a wild week. Markets are crazy right now. Stay safe out there.",
     "label": {"ticker": "NOISE", "sentiment": "neutral", "direction": "long"}},
    {"tweet": "$SOL breaking out while $BTC chops. Longed SOL at $98, this is going to $120+",
     "label": {"ticker": "SOL", "sentiment": "bullish", "direction": "long"}},
    {"tweet": "$DOGE looks weak. Expecting a dump to 0.12. Not touching this.",
     "label": {"ticker": "DOGE", "sentiment": "bearish", "direction": "short"}},
    {"tweet": "🧵 Thread: Top 10 altcoins for 2025. Like and RT if you want me to cover more!",
     "label": {"ticker": "NOISE", "sentiment": "neutral", "direction": "long"}},
    {"tweet": "$BTC whale just moved 5000 BTC to Binance. Watch closely.",
     "label": {"ticker": "BTC", "sentiment": "neutral", "direction": "long"}},
    {"tweet": "$BTC short squeeze incoming. Funding is super negative, shorts gonna get rekt 🔥",
     "label": {"ticker": "BTC", "sentiment": "bullish", "direction": "long"}},
]


# ═══════════════════════════════════════════════════════════════════════
#  TICKER NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════

def _strip_contract_suffix(sym: str) -> str:
    for suf in PERP_SUFFIXES:
        if sym.endswith(suf):
            base = sym[: -len(suf)]
            return base if base else "PERP"
    return sym


def _strip_stable_quote_pair(sym: str) -> str:
    u = sym.upper()
    for sep in PAIR_SEPARATORS:
        if sep in u:
            parts = [p for p in u.split(sep) if p]
            if len(parts) == 2:
                a, b = parts
                if b in STABLE_SUFFIXES:
                    return a
                if any(b.endswith(s) or b == s for s in PERP_SUFFIXES):
                    return a
                if a in STABLE_SUFFIXES:
                    return b
            if len(parts) >= 3:
                for p in parts:
                    if p not in STABLE_SUFFIXES and not any(
                        p.endswith(s) or p == s for s in PERP_SUFFIXES
                    ):
                        return p
    for q in STABLE_SUFFIXES:
        if u.endswith(q) and len(u) > len(q) + 1:
            return u[: -len(q)]
    return u


def normalize_ticker(raw: str) -> str:
    if not raw:
        return "NOISE"
    s = raw.strip().upper().replace("$", "").replace("#", "").replace(" ", "")
    s = _strip_contract_suffix(s)
    s = _strip_stable_quote_pair(s)
    s = ALNUM_RE.sub("", s)
    if s in ALIAS_TO_CANON:
        s = ALIAS_TO_CANON[s]
    if s.endswith("PERP") and s != "PERP":
        s = s[:-4]
    s = ALNUM_RE.sub("", s)
    if not (2 <= len(s) <= 15):
        return "NOISE"
    if STRICT_CANON and s not in SUPPORTED_CANON:
        return "NOISE"
    return s


# ═══════════════════════════════════════════════════════════════════════
#  INGESTOR STATE DB
# ═══════════════════════════════════════════════════════════════════════

def _state_db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(STATE_DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            username         TEXT PRIMARY KEY,
            user_id          TEXT NOT NULL,
            last_tweet_id    TEXT,
            avg_tweets_per_day REAL NOT NULL DEFAULT 0,
            empty_polls      INTEGER NOT NULL DEFAULT 0,
            poll_interval_h  REAL NOT NULL DEFAULT 2.0,
            last_polled_at   TEXT,
            updated_at       TEXT NOT NULL
        )
    """)
    for col, defn in [
        ("avg_tweets_per_day", "REAL NOT NULL DEFAULT 0"),
        ("empty_polls",        "INTEGER NOT NULL DEFAULT 0"),
        ("poll_interval_h",    "REAL NOT NULL DEFAULT 2.0"),
        ("last_polled_at",     "TEXT"),
    ]:
        try:
            con.execute(f"ALTER TABLE user_state ADD COLUMN {col} {defn}")
        except Exception:
            pass
    con.commit()
    return con


MIN_POLL_INTERVAL_H = 1.0
MAX_POLL_INTERVAL_H = 24.0
BACKOFF_FACTOR      = 1.5
SPEEDUP_FACTOR      = 0.7


def _state_get_user_id(con: sqlite3.Connection, username: str) -> Optional[str]:
    row = con.execute(
        "SELECT user_id FROM user_state WHERE username = ?", (username,)
    ).fetchone()
    return row[0] if row else None


def _state_get_since_id(con: sqlite3.Connection, username: str) -> Optional[str]:
    row = con.execute(
        "SELECT last_tweet_id FROM user_state WHERE username = ?", (username,)
    ).fetchone()
    return row[0] if row else None


def _state_should_poll(con: sqlite3.Connection, username: str) -> bool:
    row = con.execute(
        "SELECT last_polled_at, poll_interval_h FROM user_state WHERE username = ?",
        (username,),
    ).fetchone()
    if not row or not row[0]:
        return True
    last_polled = datetime.fromisoformat(row[0])
    interval_h = row[1] or 2.0
    next_poll = last_polled + timedelta(hours=interval_h)
    return datetime.now(timezone.utc) >= next_poll


def _state_save(con: sqlite3.Connection, username: str, user_id: str,
                last_tweet_id: Optional[str] = None,
                tweets_found: int = 0):
    now = datetime.now(timezone.utc).isoformat()
    existing = con.execute(
        "SELECT last_tweet_id, avg_tweets_per_day, empty_polls, poll_interval_h "
        "FROM user_state WHERE username = ?", (username,)
    ).fetchone()

    if existing:
        old_avg = existing[1] or 0.0
        old_empty = existing[2] or 0
        old_interval = existing[3] or 2.0
        new_avg = old_avg * 0.7 + (tweets_found / max(old_interval / 24.0, 0.04)) * 0.3
        if tweets_found == 0:
            new_empty = old_empty + 1
            new_interval = min(old_interval * BACKOFF_FACTOR, MAX_POLL_INTERVAL_H)
        else:
            new_empty = 0
            if new_avg > 5:
                target = MIN_POLL_INTERVAL_H
            elif new_avg > 2:
                target = 2.0
            elif new_avg > 0.5:
                target = 4.0
            else:
                target = 8.0
            new_interval = max(old_interval * SPEEDUP_FACTOR, target, MIN_POLL_INTERVAL_H)
        con.execute(
            """UPDATE user_state SET user_id=?, last_tweet_id=COALESCE(?,last_tweet_id),
               avg_tweets_per_day=?, empty_polls=?, poll_interval_h=?,
               last_polled_at=?, updated_at=?
               WHERE username=?""",
            (user_id, last_tweet_id, round(new_avg, 2), new_empty,
             round(new_interval, 1), now, now, username),
        )
    else:
        con.execute(
            """INSERT INTO user_state
               (username, user_id, last_tweet_id, avg_tweets_per_day,
                empty_polls, poll_interval_h, last_polled_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (username, user_id, last_tweet_id, 0.0, 0, 2.0, now, now),
        )
    con.commit()


# ═══════════════════════════════════════════════════════════════════════
#  LABEL CACHE
# ═══════════════════════════════════════════════════════════════════════

def _label_cache_connect() -> sqlite3.Connection:
    con = sqlite3.connect(LABEL_CACHE_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS label_cache (
            tweet_hash TEXT PRIMARY KEY,
            ticker     TEXT NOT NULL,
            sentiment  TEXT NOT NULL,
            direction  TEXT NOT NULL DEFAULT 'long',
            created_at TEXT NOT NULL
        )
    """)
    try:
        con.execute("ALTER TABLE label_cache ADD COLUMN direction TEXT NOT NULL DEFAULT 'long'")
    except Exception:
        pass
    return con


def _label_cache_get(con: sqlite3.Connection, h: str) -> Optional[Tuple[str, str, str]]:
    row = con.execute(
        "SELECT ticker, sentiment, direction FROM label_cache WHERE tweet_hash = ?", (h,)
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def _label_cache_put(con: sqlite3.Connection, h: str, ticker: str, sentiment: str, direction: str):
    con.execute(
        "INSERT OR REPLACE INTO label_cache (tweet_hash, ticker, sentiment, direction, created_at) "
        "VALUES (?,?,?,?,?)",
        (h, ticker, sentiment, direction, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


def _stable_tweet_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════════
#  CHEAP HEURISTIC LABELING (UPGRADED — context-aware)
# ═══════════════════════════════════════════════════════════════════════

def _cheap_ticker(text: str) -> Optional[str]:
    """
    Extract the PRIMARY ticker from tweet text.
    When multiple $TICKERs found, pick the one closest to action words.
    """
    if not text:
        return None

    # 1) Collect ALL $TICKER mentions with position
    dollar_tickers = []
    for m in DOLLAR_TICKER_RE.finditer(text):
        sym = normalize_ticker(m.group(1))
        if sym != "NOISE" and sym not in TICKER_BLACKLIST:
            dollar_tickers.append((sym, m.start()))

    if dollar_tickers:
        if len(dollar_tickers) == 1:
            return dollar_tickers[0][0]

        # Multiple $TICKERs: score by proximity to action words + frequency
        t_lower = text.lower()
        best_sym = None
        best_score = -1

        ticker_counts: Dict[str, int] = {}
        for sym, _ in dollar_tickers:
            ticker_counts[sym] = ticker_counts.get(sym, 0) + 1

        for sym, pos in dollar_tickers:
            score = 0
            score += ticker_counts[sym] * 2

            context_start = max(0, pos - 40)
            context_end = min(len(text), pos + 80)
            context = text[context_start:context_end].lower()

            action_words = [
                "long","short","buy","sell","entry","target",
                "tp","sl","stop","loaded","shorted","longed",
                "bullish","bearish","pump","dump","breakout",
            ]
            for w in action_words:
                if w in context:
                    score += 3

            if pos == dollar_tickers[0][1]:
                score += 1

            if score > best_score:
                best_score = score
                best_sym = sym

        return best_sym

    # 2) #TICKER — trust if in COMMON_CRYPTO
    for m in HASH_TICKER_RE.finditer(text):
        sym = normalize_ticker(m.group(1))
        if sym in COMMON_CRYPTO:
            return sym

    # 3) Bare uppercase — only match known crypto
    candidates = set()
    for c in PLAIN_TICKER_RE.findall(text.upper()):
        sym = normalize_ticker(c)
        if sym in COMMON_CRYPTO and sym not in TICKER_BLACKLIST:
            candidates.add(sym)
    if len(candidates) == 1:
        return next(iter(candidates))

    return None


def _cheap_sentiment(text: str) -> Optional[str]:
    """
    Context-aware sentiment detection using phrase matching.
    Returns None for ambiguous cases → routed to LLM.
    """
    if not text:
        return None
    t = text.lower()

    pos_score = 0
    neg_score = 0

    for phrase in POS_PHRASES:
        if phrase in t:
            is_false = False
            for fp in FALSE_POS_PATTERNS:
                if fp in t and phrase in fp:
                    is_false = True
                    break
            if not is_false:
                pos_score += len(phrase.split())

    for phrase in NEG_PHRASES:
        if phrase in t:
            is_false = False
            for fp in FALSE_POS_PATTERNS:
                if fp in t and phrase in fp:
                    is_false = True
                    break
            if not is_false:
                neg_score += len(phrase.split())

    if pos_score >= 2 and pos_score > neg_score * 1.5:
        return "bullish"
    if neg_score >= 2 and neg_score > pos_score * 1.5:
        return "bearish"

    return None


def _sentiment_to_direction(sentiment: str, text: str) -> str:
    """Determine trade direction from explicit phrases, fallback to sentiment."""
    t = text.lower()

    long_score = sum(1 for p in LONG_DIRECTION_PHRASES if p in t)
    short_score = sum(1 for p in SHORT_DIRECTION_PHRASES if p in t)

    if long_score > short_score:
        return "long"
    if short_score > long_score:
        return "short"

    return "short" if sentiment == "bearish" else "long"


# ═══════════════════════════════════════════════════════════════════════
#  OPENAI LABELING (UPGRADED — few-shot examples)
# ═══════════════════════════════════════════════════════════════════════

def _llm_request(
    messages: List[Dict[str, Any]],
    max_tokens: int,
    temperature: float,
    retries: int = 4,
    base_delay: float = 1.0,
) -> Tuple[str, int, int]:
    last_err = None
    for attempt in range(retries):
        try:
            resp = openai.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            content = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            tin  = getattr(usage, "prompt_tokens", 0) if usage else 0
            tout = getattr(usage, "completion_tokens", 0) if usage else 0
            return content, tin, tout
        except Exception as e:
            last_err = e
            wait = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            print(f"[LLM] Error ({attempt+1}/{retries}): {e} → retry in {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"OpenAI request failed after retries: {last_err}")


def llm_batch_label(
    items: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, str]], int, int]:
    """Batch label tweets with few-shot examples for accuracy."""
    examples_str = "\n".join([
        f'  Tweet: "{ex["tweet"]}" → {json.dumps(ex["label"])}'
        for ex in LLM_FEW_SHOT_EXAMPLES
    ])

    user_payload = {
        "task": "label_crypto_tweets",
        "schema": {"id": "str", "ticker": "str", "sentiment": "str", "direction": "str"},
        "rules": [
            "Return JSON: {\"labels\": [{id, ticker, sentiment, direction}, ...]}",
            "Ticker: the PRIMARY crypto being traded/analyzed. Use symbol (BTC, ETH, SOL, HYPE, etc).",
            "  - 'NOISE' if tweet is not about a specific crypto trade/analysis (promo, thread, GM, engagement bait, personal life).",
            "  - 'MARKET' if about general crypto market with no specific coin.",
            "  - If multiple tickers, pick the one the trader is ACTING on or analyzing most.",
            "Sentiment: 'bullish', 'bearish', or 'neutral'.",
            "  - bullish: buying, longing, expecting up move, positive outlook.",
            "  - bearish: selling, shorting, expecting down move, negative outlook.",
            "  - neutral: data/news sharing, no clear directional bias.",
            "Direction: 'long' or 'short' — the trader's POSITION, not market direction.",
            "  - 'short squeeze' = direction 'long' (expecting price UP).",
            "  - 'bearish analysis without position' = direction 'short'.",
            "  - If unclear, default: bullish→long, bearish→short, neutral→long.",
            "No extra keys. No commentary. Strict JSON only.",
        ],
        "examples": examples_str,
        "items": [{"id": it["id"], "tweet": it["tweet"][:1000]} for it in items],
    }

    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert crypto trading signal classifier. "
                "You understand crypto Twitter slang, trading jargon, and can distinguish "
                "actionable trade signals from noise (promos, threads, engagement bait, news). "
                "Always return strict JSON. No markdown, no commentary."
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]

    raw, tin, tout = _llm_request(
        messages, max_tokens=min(800, 100 + 30 * len(items)), temperature=0.05
    )

    try:
        parsed = json.loads(raw)
        out: Dict[str, Dict[str, str]] = {}
        for rec in parsed.get("labels", []):
            _id = str(rec.get("id"))
            ticker = normalize_ticker(str(rec.get("ticker", "")))
            sentiment = str(rec.get("sentiment", "")).lower().strip()
            direction = str(rec.get("direction", "")).lower().strip()
            if sentiment not in ("bullish", "bearish", "neutral"):
                sentiment = "neutral"
            if direction not in ("long", "short"):
                direction = "long" if sentiment != "bearish" else "short"
            out[_id] = {"ticker": ticker, "sentiment": sentiment, "direction": direction}
        return out, tin, tout
    except Exception:
        print(f"[LLM] Parse error → fallback. Raw: {raw[:200]}")
        return (
            {it["id"]: {"ticker": "NOISE", "sentiment": "neutral", "direction": "long"} for it in items},
            tin, tout,
        )


# ═══════════════════════════════════════════════════════════════════════
#  X API v2 CLIENT
# ═══════════════════════════════════════════════════════════════════════

X_API_BASE = "https://api.twitter.com/2"


def _x_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {X_BEARER_TOKEN}", "User-Agent": "HyperCopy/1.0"}


def _x_get(
    url: str,
    params: Dict[str, Any] | None = None,
    retries: int = 5,
    base_delay: float = 2.0,
) -> Dict[str, Any]:
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_x_headers(), params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                reset_ts = r.headers.get("x-rate-limit-reset")
                wait = max(int(reset_ts) - int(time.time()), 1) + 2 if reset_ts else int(r.headers.get("Retry-After", 60))
                print(f"[X API] 429 rate-limited → sleeping {wait}s")
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                wait = base_delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"[X API] {r.status_code} → retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_err = e
            wait = base_delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"[X API] Request error ({attempt+1}/{retries}): {e} → {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"X API failed after {retries} retries: {last_err}")


# ═══════════════════════════════════════════════════════════════════════
#  USER PROFILE RESOLUTION
# ═══════════════════════════════════════════════════════════════════════

def _resolve_user_profile(
    username: str,
    state_con: sqlite3.Connection,
) -> Optional[Dict[str, Any]]:
    """
    Resolve username → full profile dict.
    Same API call as before, just added user.fields for avatar/bio/etc.
    """
    cached_uid = _state_get_user_id(state_con, username)

    url = f"{X_API_BASE}/users/by/username/{username}"
    params = {
        "user.fields": "profile_image_url,name,description,public_metrics,verified"
    }

    try:
        data = _x_get(url, params=params)
        user_data = data.get("data", {})
        uid = user_data.get("id")
        if not uid:
            print(f"  ✗ Could not resolve @{username}")
            return None

        avatar = user_data.get("profile_image_url", "")
        if avatar:
            avatar = avatar.replace("_normal.", "_400x400.")

        metrics = user_data.get("public_metrics", {})
        profile = {
            "user_id": uid,
            "display_name": user_data.get("name", ""),
            "avatar_url": avatar,
            "bio": user_data.get("description", ""),
            "is_verified": user_data.get("verified", False),
            "followers_count": metrics.get("followers_count", 0),
            "following_count": metrics.get("following_count", 0),
        }

        if not cached_uid:
            _state_save(state_con, username, uid)
            print(f"  ✓ Resolved @{username} → {uid} (cached)")
        else:
            print(f"  ✓ Profile fetched for @{username} (uid cached)")

        return profile

    except Exception as e:
        if cached_uid:
            print(f"  ⚠ Profile fetch failed for @{username}, using cached uid={cached_uid}")
            return {
                "user_id": cached_uid,
                "display_name": "", "avatar_url": "", "bio": "",
                "is_verified": False, "followers_count": 0, "following_count": 0,
            }
        print(f"  ✗ Could not resolve @{username}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
#  TWEET FETCHING
# ═══════════════════════════════════════════════════════════════════════

def _fetch_user_tweets(
    user_id: str,
    username: str,
    since_id: Optional[str] = None,
    max_days: int = 7,
    max_results_per_page: int = 100,
    max_pages: int = 10,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "max_results": max_results_per_page,
        "tweet.fields": "created_at,author_id,public_metrics,attachments",
        "expansions": "attachments.media_keys",
        "media.fields": "url,preview_image_url,type",
        "exclude": "retweets,replies",
    }

    if since_id:
        params["since_id"] = since_id
        print(f"  → incremental fetch (since_id={since_id})")
    else:
        start_time = (datetime.now(timezone.utc) - timedelta(days=max_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["start_time"] = start_time
        print(f"  → full fetch (last {max_days} days)")

    url = f"{X_API_BASE}/users/{user_id}/tweets"
    all_tweets: List[Dict[str, Any]] = []
    pages = 0

    while pages < max_pages:
        data = _x_get(url, params=params)
        meta = data.get("meta", {})
        if meta.get("result_count", 0) == 0:
            break

        media_map: Dict[str, str] = {}
        for m in data.get("includes", {}).get("media", []):
            k = m.get("media_key", "")
            img = m.get("url") or m.get("preview_image_url") or ""
            if k and img:
                media_map[k] = img

        for tw in data.get("data", []):
            text = tw.get("text", "").strip()
            if not text:
                continue
            created = tw.get("created_at", "")
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                dt = datetime.now(timezone.utc)

            imgs = [media_map[mk] for mk in tw.get("attachments", {}).get("media_keys", []) if mk in media_map]
            metrics = tw.get("public_metrics", {})

            all_tweets.append({
                "tweet_id": tw.get("id", ""),
                "text": text,
                "created_at": dt,
                "images": imgs,
                "likes": metrics.get("like_count", 0),
                "retweets": metrics.get("retweet_count", 0),
                "replies": metrics.get("reply_count", 0),
            })

        next_token = meta.get("next_token")
        if not next_token:
            break
        params["pagination_token"] = next_token
        pages += 1
        time.sleep(0.3)

    print(f"  ✓ {len(all_tweets)} tweets fetched for @{username} ({pages+1} pages)")
    return all_tweets


# ═══════════════════════════════════════════════════════════════════════
#  DATABASE WRITE
# ═══════════════════════════════════════════════════════════════════════

from backend.database import SessionLocal
from backend.models.trader import Trader
from backend.models.signal import Signal


def _get_or_create_trader(
    session,
    username: str,
    profile: Dict[str, Any] | None = None,
) -> Trader:
    """Get or create trader. Updates profile fields if fresh data available."""
    trader = session.query(Trader).filter(Trader.username == username).first()
    if not trader:
        trader = Trader(username=username)
        session.add(trader)
        session.flush()
        print(f"  + Created Trader: @{username} (id={trader.id})")

    if profile:
        changed = False
        for field in ("display_name", "avatar_url", "bio", "is_verified",
                      "followers_count", "following_count"):
            new_val = profile.get(field)
            if new_val is not None and new_val != "" and getattr(trader, field, None) != new_val:
                setattr(trader, field, new_val)
                changed = True
        if changed:
            trader.updated_at = datetime.now(timezone.utc)
            print(f"  ↻ Updated profile for @{username}")

    return trader


def _signal_exists(session, tweet_id: str) -> bool:
    if not tweet_id:
        return False
    return session.query(Signal.id).filter(Signal.tweet_id == tweet_id).first() is not None


# ═══════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════════

def run_once(max_days: int = 7, batch_size: int = 20, force_all: bool = False):
    """
    Full pipeline: fetch tweets → label → write to DB.
    Includes profile enrichment (avatar, bio) and tweet image capture.
    """
    users = _resolve_user_list()
    print(f"Users: {len(users)} | force_all={force_all}")
    print(f"LLM model: {LLM_MODEL}\n")

    state_con = _state_db_connect()

    # ── step 1: fetch tweets + profiles ─────────────────────────────
    all_user_tweets: List[Tuple[str, Dict[str, Any]]] = []
    user_profiles: Dict[str, Dict[str, Any]] = {}
    api_calls_saved = 0
    skipped_not_due = 0

    for i, username in enumerate(users):
        if not force_all and not _state_should_poll(state_con, username):
            skipped_not_due += 1
            continue

        print(f"\n{'='*50}")
        print(f"[{i+1}/{len(users)}] Fetching @{username}")
        print(f"{'='*50}")

        profile = _resolve_user_profile(username, state_con)
        if not profile:
            print(f"  ✗ Could not resolve @{username}, skipping")
            continue

        uid = profile["user_id"]
        user_profiles[username] = profile

        if _state_get_user_id(state_con, username):
            api_calls_saved += 1

        since_id = _state_get_since_id(state_con, username)
        tweets = _fetch_user_tweets(uid, username, since_id=since_id, max_days=max_days)

        newest_id = max(tweets, key=lambda t: t["tweet_id"])["tweet_id"] if tweets else None
        _state_save(state_con, username, uid,
                    last_tweet_id=newest_id,
                    tweets_found=len(tweets))

        for tw in tweets:
            all_user_tweets.append((username, tw))

        if i < len(users) - 1:
            time.sleep(1)

    print(f"\n{'='*50}")
    print(f"Total tweets fetched: {len(all_user_tweets)}")
    print(f"Profiles fetched: {len(user_profiles)}")
    print(f"Users skipped (not due): {skipped_not_due}/{len(users)}")
    print(f"API calls saved (cached user_ids): {api_calls_saved}")
    print(f"{'='*50}")

    if not all_user_tweets:
        print("No new tweets to process.")
        return

    # ── step 2: label ─────────────────────────────────────────────
    cache_con = _label_cache_connect()
    labeled: List[Dict[str, Any]] = []
    llm_queue: List[Dict[str, Any]] = []
    cache_hits = 0

    for idx, (username, tw) in enumerate(all_user_tweets):
        text = tw["text"]
        thash = _stable_tweet_hash(text)

        cached = _label_cache_get(cache_con, thash)
        if cached:
            cache_hits += 1
            labeled.append({**tw, "username": username, "ticker": cached[0],
                            "sentiment": cached[1], "direction": cached[2]})
            continue

        tk = _cheap_ticker(text)
        st = _cheap_sentiment(text)
        if tk and st:
            dr = _sentiment_to_direction(st, text)
            _label_cache_put(cache_con, thash, tk, st, dr)
            labeled.append({**tw, "username": username, "ticker": tk,
                            "sentiment": st, "direction": dr})
        else:
            llm_queue.append({"idx": idx, "id": str(idx), "tweet": text,
                              "username": username, "tw": tw})

    tokens_in_total = tokens_out_total = batches = 0
    for start in range(0, len(llm_queue), batch_size):
        chunk = llm_queue[start : start + batch_size]
        labels, tin, tout = llm_batch_label(chunk)
        tokens_in_total += tin
        tokens_out_total += tout
        batches += 1

        for item in chunk:
            res = labels.get(item["id"], {"ticker": "NOISE", "sentiment": "neutral", "direction": "long"})
            text = item["tweet"]
            thash = _stable_tweet_hash(text)
            _label_cache_put(cache_con, thash, res["ticker"], res["sentiment"], res["direction"])
            labeled.append({**item["tw"], "username": item["username"],
                            "ticker": res["ticker"], "sentiment": res["sentiment"],
                            "direction": res["direction"]})

        print(f"[Label] {start+len(chunk)}/{len(llm_queue)} done (tokens: {tin}→{tout})")

    relevant = [r for r in labeled if r["ticker"] not in ("NOISE",)]
    noise_count = len(labeled) - len(relevant)

    print(f"\nLabeling done: {len(relevant)} relevant, {noise_count} noise")
    print(f"  cache_hits={cache_hits}, llm_items={len(llm_queue)}, batches={batches}")
    print(f"  tokens_in={tokens_in_total}, tokens_out={tokens_out_total}")

    # ── step 3: write to DB ───────────────────────────────────────
    session = SessionLocal()
    inserted = skipped_dup = 0

    try:
        for r in relevant:
            tweet_id = r.get("tweet_id", "")
            if tweet_id and _signal_exists(session, tweet_id):
                skipped_dup += 1
                continue

            trader = _get_or_create_trader(
                session, r["username"],
                profile=user_profiles.get(r["username"]),
            )

            imgs = r.get("images", [])
            signal = Signal(
                trader_id=trader.id,
                tweet_id=tweet_id or None,
                tweet_text=r["text"],
                ticker=r["ticker"],
                direction=r["direction"],
                sentiment=r["sentiment"],
                likes=r.get("likes", 0),
                retweets=r.get("retweets", 0),
                replies=r.get("replies", 0),
                tweet_image_url=imgs[0] if imgs else None,
                tweet_time=r["created_at"],
                status="active",
            )
            session.add(signal)
            inserted += 1

        session.commit()
        print(f"\n✅ DB write complete: {inserted} signals, {skipped_dup} dupes, {len(user_profiles)} profiles updated")

    except Exception as e:
        session.rollback()
        print(f"\n❌ DB error: {e}")
        raise
    finally:
        session.close()

    # ── telemetry ─────────────────────────────────────────────────
    if relevant:
        from collections import Counter
        tickers = Counter(r["ticker"] for r in relevant)
        sents = Counter(r["sentiment"] for r in relevant)
        dirs = Counter(r["direction"] for r in relevant)

        print("\nTicker distribution (top 10):")
        for t, c in tickers.most_common(10):
            print(f"  {t}: {c}")
        print("Sentiment distribution:")
        for s, c in sents.most_common():
            print(f"  {s}: {c} ({100*c/len(relevant):.1f}%)")
        print("Direction distribution:")
        for d, c in dirs.most_common():
            print(f"  {d}: {c} ({100*c/len(relevant):.1f}%)")


# ═══════════════════════════════════════════════════════════════════════
#  USER LIST
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_USERS = [
    # ── Tier 1: Elite Signal Traders (widely referenced, 50k+) ──
    "DonAlt","CryptoCred","HsakaTrades","Tradermayne","RookieXBT",
    "DegenSpartan","ColdBloodShill","pierre_crypt0","ByzGeneral",
    "egirl_capital","TheFlowHorse","Bluntz_Capital","AltcoinSherpa",
    "SmartContracter","GCRClassic",
    # ── Tier 2: Top Traders (active signals, 20k+) ──
    "BigCheds","Ninjascalp","abetrade","tradingriot","AltcoinPsycho",
    "Pentosh1","CryptoKaleo","gainzy222","inversebrah","Tree_of_Alpha",
    "tradingstable","Nebraskangooner","satsdart","jimtalbot","Citrini7",
    "jukan05","CL207","ledgerstatus","MuroCrypto","RektProof","DaanCrypto",
    "CryptoJelleNL","NukeCapital","TraderMercury","RealCryptoFace",
    # ── Tier 3: Daily Signal Posters (10k+, frequent entries) ──
    "trader1sz","Trader_XO","IncomeSharks","CryptoTony__","CryptoGodJohn",
    "EmperorBTC","PeterLBrandt","TrueCrypto28","MoonOverlord","TheCryptoDog",
    "bitcoinjack","insomniacxbt","Tom__Capital","flopxbt","docXBT",
    "Danny_Crypton","dailytradr","CryptoWizardd","trader_koala","LomahCrypto",
    "CredibleCrypto","galaxyBTC","CryptoCaesarTA","Crypto_Chase","cryptogoos",
    "ThinkingUSD","PriorXBT","yourQuantGuy","0xThoor","coinmamba",
    "CryptoPoseidonn","papagiorgioXBT","PillageCapital","macklorden",
    "kaceohhh","PhoenixBtcFire",
    # ── Quant / Chart-based Traders ──
    "QuantMeta","TechCharts","Quantzilla34","alphawhaletrade",
    "ConnorJBates_","LightCrypto",
    # ── Hyperliquid Ecosystem ──
    "MomentumKevin","TheWhiteWhaleHL","JamesWynnReal","KeyboardMonkey3",
    "lBattleRhino","karbonbased","Rijk__","HYPEconomist","MizerXBT",
    # ── On-chain Actionable (whale alerts, breaking data) ──
    "lookonchain","OnChainWizard","whale_alert",
    "spot_on_chain","Arkham","tier10k","smartestmoney","DefiIgnas",
    # ── Meme / Degen Signal Hunters ──
    "MustStopMurad","blknoiz06","GiganticRebirth","CryptoAnup","TedPillows",
    "izebel_eth","defi_mochi",
    # ── Active Traders with Macro Edge (still post entries) ──
    "CryptoHayes","arthur0x","Rewkang","QwQiao",
    "Darrenlautf","ThinkingBitmex","TheBootMex","BastilleBtc",
    "ColeGarnersTake","R89Capital","JustinCBram","MissionGains",
    "dennis_qian","noBScrypto","Numb3rsguy_","EtherWizz_","TimeFreedomROB",
    "GarrettBullish",
    # ── Established Signal Accounts ──
    "rektcapital","AltCryptoGems","alicharts","CastilloTrading",
    "AltcoinDaily","TheLongInvest","CryptoBusy",
    "SolidTradesz","ZordXBT","kropts","breyonchain",
]


def _resolve_user_list() -> List[str]:
    if SCRAPE_USERS_ENV:
        parts = [p.strip() for p in SCRAPE_USERS_ENV.split(",") if p.strip()]
        if parts:
            return parts
    return DEFAULT_USERS


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-days", type=int, default=7,
                        help="How far back to look on first run (default 7)")
    parser.add_argument("--force-all", action="store_true",
                        help="Ignore adaptive schedule, poll everyone")
    args = parser.parse_args()
    run_once(max_days=args.max_days, force_all=args.force_all)