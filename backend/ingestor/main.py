"""
HyperCopy Ingestor — production-grade continuous service.

Key design principles:
  • Per-user atomic processing: fetch → label → DB write per user.
    One user's failure never loses another user's data.
  • Immediate DB commits: every signal is persisted the moment it's labeled.
    No "accumulate everything then commit" anti-pattern.
  • Graceful shutdown: SIGTERM/SIGINT set a flag, current user finishes, then exit.
  • Exponential backoff on transient errors, circuit-breaker on persistent ones.
  • Label cache ensures we never pay OpenAI twice for the same tweet text.
  • Token whitelist fetched from HyperLiquid meta — only tradeable tokens pass.
  • ★ Confidence-gated signals: LLM must output confidence ≥ 60 AND is_signal=true.
  • ★ neutral sentiment is never stored — only bullish/bearish with conviction.
  • ★ Heuristic only fires on EXPLICIT trade language (entries, stops, targets).
"""
from __future__ import annotations
import os, re, time, json, hashlib, random, sqlite3, signal, sys
import logging, requests, threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict, Any
from contextlib import contextmanager

import openai
from dotenv import load_dotenv
load_dotenv()

# ── logging ────────────────────────────────────────────────────────────
log = logging.getLogger("ingestor")
log.setLevel(logging.INFO)
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(h)

def _env(k: str, d: str = "") -> str:
    return os.environ.get(k, d)

# ── paths & secrets ────────────────────────────────────────────────────
DATA_DIR = _env("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
LABEL_CACHE_PATH = os.path.join(DATA_DIR, "label_cache.sqlite")
STATE_DB_PATH    = os.path.join(DATA_DIR, "ingestor_state.sqlite")

OPENAI_API_KEY = _env("OPENAI_API_KEY")
X_BEARER_TOKEN = _env("X_BEARER_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not X_BEARER_TOKEN:
    raise RuntimeError("Missing X_BEARER_TOKEN")
openai.api_key = OPENAI_API_KEY

SCRAPE_USERS_ENV = _env("SCRAPE_USERS", "")
LLM_MODEL      = _env("LLM_MODEL", "gpt-4o-mini")
VISION_MODEL   = _env("VISION_MODEL", "gpt-4o-mini")
VISION_ENABLED = _env("VISION_ENABLED", "true").lower() in ("1", "true", "yes")

CYCLE_INTERVAL_S   = int(_env("CYCLE_INTERVAL_S", "300"))
MAX_CONSECUTIVE_FAILURES = int(_env("MAX_CONSECUTIVE_FAILURES", "10"))

HL_BASE_URL = _env("HL_BASE_URL", "https://api.hyperliquid.xyz")

# ★ Signal quality gate
CONFIDENCE_THRESHOLD = int(_env("CONFIDENCE_THRESHOLD", "60"))

# ── regex / constants ──────────────────────────────────────────────────
ALNUM_RE         = re.compile(r"[^A-Z0-9]")
DOLLAR_TICKER_RE = re.compile(r"\$([A-Za-z0-9]{2,15})\b")
HASH_TICKER_RE   = re.compile(r"#([A-Za-z0-9]{2,15})\b")
PLAIN_TICKER_RE  = re.compile(r"\b([A-Z]{2,10})\b")


# ═══════════════════════════════════════════════════════════════════════
#  HYPERLIQUID TOKEN WHITELIST (the source of truth)
# ═══════════════════════════════════════════════════════════════════════

_hl_token_cache: Dict[str, Any] = {"tokens": set(), "fetched_at": 0.0}
HL_TOKEN_CACHE_TTL = 3600  # refresh every hour

def _fetch_hl_tokens() -> set[str]:
    """Fetch all tradeable token symbols from HyperLiquid meta endpoint."""
    tokens: set[str] = set()
    try:
        r = requests.post(f"{HL_BASE_URL}/info", json={"type": "meta"}, timeout=15)
        r.raise_for_status()
        meta = r.json()
        for asset in meta.get("universe", []):
            name = asset.get("name", "").upper().strip()
            if name:
                tokens.add(name)
        try:
            r2 = requests.post(f"{HL_BASE_URL}/info", json={"type": "spotMeta"}, timeout=15)
            r2.raise_for_status()
            spot = r2.json()
            for tok in spot.get("tokens", []):
                name = tok.get("name", "").upper().strip()
                if name:
                    tokens.add(name)
        except Exception as e:
            log.warning(f"spotMeta fetch failed (non-fatal): {e}")
        log.info(f"📋 Fetched {len(tokens)} tradeable tokens from HL: {sorted(list(tokens))[:20]}…")
    except Exception as e:
        log.error(f"Failed to fetch HL tokens: {e}")
    return tokens

def get_hl_tokens() -> set[str]:
    now = time.monotonic()
    if now - _hl_token_cache["fetched_at"] > HL_TOKEN_CACHE_TTL or not _hl_token_cache["tokens"]:
        fetched = _fetch_hl_tokens()
        if fetched:
            _hl_token_cache["tokens"] = fetched
            _hl_token_cache["fetched_at"] = now
        elif not _hl_token_cache["tokens"]:
            log.warning("Using COMMON_CRYPTO fallback — HL unreachable")
            _hl_token_cache["tokens"] = COMMON_CRYPTO_FALLBACK
            _hl_token_cache["fetched_at"] = now - HL_TOKEN_CACHE_TTL + 120
    return _hl_token_cache["tokens"]

COMMON_CRYPTO_FALLBACK = {
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
    "ORDI","RUNE","SATS",
    "PYTH","KAS","JTO","NOT","HYPE",
    "FTM","ROSE","AR","MINA","HNT","CHZ",
}


# ═══════════════════════════════════════════════════════════════════════
#  PHRASE LISTS — tightened for precision
# ═══════════════════════════════════════════════════════════════════════

# ★ EXPLICIT TRADE LANGUAGE — only these trigger the heuristic bypass.
# These indicate the author IS TAKING or HAS TAKEN a position.
EXPLICIT_TRADE_PHRASES = [
    # Position entries
    "longed", "longing", "going long", "opened a long", "opening long",
    "entered long", "long position", "long here", "long from", "long entry",
    "shorted", "shorting", "going short", "opened a short", "opening short",
    "entered short", "short position", "short here", "short from", "short entry",
    # Trade management
    "tp hit", "sl hit", "stop loss at", "take profit at", "target hit",
    "closed my", "closing my", "exited my", "taking profit",
    "entry at", "entry price", "entered at", "got in at",
    # Explicit buy/sell with conviction
    "loaded up", "loading up", "added more", "adding more",
    "accumulated", "accumulating heavy",
    "buying the dip", "btfd", "dip buy",
    "sold my", "selling my", "fading this",
]

# Broader sentiment words — only used BY LLM, not for heuristic bypass
POS_PHRASES = [
    "longed","longing","going long","opened a long","opening long",
    "entered long","long position","long here","long from",
    "buying","bought","loaded","loading up","adding","added more",
    "accumulating","accumulated","bid","bidding",
    "bullish","bull case","bull flag","bull run",
    "breakout","breaking out","broke out",
    "pumping","ripping","mooning",
    "rally","rallying","surging",
    "bounce","bouncing","recovery",
    "bottomed","bottom is in","dip buy","buying the dip","btfd",
    "undervalued",
    "🚀","📈","🟢",
]
NEG_PHRASES = [
    "shorted","shorting","going short","opened a short","opening short",
    "entered short","short position","short here","short from",
    "selling","sold","exited","closed my","taking profit","tp hit",
    "fading","faded","cutting",
    "bearish","bear case","bear flag",
    "breakdown","breaking down","broke down",
    "dumping","crashed","nuked","tanking","drilling",
    "rejection","rejected",
    "topped","top signal","top is in",
    "overvalued","dead cat","bull trap",
    "rekt","liquidated",
    "rug","rugged","scam",
    "📉","💀",
]
FALSE_POS_PATTERNS = [
    "short term","short-term","short squeeze","short covering",
    "in short","short summary","shorts getting","shorts are",
    "long term","long-term","long way","long time","long run",
    "as long as","so long","how long","before long","long story",
    "not long","no longer",
]
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
    "POL":"MATIC","CELESTIA":"TIA","STARKNET":"STRK",
    "BCC":"BCH","BCHABC":"BCH","BCHSV":"BSV",
    "RIPPLE":"XRP","XRP-USD":"XRP",
    "XDG":"DOGE","DOG":"DOGE","DOGECOIN":"DOGE",
    "SHIBAINU":"SHIB","SHIBA":"SHIB",
    "TETHER":"USDT","USDCOIN":"USDC",
    "LNK":"LINK","CHAINLINK":"LINK",
    "TRON":"TRX","TRX-USD":"TRX",
    "POLKADOT":"DOT","DOT-USD":"DOT",
    "COSMOS":"ATOM","RENDER":"RNDR","WORLDCOIN":"WLD",
    "FANTOM":"FTM","SONIC":"FTM",
    "IOTA-USD":"IOTA","MIOTA":"IOTA",
    "NAN0":"NANO","BTTOLD":"BTT","BTTNEW":"BTT",
    "OCEAN":"FET","AGIX":"FET",
    "ONDO-PERP":"ONDO","PEPE-PERP":"PEPE","DOGE-PERP":"DOGE",
    "ARB-PERP":"ARB","SUI-PERP":"SUI","APT-PERP":"APT",
    "AVAX-PERP":"AVAX","LINK-PERP":"LINK","INJ-PERP":"INJ",
    "GOLD":"GLD","XAUUSD":"GLD","XAU":"GLD",
    "SILVER":"SLV","XAGUSD":"SLV","XAG":"SLV",
    "GOOG":"GOOGL","GOOGLE":"GOOGL",
    "APPLE":"AAPL","AMAZON":"AMZN","TESLA":"TSLA",
    "MICROSOFT":"MSFT","NVIDIA":"NVDA","ORACLE":"ORCL",
    "SP500":"SPY","SNP500":"SPY","NASDAQ100":"QQQ","NDX":"QQQ",
    "EURUSD":"EUR",
}

STRICT_CANON = True

TICKER_BLACKLIST = {
    "THE","AND","FOR","ARE","BUT","NOT","YOU","ALL","CAN","HER","WAS","ONE",
    "OUR","OUT","HAS","HIS","HOW","ITS","MAY","NEW","NOW","OLD","SEE","WAY",
    "WHO","DID","GET","HIM","LET","SAY","SHE","TOO","USE","DAD","MOM","USA",
    "CEO","CTO","COO","CFO","IMO","TBH","LMAO","HODL","NGMI","WAGMI","DYOR",
    "NFA","PSA","FYI","ATH","ATL","API","ETF","SEC","USD","JPY","GBP",
    "IPO","ICO","IDO","DEX","CEX","TVL","APR","APY","ROI","PNL","OTC",
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
    "USDT","USDC","BUSD","DAI","TUSD","USDP","GUSD","FRAX","LUSD",
    "PYUSD","FDUSD","CUSD","EUSD","SUSD","MIM","UST","USTC",
    "USDD","CRVUSD","DOLA","ALUSD","EURT","EURS",
}

# ── Noise tweet patterns ──
NOISE_PATTERNS = [
    re.compile(r"(?:transferred|transfer)\s+(?:from|to)\s+(?:unknown|#?\w+)\s+wallet", re.I),
    re.compile(r"\d[\d,]*\s+#?(?:USDC|USDT|BTC|ETH)\s+\(\$?[\d,]+", re.I),
    re.compile(r"(?:minted|burned|mint|burn)\s+(?:at|from|to)\s+", re.I),
    re.compile(r"(?:circle|tether)\s+(?:minted|burned|treasury)", re.I),
    re.compile(r"🚨\s*(?:\d[\d,.]*\s*)+#?\w+\s*(?:\(|\$)", re.I),
    re.compile(r"(?:whale|alert|tracker).*?(?:moved|deposited|withdrew|transferred)", re.I),
    re.compile(r"(?:deposited|withdrew)\s+(?:into|from|to)\s+(?:binance|coinbase|okx|kraken|bybit|bitfinex)", re.I),
    re.compile(r"(?:giveaway|giving away|airdrop).*?(?:like|rt|retweet|follow)", re.I),
    re.compile(r"(?:like|rt|retweet)\s+(?:and|&|to)\s+(?:follow|win|enter)", re.I),
]

ALERT_BOT_USERNAMES = {
    "whale_alert", "lookonchain", "spot_on_chain", "Arkham", "tier10k",
    "smartestmoney", "OnChainWizard",
}


# ═══════════════════════════════════════════════════════════════════════
#  ★ FEW-SHOT EXAMPLES — expanded with common false positives
# ═══════════════════════════════════════════════════════════════════════

LLM_FEW_SHOT_EXAMPLES = [
    # ── TRUE SIGNALS: author is taking/recommending a position ──
    {"tweet": "$BTC looking strong here, longed at 67.5k. TP 72k, SL 65k 🚀",
     "label": {"is_signal": True, "ticker": "BTC", "sentiment": "bullish", "direction": "long", "confidence": 95}},
    {"tweet": "Shorted $ETH at 2450. This is going to 2200. Bear flag on 4H.",
     "label": {"is_signal": True, "ticker": "ETH", "sentiment": "bearish", "direction": "short", "confidence": 95}},
    {"tweet": "$SOL breaking out while $BTC chops. Longed SOL at $98, this is going to $120+",
     "label": {"is_signal": True, "ticker": "SOL", "sentiment": "bullish", "direction": "long", "confidence": 90}},
    {"tweet": "$DOGE chart looks terrible. Shorting from 0.15, expecting dump to 0.12.",
     "label": {"is_signal": True, "ticker": "DOGE", "sentiment": "bearish", "direction": "short", "confidence": 90}},
    {"tweet": "$BTC short squeeze incoming. Funding is super negative, shorts gonna get rekt 🔥",
     "label": {"is_signal": True, "ticker": "BTC", "sentiment": "bullish", "direction": "long", "confidence": 75}},
    {"tweet": "$HYPE chart looking exactly like $SOL did before its run. Accumulating heavy.",
     "label": {"is_signal": True, "ticker": "HYPE", "sentiment": "bullish", "direction": "long", "confidence": 85}},
    {"tweet": "$ETH needs to hold $2000 or we're going much lower. Bearish below that level.",
     "label": {"is_signal": True, "ticker": "ETH", "sentiment": "bearish", "direction": "short", "confidence": 70}},
    {"tweet": "Loading $INJ here. This is one of the most undervalued L1s. Target $50+.",
     "label": {"is_signal": True, "ticker": "INJ", "sentiment": "bullish", "direction": "long", "confidence": 85}},

    # ── NOISE: discussion, news, questions, memes, alerts ──
    {"tweet": "GM CT! What a wild week. Markets are crazy right now. Stay safe out there.",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "🧵 Thread: Top 10 altcoins for 2025. Like and RT if you want me to cover more!",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "$BTC whale just moved 5000 BTC to Binance. Watch closely.",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "Just hit 100k followers! Thank you fam 🙏 Giveaway coming soon...",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "🚨 300,000,000 #USDC (300,110,196 USD) transferred from unknown wallet to unknown wallet",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "ALERT: CIRCLE MINTED $500M USDC https://t.co/xyz",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "A whale deposited 5,000 $ETH ($10.5M) into Binance 2 hours ago",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},

    # ★ NEW: common false positives that MUST be NOISE
    {"tweet": "guys you can trade oil on hype. not sure why that is going down",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "What do you think about $ETH? Is it a good buy here or should I wait?",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "Ethcoin. Basically free money. Go mine it. https://t.co/xxx",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "Even Zoomer can't resist those polymarket bucks https://t.co/xxx",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "$ETH is in constant overdrive mode. Only people do not comprehend it.",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 10}},
    {"tweet": "Remember when they told you Bitcoin is Digital gold and ETH is digital oil?",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 5}},
    {"tweet": "yeah literally, you AI agent can now trade perps on a DEX while you sleep.",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "This is where I bought my first ETH back in 2017. 17 square meter in Paris.",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "ETH isn't trading like digital oil at all",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 5}},
    {"tweet": "Reports like this only strengthen my ultra bullish ETH thesis. Because they're without any real substance...",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 20}},
    {"tweet": "AI can compress the Ethereum straw man roadmap from Completion by 2029 to 1 year if we push hard",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "Broskis. As a matter of fact. 25k ETH is still the basecase.",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 25}},
    {"tweet": "Congrats to everyone who bought $SOL under $10. What a ride.",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 5}},
    {"tweet": "I'm not trading this chop. Sitting on my hands until we get clarity.",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 0}},
    {"tweet": "$BTC dominance rising. Alts getting crushed. Be careful out there.",
     "label": {"is_signal": False, "ticker": "NOISE", "sentiment": "neutral", "direction": "long", "confidence": 15}},
    {"tweet": "Seems that $HYPE is preparing for a correction. Price action started to slow down and is locally breaking down.",
     "label": {"is_signal": True, "ticker": "HYPE", "sentiment": "bearish", "direction": "short", "confidence": 70}},
]


# ═══════════════════════════════════════════════════════════════════════
#  GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════

class ShutdownRequested(Exception):
    pass

_shutdown = threading.Event()

def _handle_signal(signum, frame):
    sig_name = signal.Signals(signum).name
    log.info(f"Received {sig_name} — finishing current user then shutting down…")
    _shutdown.set()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

def _check_shutdown():
    if _shutdown.is_set():
        raise ShutdownRequested()


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
    if s in TICKER_BLACKLIST:
        return "NOISE"
    if STRICT_CANON:
        hl_tokens = get_hl_tokens()
        if hl_tokens and s not in hl_tokens:
            return "NOISE"
    return s


# ═══════════════════════════════════════════════════════════════════════
#  NOISE DETECTION — pre-LLM fast filter
# ═══════════════════════════════════════════════════════════════════════

def _is_noise_tweet(text: str, username: str = "") -> bool:
    """Fast check if a tweet is obviously noise before spending LLM tokens."""
    if not text:
        return True

    # Alert bot accounts
    if username.lower() in {u.lower() for u in ALERT_BOT_USERNAMES}:
        t_lower = text.lower()
        has_position = any(p in t_lower for p in [
            "longed", "shorted", "buying", "selling", "entry", "tp ",
            "sl ", "target", "stop loss", "take profit", "opened a",
        ])
        if not has_position:
            return True

    # Regex noise patterns
    for pattern in NOISE_PATTERNS:
        if pattern.search(text):
            return True

    return False


# ═══════════════════════════════════════════════════════════════════════
#  INGESTOR STATE DB
# ═══════════════════════════════════════════════════════════════════════

def _state_db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(STATE_DB_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            username         TEXT PRIMARY KEY,
            user_id          TEXT NOT NULL,
            last_tweet_id    TEXT,
            avg_tweets_per_day REAL NOT NULL DEFAULT 0,
            empty_polls      INTEGER NOT NULL DEFAULT 0,
            poll_interval_h  REAL NOT NULL DEFAULT 2.0,
            last_polled_at   TEXT,
            last_profile_at  TEXT,
            consecutive_errors INTEGER NOT NULL DEFAULT 0,
            updated_at       TEXT NOT NULL
        )
    """)
    for col, defn in [
        ("avg_tweets_per_day",  "REAL NOT NULL DEFAULT 0"),
        ("empty_polls",         "INTEGER NOT NULL DEFAULT 0"),
        ("poll_interval_h",     "REAL NOT NULL DEFAULT 2.0"),
        ("last_polled_at",      "TEXT"),
        ("last_profile_at",     "TEXT"),
        ("consecutive_errors",  "INTEGER NOT NULL DEFAULT 0"),
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
PROFILE_REFRESH_DAYS = 7
ERROR_BACKOFF_H     = 6.0
MAX_USER_ERRORS     = 5

def _state_get_user_id(con, username):
    row = con.execute("SELECT user_id FROM user_state WHERE username=?", (username,)).fetchone()
    return row[0] if row else None

def _state_get_since_id(con, username):
    row = con.execute("SELECT last_tweet_id FROM user_state WHERE username=?", (username,)).fetchone()
    return row[0] if row else None

def _state_should_poll(con, username) -> Tuple[bool, str]:
    row = con.execute(
        "SELECT last_polled_at, poll_interval_h, consecutive_errors FROM user_state WHERE username=?",
        (username,),
    ).fetchone()
    if not row or not row[0]:
        return True, "never_polled"
    errs = row[2] or 0
    if errs >= MAX_USER_ERRORS:
        return False, f"circuit_open({errs}_errors)"
    interval = row[1] or 2.0
    if errs > 0:
        interval = max(interval, ERROR_BACKOFF_H)
    last = datetime.fromisoformat(row[0])
    due = last + timedelta(hours=interval)
    if datetime.now(timezone.utc) >= due:
        return True, "due"
    return False, "not_due"

def _state_needs_profile_refresh(con, username) -> bool:
    row = con.execute("SELECT last_profile_at FROM user_state WHERE username=?", (username,)).fetchone()
    if not row or not row[0]:
        return True
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) > timedelta(days=PROFILE_REFRESH_DAYS)
    except Exception:
        return True

def _state_update_profile_time(con, username):
    con.execute("UPDATE user_state SET last_profile_at=? WHERE username=?",
                (datetime.now(timezone.utc).isoformat(), username))
    con.commit()

def _state_record_error(con, username, user_id):
    now = datetime.now(timezone.utc).isoformat()
    existing = con.execute("SELECT 1 FROM user_state WHERE username=?", (username,)).fetchone()
    if existing:
        con.execute(
            "UPDATE user_state SET consecutive_errors=consecutive_errors+1, last_polled_at=?, updated_at=? WHERE username=?",
            (now, now, username),
        )
    else:
        con.execute(
            "INSERT INTO user_state (username, user_id, consecutive_errors, last_polled_at, updated_at) VALUES (?,?,1,?,?)",
            (username, user_id or "", now, now),
        )
    con.commit()

def _state_save(con, username, user_id, last_tweet_id=None, tweets_found=0):
    now = datetime.now(timezone.utc).isoformat()
    existing = con.execute(
        "SELECT last_tweet_id, avg_tweets_per_day, empty_polls, poll_interval_h "
        "FROM user_state WHERE username=?", (username,)
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
            if new_avg > 5:    target = MIN_POLL_INTERVAL_H
            elif new_avg > 2:  target = 2.0
            elif new_avg > 0.5: target = 4.0
            else:              target = 8.0
            new_interval = max(old_interval * SPEEDUP_FACTOR, target, MIN_POLL_INTERVAL_H)
        con.execute(
            """UPDATE user_state SET user_id=?, last_tweet_id=COALESCE(?,last_tweet_id),
               avg_tweets_per_day=?, empty_polls=?, poll_interval_h=?,
               last_polled_at=?, consecutive_errors=0, updated_at=?
               WHERE username=?""",
            (user_id, last_tweet_id, round(new_avg, 2), new_empty,
             round(new_interval, 1), now, now, username),
        )
    else:
        con.execute(
            """INSERT INTO user_state
               (username, user_id, last_tweet_id, avg_tweets_per_day,
                empty_polls, poll_interval_h, last_polled_at, consecutive_errors, updated_at)
               VALUES (?,?,?,?,?,?,?,0,?)""",
            (username, user_id, last_tweet_id, 0.0, 0, 2.0, now, now),
        )
    con.commit()


# ═══════════════════════════════════════════════════════════════════════
#  LABEL CACHE — ★ now stores confidence + is_signal
# ═══════════════════════════════════════════════════════════════════════

def _label_cache_connect() -> sqlite3.Connection:
    con = sqlite3.connect(LABEL_CACHE_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS label_cache (
            tweet_hash TEXT PRIMARY KEY,
            ticker     TEXT NOT NULL,
            sentiment  TEXT NOT NULL,
            direction  TEXT NOT NULL DEFAULT 'long',
            confidence INTEGER NOT NULL DEFAULT 50,
            is_signal  INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    for col, defn in [
        ("direction",  "TEXT NOT NULL DEFAULT 'long'"),
        ("confidence", "INTEGER NOT NULL DEFAULT 50"),
        ("is_signal",  "INTEGER NOT NULL DEFAULT 1"),
    ]:
        try:
            con.execute(f"ALTER TABLE label_cache ADD COLUMN {col} {defn}")
        except Exception:
            pass
    return con

def _label_cache_get(con, h):
    row = con.execute(
        "SELECT ticker, sentiment, direction, confidence, is_signal FROM label_cache WHERE tweet_hash=?",
        (h,),
    ).fetchone()
    if row:
        return {"ticker": row[0], "sentiment": row[1], "direction": row[2],
                "confidence": row[3], "is_signal": bool(row[4])}
    return None

def _label_cache_put(con, h, ticker, sentiment, direction, confidence=50, is_signal=True):
    con.execute(
        "INSERT OR REPLACE INTO label_cache (tweet_hash,ticker,sentiment,direction,confidence,is_signal,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (h, ticker, sentiment, direction, confidence, int(is_signal),
         datetime.now(timezone.utc).isoformat()),
    )
    con.commit()

def _stable_tweet_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════════
#  ★ HEURISTIC LABELING — much stricter, requires explicit trade language
# ═══════════════════════════════════════════════════════════════════════

def _cheap_ticker(text):
    if not text:
        return None
    hl_tokens = get_hl_tokens()

    dollar_tickers = []
    for m in DOLLAR_TICKER_RE.finditer(text):
        sym = normalize_ticker(m.group(1))
        if sym != "NOISE" and sym not in TICKER_BLACKLIST:
            dollar_tickers.append((sym, m.start()))
    if dollar_tickers:
        if len(dollar_tickers) == 1:
            return dollar_tickers[0][0]
        t_lower = text.lower()
        ticker_counts: Dict[str, int] = {}
        for sym, _ in dollar_tickers:
            ticker_counts[sym] = ticker_counts.get(sym, 0) + 1
        best_sym, best_score = None, -1
        for sym, pos in dollar_tickers:
            score = ticker_counts[sym] * 2
            ctx_start = max(0, pos - 40)
            ctx_end = min(len(text), pos + 80)
            ctx = text[ctx_start:ctx_end].lower()
            for w in ["long","short","buy","sell","entry","target","tp","sl","stop",
                       "loaded","shorted","longed","bullish","bearish","breakout"]:
                if w in ctx:
                    score += 3
            if pos == dollar_tickers[0][1]:
                score += 1
            if score > best_score:
                best_score = score
                best_sym = sym
        return best_sym

    for m in HASH_TICKER_RE.finditer(text):
        sym = normalize_ticker(m.group(1))
        if sym != "NOISE" and sym in hl_tokens:
            return sym

    candidates = set()
    for c in PLAIN_TICKER_RE.findall(text.upper()):
        sym = normalize_ticker(c)
        if sym != "NOISE" and sym in hl_tokens and sym not in TICKER_BLACKLIST:
            candidates.add(sym)
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _has_explicit_trade_language(text: str) -> bool:
    """★ Returns True only if the tweet contains EXPLICIT trade entry/exit language.
    This is the gate for heuristic-only labeling (bypassing LLM)."""
    t = text.lower()
    # Check false positives first
    for fp in FALSE_POS_PATTERNS:
        t = t.replace(fp, "")
    return any(phrase in t for phrase in EXPLICIT_TRADE_PHRASES)


def _cheap_sentiment(text):
    """Only used when _has_explicit_trade_language is True."""
    if not text:
        return None
    t = text.lower()
    pos_score = neg_score = 0
    for phrase in POS_PHRASES:
        if phrase in t:
            if not any(fp in t and phrase in fp for fp in FALSE_POS_PATTERNS):
                pos_score += len(phrase.split())
    for phrase in NEG_PHRASES:
        if phrase in t:
            if not any(fp in t and phrase in fp for fp in FALSE_POS_PATTERNS):
                neg_score += len(phrase.split())
    if pos_score >= 2 and pos_score > neg_score * 1.5:
        return "bullish"
    if neg_score >= 2 and neg_score > pos_score * 1.5:
        return "bearish"
    return None

def _sentiment_to_direction(sentiment, text):
    t = text.lower()
    long_score  = sum(1 for p in LONG_DIRECTION_PHRASES if p in t)
    short_score = sum(1 for p in SHORT_DIRECTION_PHRASES if p in t)
    if long_score > short_score:  return "long"
    if short_score > long_score:  return "short"
    return "short" if sentiment == "bearish" else "long"


# ═══════════════════════════════════════════════════════════════════════
#  ★ OPENAI LLM — with confidence + is_signal
# ═══════════════════════════════════════════════════════════════════════

def _llm_request(messages, max_tokens, temperature, model=None, retries=4, base_delay=1.0):
    last_err = None
    use_model = model or LLM_MODEL
    for attempt in range(retries):
        _check_shutdown()
        try:
            resp = openai.chat.completions.create(
                model=use_model, messages=messages,
                max_tokens=max_tokens, temperature=temperature,
                response_format={"type": "json_object"},
            )
            content = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            tin  = getattr(usage, "prompt_tokens", 0) if usage else 0
            tout = getattr(usage, "completion_tokens", 0) if usage else 0
            return content, tin, tout
        except ShutdownRequested:
            raise
        except openai.AuthenticationError:
            raise
        except Exception as e:
            last_err = e
            wait = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            log.warning(f"LLM error ({attempt+1}/{retries}): {e} — retry in {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"OpenAI failed after {retries} retries: {last_err}")


def llm_batch_label(items):
    hl_tokens = get_hl_tokens()
    hl_list_str = ", ".join(sorted(hl_tokens)[:80]) + ("…" if len(hl_tokens) > 80 else "")

    examples_str = "\n".join([
        f'  Tweet: "{ex["tweet"]}" → {json.dumps(ex["label"])}'
        for ex in LLM_FEW_SHOT_EXAMPLES
    ])
    user_payload = {
        "task": "label_crypto_tweets",
        "schema": {"id": "str", "is_signal": "bool", "ticker": "str",
                    "sentiment": "str", "direction": "str", "confidence": "int"},
        "rules": [
            "Return JSON: {\"labels\": [{id, is_signal, ticker, sentiment, direction, confidence}, ...]}",
            "",
            "★ CRITICAL: is_signal (boolean) — Is the author expressing a DIRECTIONAL TRADING VIEW or TAKING A POSITION?",
            "  TRUE if: author says they longed/shorted, gives entry/target/SL, recommends buying/selling,",
            "    makes a specific price prediction with direction, or provides technical analysis with a clear conclusion.",
            "  FALSE if: casual mention, question, news sharing, whale alert, on-chain data, meme, personal story,",
            "    vague commentary ('ETH is great'), thread/engagement bait, discussion without position.",
            "",
            "★ confidence (0-100) — How confident are you this is a real trading signal?",
            "  90-100: Explicit trade entry with price levels (longed at X, shorted at Y, TP/SL)",
            "  70-89:  Clear directional view with analysis (chart breakdown, bearish below X)",
            "  50-69:  Moderate directional opinion (bullish on X, accumulating)",
            "  30-49:  Weak signal, could be discussion",
            "  0-29:   Not a signal, just mentioning a token",
            "",
            "Ticker: the PRIMARY crypto being traded/analyzed. Use symbol (BTC, ETH, SOL, etc).",
            f"  ONLY use tickers from: {hl_list_str}",
            "  'NOISE' if is_signal=false, or if stablecoin, or no specific crypto.",
            "",
            "Sentiment: 'bullish', 'bearish', or 'neutral'.",
            "  ONLY use bullish/bearish if author has a clear directional view.",
            "  Use 'neutral' for anything ambiguous.",
            "",
            "Direction: 'long' or 'short'. Default: bullish→long, bearish→short, neutral→long.",
            "  'short squeeze' = direction 'long'.",
            "",
            "Strict JSON only. No markdown.",
        ],
        "examples": examples_str,
        "items": [{"id": it["id"], "tweet": it["tweet"][:1000]} for it in items],
    }
    messages = [
        {"role": "system", "content": (
            "You are an expert crypto trading signal classifier for a copy-trading platform. "
            "Your job is to determine if a tweet is an ACTIONABLE TRADING SIGNAL that someone could copy-trade. "
            "Be STRICT. Most tweets that mention crypto are NOT trading signals. "
            "A signal requires the author to be TAKING or RECOMMENDING a specific directional position. "
            "Casual discussion, news, memes, questions, on-chain alerts, and vague bullishness are NOT signals. "
            "Always return strict JSON. No markdown, no commentary."
        )},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    raw, tin, tout = _llm_request(messages, max_tokens=min(3000, 400 + 80 * len(items)), temperature=0.05)
    try:
        parsed = json.loads(raw)
        out = {}
        for rec in parsed.get("labels", []):
            _id = str(rec.get("id"))
            is_signal = bool(rec.get("is_signal", False))
            confidence = int(rec.get("confidence", 0))
            ticker_raw = str(rec.get("ticker", ""))

            if not is_signal or confidence < CONFIDENCE_THRESHOLD:
                out[_id] = {"ticker": "NOISE", "sentiment": "neutral", "direction": "long",
                            "confidence": confidence, "is_signal": False}
                continue

            ticker = normalize_ticker(ticker_raw)
            sentiment = str(rec.get("sentiment", "")).lower().strip()
            direction = str(rec.get("direction", "")).lower().strip()
            if sentiment not in ("bullish", "bearish", "neutral"):
                sentiment = "neutral"
            if direction not in ("long", "short"):
                direction = "long" if sentiment != "bearish" else "short"

            # ★ neutral sentiment with low confidence → NOISE
            if sentiment == "neutral" and confidence < 80:
                out[_id] = {"ticker": "NOISE", "sentiment": "neutral", "direction": "long",
                            "confidence": confidence, "is_signal": False}
                continue

            out[_id] = {"ticker": ticker, "sentiment": sentiment, "direction": direction,
                        "confidence": confidence, "is_signal": True}
        return out, tin, tout
    except Exception:
        log.warning(f"LLM parse error, fallback. Raw: {raw[:200]}")
        return (
            {it["id"]: {"ticker": "NOISE", "sentiment": "neutral", "direction": "long",
                        "confidence": 0, "is_signal": False} for it in items},
            tin, tout,
        )


VISION_SYSTEM_PROMPT = (
    "You are an expert crypto trading signal classifier for a copy-trading platform. "
    "Analyze the tweet text AND the attached image (chart, screenshot, etc). "
    "Return strict JSON: {\"is_signal\": bool, \"ticker\": \"...\", \"sentiment\": \"...\", "
    "\"direction\": \"...\", \"confidence\": int}\n"
    "- is_signal: true ONLY if author is taking/recommending a specific directional trade.\n"
    "- ticker: crypto symbol or 'NOISE'. Only use tradeable tokens.\n"
    "- sentiment: 'bullish','bearish','neutral'.\n"
    "- direction: 'long' or 'short'.\n"
    "- confidence: 0-100 how certain this is an actionable trading signal.\n"
    "Strict JSON only."
)

def _llm_label_with_vision(text, image_url):
    messages = [
        {"role": "system", "content": VISION_SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": f'Label this crypto tweet:\n\n"{text[:1000]}"'},
            {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
        ]},
    ]
    try:
        raw, tin, tout = _llm_request(messages, max_tokens=120, temperature=0.05, model=VISION_MODEL)
        parsed = json.loads(raw)
        is_signal = bool(parsed.get("is_signal", False))
        confidence = int(parsed.get("confidence", 0))

        if not is_signal or confidence < CONFIDENCE_THRESHOLD:
            return {"ticker": "NOISE", "sentiment": "neutral", "direction": "long",
                    "confidence": confidence, "is_signal": False}, tin, tout

        ticker = normalize_ticker(str(parsed.get("ticker", "")))
        sentiment = str(parsed.get("sentiment", "")).lower().strip()
        direction = str(parsed.get("direction", "")).lower().strip()
        if sentiment not in ("bullish", "bearish", "neutral"): sentiment = "neutral"
        if direction not in ("long", "short"): direction = "long" if sentiment != "bearish" else "short"

        if sentiment == "neutral" and confidence < 80:
            return {"ticker": "NOISE", "sentiment": "neutral", "direction": "long",
                    "confidence": confidence, "is_signal": False}, tin, tout

        return {"ticker": ticker, "sentiment": sentiment, "direction": direction,
                "confidence": confidence, "is_signal": True}, tin, tout
    except (ShutdownRequested, openai.AuthenticationError):
        raise
    except Exception as e:
        log.warning(f"Vision failed for {image_url[:60]}…: {e}")
        return {"ticker": "NOISE", "sentiment": "neutral", "direction": "long",
                "confidence": 0, "is_signal": False}, 0, 0


# ═══════════════════════════════════════════════════════════════════════
#  X API v2 CLIENT
# ═══════════════════════════════════════════════════════════════════════

X_API_BASE = "https://api.twitter.com/2"

def _x_headers():
    return {"Authorization": f"Bearer {X_BEARER_TOKEN}", "User-Agent": "HyperCopy/1.0"}

def _x_get(url, params=None, retries=3, base_delay=2.0):
    last_err = None
    for attempt in range(retries):
        _check_shutdown()
        try:
            r = requests.get(url, headers=_x_headers(), params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                reset_ts = r.headers.get("x-rate-limit-reset")
                wait = max(int(reset_ts) - int(time.time()), 1) + 2 if reset_ts else int(r.headers.get("Retry-After", 60))
                log.warning(f"X API 429 — sleeping {wait}s")
                _interruptible_sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                wait = base_delay * (2 ** attempt) + random.uniform(0, 1)
                log.warning(f"X API {r.status_code} — retry in {wait:.1f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
        except ShutdownRequested:
            raise
        except requests.exceptions.RequestException as e:
            last_err = e
            wait = base_delay * (2 ** attempt) + random.uniform(0, 1)
            log.warning(f"X API error ({attempt+1}/{retries}): {e} — {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"X API failed after {retries} retries: {last_err}")

def _interruptible_sleep(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _shutdown.is_set():
            raise ShutdownRequested()
        time.sleep(min(1.0, end - time.monotonic()))


# ═══════════════════════════════════════════════════════════════════════
#  USER PROFILE + TWEET FETCHING
# ═══════════════════════════════════════════════════════════════════════

def _resolve_user_profile(username, state_con):
    cached_uid = _state_get_user_id(state_con, username)
    url = f"{X_API_BASE}/users/by/username/{username}"
    params = {"user.fields": "profile_image_url,name,description,public_metrics,verified"}
    try:
        data = _x_get(url, params=params)
        user_data = data.get("data", {})
        uid = user_data.get("id")
        if not uid:
            log.warning(f"  ✗ Could not resolve @{username}")
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
        return profile
    except ShutdownRequested:
        raise
    except Exception as e:
        if cached_uid:
            log.warning(f"  ⚠ Profile fetch failed for @{username}, using cached uid")
            return {
                "user_id": cached_uid, "display_name": "", "avatar_url": "",
                "bio": "", "is_verified": False, "followers_count": 0, "following_count": 0,
            }
        log.warning(f"  ✗ Could not resolve @{username}: {e}")
        return None


def _fetch_user_tweets(user_id, username, since_id=None, max_days=7,
                       max_results_per_page=100, max_pages=10):
    params = {
        "max_results": max_results_per_page,
        "tweet.fields": "created_at,author_id,public_metrics,attachments",
        "expansions": "attachments.media_keys",
        "media.fields": "url,preview_image_url,type",
        "exclude": "retweets,replies",
    }
    if since_id:
        params["since_id"] = since_id
    else:
        start_time = (datetime.now(timezone.utc) - timedelta(days=max_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["start_time"] = start_time

    url = f"{X_API_BASE}/users/{user_id}/tweets"
    all_tweets = []
    pages = 0
    while pages < max_pages:
        _check_shutdown()
        data = _x_get(url, params=params)
        meta = data.get("meta", {})
        if meta.get("result_count", 0) == 0:
            break
        media_map = {}
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
                "author_username": username,
            })
        next_token = meta.get("next_token")
        if not next_token:
            break
        params["pagination_token"] = next_token
        pages += 1
        time.sleep(0.3)
    return all_tweets


# ═══════════════════════════════════════════════════════════════════════
#  ★ LABEL ONE USER'S TWEETS — stricter pipeline
# ═══════════════════════════════════════════════════════════════════════

def _label_tweets(tweets, cache_con, username="", batch_size=20):
    """Label a list of tweets. Returns list of labeled dicts."""
    results = []
    llm_text_q  = []
    llm_vis_q   = []
    token_stats = {"in": 0, "out": 0}
    noise_filtered = 0
    heuristic_labeled = 0

    for i, tw in enumerate(tweets):
        text = tw["text"]
        author = tw.get("author_username", username)

        # 0) Pre-LLM noise filter — free, instant
        if _is_noise_tweet(text, author):
            results.append({**tw, "ticker": "NOISE", "sentiment": "neutral",
                            "direction": "long", "confidence": 0, "is_signal": False})
            noise_filtered += 1
            continue

        thash = _stable_tweet_hash(text)

        # 1) cache hit
        cached = _label_cache_get(cache_con, thash)
        if cached:
            results.append({**tw, **cached})
            continue

        # 2) ★ Heuristic — ONLY if tweet has explicit trade language
        tk = _cheap_ticker(text)
        if tk and _has_explicit_trade_language(text):
            st = _cheap_sentiment(text)
            if st and st != "neutral":
                dr = _sentiment_to_direction(st, text)
                _label_cache_put(cache_con, thash, tk, st, dr, confidence=80, is_signal=True)
                results.append({**tw, "ticker": tk, "sentiment": st, "direction": dr,
                                "confidence": 80, "is_signal": True})
                heuristic_labeled += 1
                continue

        # 3) queue for LLM
        item = {"idx": i, "id": str(i), "tweet": text, "tw": tw}
        if VISION_ENABLED and tw.get("images"):
            llm_vis_q.append(item)
        else:
            llm_text_q.append(item)

    if noise_filtered > 0:
        log.info(f"    Pre-filtered {noise_filtered} noise tweets")
    if heuristic_labeled > 0:
        log.info(f"    Heuristic-labeled {heuristic_labeled} (explicit trade language)")

    # -- batch text labeling --
    for start in range(0, len(llm_text_q), batch_size):
        _check_shutdown()
        chunk = llm_text_q[start:start + batch_size]
        try:
            labels, tin, tout = llm_batch_label(chunk)
            token_stats["in"] += tin
            token_stats["out"] += tout
        except (ShutdownRequested, openai.AuthenticationError):
            raise
        except Exception as e:
            log.error(f"LLM batch failed: {e} — marking {len(chunk)} as NOISE")
            labels = {it["id"]: {"ticker": "NOISE", "sentiment": "neutral", "direction": "long",
                                  "confidence": 0, "is_signal": False} for it in chunk}

        for item in chunk:
            res = labels.get(item["id"], {"ticker": "NOISE", "sentiment": "neutral",
                                          "direction": "long", "confidence": 0, "is_signal": False})
            thash = _stable_tweet_hash(item["tweet"])
            _label_cache_put(cache_con, thash, res["ticker"], res["sentiment"], res["direction"],
                             res.get("confidence", 0), res.get("is_signal", False))
            results.append({**item["tw"], **res})

    # -- vision labeling --
    for item in llm_vis_q:
        _check_shutdown()
        imgs = item["tw"].get("images", [])
        try:
            res, tin, tout = _llm_label_with_vision(item["tweet"], imgs[0])
            token_stats["in"] += tin
            token_stats["out"] += tout
        except (ShutdownRequested, openai.AuthenticationError):
            raise
        except Exception as e:
            log.warning(f"Vision failed: {e}")
            res = {"ticker": "NOISE", "sentiment": "neutral", "direction": "long",
                   "confidence": 0, "is_signal": False}
        thash = _stable_tweet_hash(item["tweet"])
        _label_cache_put(cache_con, thash, res["ticker"], res["sentiment"], res["direction"],
                         res.get("confidence", 0), res.get("is_signal", False))
        results.append({**item["tw"], **res})
        time.sleep(0.15)

    return results, token_stats


# ═══════════════════════════════════════════════════════════════════════
#  ★ DATABASE WRITE — filters NOISE + neutral + low confidence
# ═══════════════════════════════════════════════════════════════════════

from backend.database import SessionLocal
from backend.models.trader import Trader
from backend.models.signal import Signal


def _get_or_create_trader(session, username, profile=None):
    trader = session.query(Trader).filter(Trader.username == username).first()
    if not trader:
        trader = Trader(username=username)
        session.add(trader)
        session.flush()
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
    return trader


def _signal_exists(session, tweet_id):
    if not tweet_id:
        return False
    return session.query(Signal.id).filter(Signal.tweet_id == tweet_id).first() is not None


def _write_user_signals(username, labeled_tweets, profile=None):
    # ★ THREE gates: not NOISE, is_signal=True, sentiment is bullish/bearish
    relevant = [
        r for r in labeled_tweets
        if r.get("ticker") not in ("NOISE",)
        and r.get("is_signal", False) is True
        and r.get("sentiment") in ("bullish", "bearish")
        and r.get("confidence", 0) >= CONFIDENCE_THRESHOLD
    ]
    if not relevant:
        noise = len(labeled_tweets) - len(relevant)
        return 0, 0, noise

    session = SessionLocal()
    inserted = skipped = 0
    try:
        trader = _get_or_create_trader(session, username, profile=profile)
        for r in relevant:
            tweet_id = r.get("tweet_id", "")
            if tweet_id and _signal_exists(session, tweet_id):
                skipped += 1
                continue
            imgs = r.get("images", [])
            sig = Signal(
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
            session.add(sig)
            inserted += 1
        session.commit()
    except Exception as e:
        session.rollback()
        log.error(f"DB error for @{username}: {e}")
        raise
    finally:
        session.close()

    noise = len(labeled_tweets) - len(relevant)
    return inserted, skipped, noise


# ═══════════════════════════════════════════════════════════════════════
#  PROCESS ONE USER
# ═══════════════════════════════════════════════════════════════════════

def _process_user(username, state_con, cache_con, max_days=3):
    cached_uid = _state_get_user_id(state_con, username)
    needs_profile = _state_needs_profile_refresh(state_con, username)

    profile = None
    if needs_profile or not cached_uid:
        profile = _resolve_user_profile(username, state_con)
        if not profile:
            _state_record_error(state_con, username, "")
            return None
        uid = profile["user_id"]
        _state_update_profile_time(state_con, username)
    else:
        uid = cached_uid

    since_id = _state_get_since_id(state_con, username)
    tweets = _fetch_user_tweets(uid, username, since_id=since_id, max_days=max_days)

    if not tweets:
        _state_save(state_con, username, uid, tweets_found=0)
        return {"username": username, "fetched": 0, "inserted": 0, "skipped": 0, "noise": 0}

    labeled, token_stats = _label_tweets(tweets, cache_con, username=username)

    inserted, skipped, noise = _write_user_signals(username, labeled, profile=profile)

    newest_id = max(tweets, key=lambda t: t["tweet_id"])["tweet_id"]
    _state_save(state_con, username, uid, last_tweet_id=newest_id, tweets_found=len(tweets))

    return {
        "username": username,
        "fetched": len(tweets),
        "inserted": inserted,
        "skipped": skipped,
        "noise": noise,
        "tokens_in": token_stats["in"],
        "tokens_out": token_stats["out"],
    }


# ═══════════════════════════════════════════════════════════════════════
#  MAIN CYCLE
# ═══════════════════════════════════════════════════════════════════════

def run_cycle(users, max_days=3, force_all=False):
    state_con = _state_db_connect()
    cache_con = _label_cache_connect()

    hl_tokens = get_hl_tokens()
    log.info(f"📋 HL tradeable tokens: {len(hl_tokens)} loaded")
    log.info(f"🎯 Confidence threshold: {CONFIDENCE_THRESHOLD}")

    stats = {
        "processed": 0, "skipped_not_due": 0, "skipped_circuit": 0,
        "failed": 0, "total_inserted": 0, "total_fetched": 0,
    }

    for i, username in enumerate(users):
        _check_shutdown()

        if not force_all:
            should, reason = _state_should_poll(state_con, username)
            if not should:
                if "circuit" in reason:
                    stats["skipped_circuit"] += 1
                else:
                    stats["skipped_not_due"] += 1
                continue

        log.info(f"[{i+1}/{len(users)}] @{username}")

        try:
            result = _process_user(username, state_con, cache_con, max_days=max_days)
            if result:
                stats["processed"] += 1
                stats["total_inserted"] += result["inserted"]
                stats["total_fetched"]  += result["fetched"]
                if result["inserted"] > 0 or result["fetched"] > 0:
                    log.info(f"  ✓ @{username}: {result['fetched']} fetched, "
                             f"{result['inserted']} inserted, {result['skipped']} dup, "
                             f"{result['noise']} noise")
            else:
                stats["failed"] += 1

        except ShutdownRequested:
            log.info(f"  Shutdown during @{username} — state is safe")
            raise
        except openai.AuthenticationError as e:
            log.error(f"🔑 OpenAI auth error — aborting cycle: {e}")
            raise
        except Exception as e:
            stats["failed"] += 1
            _state_record_error(state_con, username, _state_get_user_id(state_con, username) or "")
            log.error(f"  ✗ @{username} failed: {e}")

        if i < len(users) - 1:
            time.sleep(0.5)

    return stats


# ═══════════════════════════════════════════════════════════════════════
#  DAEMON LOOP
# ═══════════════════════════════════════════════════════════════════════

def run_daemon(max_days=3, force_first_cycle=False):
    users = _resolve_user_list()
    log.info(f"🐦 Ingestor daemon starting — {len(users)} users, "
             f"cycle interval={CYCLE_INTERVAL_S}s, max_days={max_days}")
    log.info(f"   LLM={LLM_MODEL}, Vision={VISION_ENABLED} ({VISION_MODEL})")
    log.info(f"   HL endpoint={HL_BASE_URL}")
    log.info(f"   🎯 Confidence threshold={CONFIDENCE_THRESHOLD}")

    hl_tokens = get_hl_tokens()
    log.info(f"   📋 {len(hl_tokens)} tradeable tokens from HL")

    cycle_num = 0
    consecutive_cycle_failures = 0

    while not _shutdown.is_set():
        cycle_num += 1
        force = force_first_cycle and cycle_num == 1
        log.info(f"\n{'='*60}")
        log.info(f"Cycle #{cycle_num} starting (force_all={force})")

        t0 = time.monotonic()
        try:
            stats = run_cycle(users, max_days=max_days, force_all=force)
            elapsed = time.monotonic() - t0
            consecutive_cycle_failures = 0

            log.info(
                f"Cycle #{cycle_num} done in {elapsed:.0f}s — "
                f"processed={stats['processed']}, inserted={stats['total_inserted']}, "
                f"fetched={stats['total_fetched']}, failed={stats['failed']}, "
                f"skipped_not_due={stats['skipped_not_due']}, "
                f"skipped_circuit={stats['skipped_circuit']}"
            )

        except ShutdownRequested:
            log.info("Shutdown requested — exiting daemon loop")
            break
        except openai.AuthenticationError:
            consecutive_cycle_failures += 1
            log.error(f"🔑 Auth failure — will retry in {CYCLE_INTERVAL_S * 2}s")
            _interruptible_sleep(CYCLE_INTERVAL_S * 2)
            continue
        except Exception as e:
            consecutive_cycle_failures += 1
            elapsed = time.monotonic() - t0
            log.error(f"Cycle #{cycle_num} crashed after {elapsed:.0f}s: {e}")
            if consecutive_cycle_failures >= MAX_CONSECUTIVE_FAILURES:
                log.critical(f"💀 {consecutive_cycle_failures} consecutive failures — exiting")
                sys.exit(1)

        log.info(f"💤 Sleeping {CYCLE_INTERVAL_S}s…")
        try:
            _interruptible_sleep(CYCLE_INTERVAL_S)
        except ShutdownRequested:
            log.info("Shutdown during sleep — exiting")
            break

    log.info("🛑 Ingestor daemon stopped cleanly")


# ═══════════════════════════════════════════════════════════════════════
#  USER LIST
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_USERS = [
    "DonAlt","CryptoCred","HsakaTrades","Tradermayne","RookieXBT",
    "DegenSpartan","ColdBloodShill","pierre_crypt0","ByzGeneral",
    "egirl_capital","TheFlowHorse","Bluntz_Capital","AltcoinSherpa",
    "SmartContracter","GCRClassic",
    "BigCheds","Ninjascalp","abetrade","tradingriot","AltcoinPsycho",
    "Pentosh1","CryptoKaleo","gainzy222","inversebrah","Tree_of_Alpha",
    "tradingstable","Nebraskangooner","satsdart","jimtalbot","Citrini7",
    "jukan05","CL207","ledgerstatus","MuroCrypto","RektProof","DaanCrypto",
    "CryptoJelleNL","NukeCapital","TraderMercury","RealCryptoFace",
    "trader1sz","Trader_XO","IncomeSharks","CryptoTony__","CryptoGodJohn",
    "EmperorBTC","PeterLBrandt","TrueCrypto28","MoonOverlord","TheCryptoDog",
    "bitcoinjack","insomniacxbt","Tom__Capital","flopxbt","docXBT",
    "Danny_Crypton","dailytradr","CryptoWizardd","trader_koala","LomahCrypto",
    "CredibleCrypto","galaxyBTC","CryptoCaesarTA","Crypto_Chase","crypto_goos",
    "ThinkingUSD","PriorXBT","yourQuantGuy","0xThoor","coinmamba",
    "CryptoPoseidonn","papagiorgioXBT","PillageCapital","macklorden",
    "kaceohhh","PhoenixBtcFire",
    "QuantMeta","TechCharts","Quantzilla34","alphawhaletrade",
    "ConnorJBates_","LightCrypto",
    "MomentumKevin","TheWhiteWhaleHL","JamesWynnReal","KeyboardMonkey3",
    "lBattleRhino","basedkarbon","Rijk__","HYPEconomist","MizerXBT",
    "lookonchain","OnChainWizard","whale_alert",
    "spot_on_chain","Arkham","tier10k","smartestmoney","DefiIgnas",
    "MustStopMurad","blknoiz06","GiganticRebirth","CryptoAnup","TedPillows",
    "Ashcryptoreal","izebel_eth","defi_mochi",
    "CryptoHayes","arthur0x","Rewkang","QwQiao",
    "Darrenlautf","ThinkingBitmex","TheBootMex","BastilleBtc",
    "ColeGarnersTake","R89Capital","JustinCBram","MissionGains",
    "dennis_qian","noBScrypto","Numb3rsguy_","EtherWizz_","TimeFreedomROB",
    "GarrettBullish",
    "rektcapital","AltCryptoGems","alicharts","CastilloTrading",
    "AltcoinDaily","TheLongInvest","CryptoBusy",
    "SolidTradesz","ZordXBT","kropts","breyonchain",
    "mert","VitalikButerin","sassal0x","hosseeb","binji_x","lex_node",
    "icobeast","DeFi_Dad","bread_","waleswoosh","andyyy","brian_armstrong",
    "0xNairolf","RyanSAdams","laurashin","udiWertheimer","sandeepnailwal",
    "jessepollak","sjdedic","EliBenSasson","Jrag0x","theunipcs",
    "scottmelker","Tyler_Did_It","leolanza","0xSammy","MikeIppolito_",
    "toly","jchaskin22","cryptorover","vibhu","aixbt_agent","KhanAbbas201",
    "AveryChing","pete_rizzo_","tkstanczak","StarPlatinum_","EasyEatsBodega",
    "j0hnwang","Marczeller","Chilearmy123","cz_binance","chainyoda",
    "mdudas","DCinvestor","uttam_singhk","cryptunez","nic_carter",
    "mteamisloading","_FORAB","ryanberckmans","TheDeFinvestor","divine_economy",
    "StaniKulechov","_Enoch","gumsays","austingriffith","stacy_muur",
    "fundstrat","AbdelStark","templecrash","serpinxbt","materkel","Punk9277",
    "dcfgod","0xJeff","LefterisJP","KookCapitalLLC","Route2FI","ilblackdragon",
    "z0r0zzz","dgt10011","notthreadguy","dotkrueger","TrustlessState",
    "litocoen","QuintenFrancois","martypartymusic","phtevenstrong","dabit3",
    "NTmoney","VivekVentures","FigoETH","DavideCrapis","FabianoSolana",
    "rektdiomedes","0xngmi","pcaversaccio","Kylechasse","PendleIntern",
    "crypto_condom",
    "0xSigil","soispoke","EvgenyGaevoy","rudolf6_",
    "MSBIntel","Bfaviero","fintechfrank","Snapcrackle","fede_intern","keoneHD",
    "balajis","KyleSamani","0xDeployer","sgoldfed","AlexFinn",
    "MINHxDYNASTY","mattshumer_","karpathy","milesdeutscher",
    "AzFlin","SolanaSensei","cryptopunk7213","ljxie","LeonWaidmann",
    "ec265","shahh","KevinWSHPod","eeelistar","0xCygaar",
    "Evan_ss6","Legendaryy","armaniferrante","saylor","jussy_world",
    "aerugoettinea","lordjorx","wmougayar","Jackkk",
    "CryptoCapo_","zachxbt","TechDev_52","Megga","JacobKinge",
    "DrProfitCrypto","traderstewie","crypto_bitlord7","cryptocevo","BobLoukas",
    "ksicrypto","AngeloBTC","alpharivelino","SecretsOfCrypto","_TJRTrades",
    "InvestorsLive","Ultra_Calls","cryptolyxe","MacroCRG","Cryptopathic",
    "crypto888crypto","sibeleth","WhaleFactor","traderpow","bitbitcrypto",
    "awawat","LSDinmycoffee","JoshMandell6","koreanjewcrypto",
    "cyrilXBT","George1Trader","UpOnlyTV","btc_charlie","gametheorizing",
    "TraderKoz","imperooterxbt","follis_","AWice","krybharat",
    "SpiderCrypto0x","MichaelXBT","rasmr_eth","KillaXBT","HerroCrypto",
    "Julianpetroulas","madaznfootballr","john_j_brown","RunnerXBT","TXMCtrades",
    "cryptonary","9x9x9eth","PaikCapital","PastelAlpha","NachoTrades",
    "Steven1_994","0xMaki","DefiSquared","CryptoParadyme","smileycapital",
    "_Investinq","bizyugo","alphawifhat","TraderNJ1","justintrimble",
    "buyerofponzi","naniXBT","splinter0n","CryptoVikings07","depression2019",
    "edwardmorra_btc","BullyDCrypto","CFTradercom","Beetcoin","arjunsethi",
    "DarkCryptoLord","0xkyle__","MacroMate8","WazzCrypto","punk3178",
    "degentrades8","Tradermeow1","degenharambe","k0a_hl","0xFarmer2",
    "DegenMandan","degenwiftrading","cryptowoIf","0xAllen_","Crypt0sRus",
    "Trfnds","Trader_Z5","PaperImperium","AdamScochran","Damskotrades",
    "CryptoColdBlood","HighSolGas","xkairos_","FatManTerra","SmartMoney0x",
    "hinkok_","kevwuzy","FlurETH","Chairman_DN","Swingtrader",
    "TitanXBT","XXAntiWar","newmichwill","elkwood66","CrypticTrades_",
    "tryfomo","AguilaTrades","HanweChang","nataninvesting",
    "MagicPoopCannon","Innerdevcrypto","dapersiantrader","0xaporia","0xVKTR",
    "Yodaskk","IcedcoffeeEth","PopcornKirby","ExitLiqCapital","KrisVerma88",
    "TheCryptoNexus","apralky","InsilicoTrading","trading__horse","BlurCrypto",
    "Bthestory87","AK_EtherMachine","FloodCapital","ShirleyXBT","umikathryn",
    "CryptoCronkite","fey_xbt","TheNFTAsian","Mattertrades","tradexyz",
    "RowdyCrypto","missoralways","OuroborosCap8","Raizelxbt","JPEGd_69",
    "zinceth","harmonictrader","snipeder","JagoeCapital","cited",
    "nftpho","real_y22","not_zkole","grugcapital","sue_xbt",
    "kerneltrader","kirbxbt","degentral","Glitch_Trades","Jahncrypto",
    "PelionCap","plum_eth","mm_flooded","Jin67171","pvp_dot_trade",
    "dunleavy89","SpacemanBTC","TwonXBT","SOL_Decoder","0x_Swiper",
    "pikachu_crypto","drewlivanos","TheTradingTank","Sakrexer","eyeonchains",
    "saliencexbt","Tyler_Neville_","CryptoEthan","notwarrenETH","ezcontra",
    "CryptoCharming","dogoshii","varrock","SilvXBT","cas3333333",
    "NMTD8","badenglishtea","realjaypelle","itspyrored","AvgJoesCrypto",
    "3liXBT","GuthixHL","Crypt0_Coral","wardaddycapital","SailorManCrypto",
    "Teambuertrades","RiffRaffOz","Discodoteth","GreekGamblerPM","Skarly",
    "WBJcrypto","Dxranteth","dragossden","leakmealpha","burstingbagel",
    "hyenatrade","0xHedge32","cosmic_xbt","LuckyXBT__","crypto_noodles",
    "HugoAlphaa","muststopNlG","kronbtc","0xLightcycle","SpecterAnalyst",
    "nicodotfun","CryptoNinjaah","k1z4_","RamXBT","crypto_adair",
    "Keisan_Crypto",
    "cobie","WClemente","VentureCoinist","loomdart","Loopify",
    "TylerDurden","lawmaster","IamNomad","krugermacro","Bagsy",
    "moonberg","Icebergy","cmsholdings","Dentoshi","intern",
    "crypto_iso","52kskew","goodalexander","wabdoteth","cointradernik",
    "Jebus","ThisIsNuse","22loops","hentaiavenger66","AviFelman",
    "boldleonidas","SizeChad","karbonbased","hedgedhog7","ciniz",
    "icebagz_","chimpp","zackvoell","CryptoHornHairs","soby0x",
    "cubantobacco","Husslin_","SplitCapital","redxbt","CryptoCharles__",
    "maruushae","mewn21","Pollo2x","deltaxbt","vydamo_",
    "cryptoaladeen","Chad_Hominem_","Abu9ala7","tnuttin1","daremo67",
    "KRMA_0","tristan","wasserpest","SMtrades_","Glimmerycoin",
    "AkadoSang","simpelyfe","AgentChud","Alice_comfy","stackinbits",
    "0xKNL__","BenYorke","MeatEsq",
    "AutismCapital","LynAldenContact","CryptoMichNL",
    "charliebilello","zhusu","eliz883","PeterMcCormack",
    "MacnBTC","punk6529","farokh","DylanLeClair","EricBalchunas",
    "KoroushAK","paoloardoino","Trader_Dante","cburniske",
    "a1lon9","gmoneyNFT","TikTokInvestors","DeeZe",
    "inmortalcrypto","AriDavidPaul","LucaNetz","SalsaTekila","seedphrase",
    "ASvanevik","coinfessions","BobEUnlimited","notsofast",
    "Crypto_Ed_NL","Vince_Van_Dough","Darkfarms1","ercwl","Stable",
    "iamjosephyoung","RNR_0","samczsun","trading_axe",
    "CarpeNoctom","jchervinsky","cryptolimbo","wizardofsoho","DoveyWan",
    "cryptomocho","JJcycles","bitcoin_dad","HighStakesCap","BullyEsq",
    "0xENAS","tradinglord","based16z","Livercoin","bitcoinpanda69",
    "needacoin","SCHIZO_FREQ","PostyXBT","panamaXBT","CryptOrca",
    "0xBackwards","CJ900X","Sicarious_","MacroScope17","owen1v9",
    "mattyryze","DipWheeler","MikeMcDonald89","timelessbeing",
    "knowerofmarkets","Beastlyorion","BitcoinBirch","cole0x","insiliconot",
    "wmd4x","crypto_bobby","im_goomba","TraderMagus","functi0nZer0",
    "TraderMotif","CryptoUB","cryptodude999",
    "ppmcghee","heart_","barneytheboi","purchasable","BrettHarrison",
    "Berko_Crypto","IDrawCharts","SmokeyHosoda","poordart",
    "KieranWarwick","tztokchad","Cheguevoblin","BigDickBull69","anambroid",
    "cyrii_MM","Fullbeerbottle","CryptoMaestro","sershokunin","majinsayan",
    "wassielawyer","HackermanAce","vec0zy","NewsyJohnson","veH0rny",
    "Pancakesbrah","_tm3k","conzimp","maybeltr","BobLaxative",
    "Crypto_Core","bitcoinPalmer","Socal_crypto","219_eth","AltsQ",
    "_RN03xx_","bitcoinbella_","bigdsenpai","Brentsketit","lowstrife",
    "passytee","chatwithcharles","13yroldwithcc","insilicobunker","KRTrades_",
    "benbybit","jespow","0xfoobar","andy8052","juliankoh",
    "alexonchain","tradeboicarti16","justinsuntron","andrecronjetech",
    "ki_young_ju","melt_dem","hasufl","rager","devchart",
    "aubreystrobel","0xsisyphus","pythianism","echodotxyz","smokeythebera",
    "chameleon_jeff","fiskantes","cbb0fe","degenping","galois_capital",
    "imnotthewolf","chiefingza","amdtrades","pwnlord69","roofhanzo",
    "smilinglllama","crypto_coffee","cryptobulma","lord_ashdrake","0xalan_",
    "monkeycharts","fishxbt","jessiemorii","knlae_","wantonwallet",
    "mcdonaldsxbt","tackettzane","grimace85","muncheds2","coffeebreak_yt",
    "loldefi","betagaiden",
    "naval","apompliano","zssbecker","raoulgmi","danheld",
    "elliotrades","erikvoorhees","barrysilbert","weremeow",
    "crypto_birb","jihoz_axie","novogratz","cryptowendyo",
    "frankdegods","selkis_2028","icedknife","orangie","solbigbrain",
    "chooserich","shardib2","lilmoonlambo","osf_rekt","cousincrypt0",
    "finallyx","algodtrading","stronghedge","matthuang","rektmando",
    "natealex","mrjasonchoi","thecryptomonk","shayne_coplan",
    "fxnction","kaiynne","axel_bitblaze69","cryptogarga","primordialaa",
    "smallcapscience","artsch00lreject","jonwu_","tayvano_",
    "doncryptodraper","danrobinson","thecryptocactus","thiccyth0t","mbtcpiz",
    "emiliemc","fomosaurus","cecilia_hsueh","jconorgrogan","0x_ultra",
    "zoomeroracle","cryptocx1","0xwangarian","grindingpoet",
    "0xmerp","tittyrespecter","gwartygwart","aureliusbtc",
    "n2ckchong","ashtoshii","tysonthe2nd","stoicsavage","twicrates",
    "blancxbt","broccolex","joshmcgruff","fitchinverse","0xanteater",
    "bitdealer_","stockmart_","0xbags","33b345","bulltrapper00",
    "plur_daddy","convexmonster","ericjuta","exitscammed","zerosupremecy",
    "satoshiwolf","degen_alfie","imcryptogoku","high_fades","lucky",
    "tomooshi_","gigimp1","bitccolo","scottshapiro","kuroxlb",
    "jeff_w1098","jebus911","fjvdb7","seranged","psyopcop",
    "anbessa100","haydenzadams",
    "100trillionusd","greg16676935420",
    "willywoo","satoshilite",
    "cdixon",
    "litcapital","excellion",
    "bankless","santiagoaufund","tuurdemeester","sriramk",
    "wuwei113","danielesesta","bbands","cryptonewton",
    "ericcryptoman","dingalingts","fehrsam",
    "banteg","chainlinkgod","cryptobull","jseyff",
    "lopp","shaughnessy119","lmrankhan","fapital3","chang_defi",
    "0xballoonlover","cryptowhail","tier1haterr","counterpartytv","swansonbenson0",
    "sanlsrni","eleanorterrett","santiagoroel","himgajria",
    "taikimaeda2","thebrianjung",
    "kevinsekniqi","wagieeacc",
    "transmissions11","blocmates","zoomerfied","amywumartin","tarunchitra",
    "gdog97_","avichal","kloss_xyz","quanterty","dreamcash",
    "0xlawliette","mlmabc","dogetoshi","0xdoug","apewoodx",
    "mapleleafcap","statelayer","game_for_one","freddieraynolds","0xbizzy",
    "kanavkariya","_dave__white_","watchking69","mhonkasalo",
    "0xracist","sherlock_hodles","frankieislost","poppunkonchain","0xdamien",
    "eyearea","delucinator","milesjennings","vnovakovski","zemirch",
    "cuntycakes123","disenpepe","riddle245","shanaggarwal","roshunpatel",
    "hantengri","atalantis7","vannacharmer","quadcommas","ambush",
    "simonsvatos1","0x4c756b65","zoomeranon","erebos991","yellowbrah69",
    "keyboardbmonkey","elder_dt","fakerosaparkxbt","sigmaxavi","0xemperor",
    "mo_xbt","apram89","danreecer_",
    "beaniemaxi","machibigbrother","0xunihax0r","unclesendit",
    "vcbrags","milkman2228","cryptomanran",
    "realrossu","zmanian",
    "justinblau","arjunblj","beausecurity","billym2k","valkenburgh",
    "punk4156","bryptobelz","crownupguy","rampcapitalllc",
    "bendavenport","dollar_monopoly",
    "nickshirleyy","gordongekko","adam3us",
    "the_jefferymead","heyibinance",
    "ethereumjoseph","rajgokal",
    "_richardteng","el33th4xor","whalepanda",
    "bcherny","tbpn",
    "olimpiocrypto",
    "silverbulletbtc","dougpolkvids","kapothegoat01",
    "austin_federa","boxmining","ottosuwen","kmoney",
    "rleshner","cooopahtroopa","gakonst","moonshilla","teddycleps",
    "alistairmilne","colethereum","thecryptomist","caprioleio",
    "joeymoose","blackbeardxbt","jasonyanowitz","bohines",
    "fluffypony","vohvohh",
    "cryptohustle","theo_crypto99",
    "cometcalls","cryptosqueeze","crypto_mckenna","rektober","altcoinist",
    "benarmstrongsx","NeerajKA",
]

# ═══════════════════════════════════════════════════════════════════════
#  COMPAT: run_once() — called by ingestor_loop.py service wrapper
# ═══════════════════════════════════════════════════════════════════════

def run_once(max_days=3):
    """Single-cycle entry point for ingestor_loop.py compatibility."""
    users = _resolve_user_list()
    return run_cycle(users, max_days=max_days, force_all=False)


def _resolve_user_list() -> List[str]:
    if SCRAPE_USERS_ENV:
        parts = [p.strip() for p in SCRAPE_USERS_ENV.split(",") if p.strip()]
        if parts:
            return parts
    return DEFAULT_USERS


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="HyperCopy Ingestor")
    p.add_argument("--max-days", type=int, default=3)
    p.add_argument("--force-first", action="store_true",
                   help="Force poll all users on the first cycle")
    p.add_argument("--once", action="store_true",
                   help="Run a single cycle then exit (for testing)")
    p.add_argument("--force-all", action="store_true",
                   help="Force poll all users (implies --once)")
    args = p.parse_args()

    if args.force_all:
        users = _resolve_user_list()
        stats = run_cycle(users, max_days=args.max_days, force_all=True)
        log.info(f"Done: {stats}")
    elif args.once:
        users = _resolve_user_list()
        stats = run_cycle(users, max_days=args.max_days, force_all=False)
        log.info(f"Done: {stats}")
    else:
        run_daemon(max_days=args.max_days, force_first_cycle=args.force_first)