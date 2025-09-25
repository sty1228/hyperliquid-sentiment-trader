
from __future__ import annotations
import os
import re
import ast
import time
import json
import math
import base64
import hashlib
import random
import sqlite3
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List, Dict, Any

# Selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

import openai


from backend.config import load_env, env
load_env()

DATA_DIR = env("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)

INPUT_CSV_PATH  = os.path.join(DATA_DIR, "twitter_scraping_results.csv")
OUTPUT_CSV_PATH = os.path.join(DATA_DIR, "tweets_processed_complete.csv")
LABEL_CACHE_PATH = os.path.join(DATA_DIR, "label_cache.sqlite")

OPENAI_API_KEY = env("OPENAI_API_KEY")
TWITTER_USER   = env("TWITTER_USER")
TWITTER_PASS   = env("TWITTER_PASS")

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in .env")

openai.api_key = OPENAI_API_KEY

SCRAPE_USERS_ENV = env("SCRAPE_USERS", "")

ALNUM_RE = re.compile(r"[^A-Z0-9]")
DOLLAR_TICKER_RE = re.compile(r"\$([A-Za-z0-9]{2,15})\b")
HASH_TICKER_RE   = re.compile(r"#([A-Za-z0-9]{2,15})\b")
PLAIN_TICKER_RE  = re.compile(r"\b([A-Z]{2,10})\b")

# common list (will be expanded)
COMMON_CRYPTO = {
    "BTC","ETH","SOL","DOGE","XRP","BNB","ADA","AVAX","ARB","OP","PEPE","TON",
    "LINK","DOT","MATIC","APT","SUI","ONDO","ATOM","TIA","NEAR","INJ","FET",
    "ORDI","RUNE","UNI","LTC","ETC","FIL","AAVE","DYDX","JUP","WIF","SEI","PYTH",
    "ENA","ARKM","SATS","TAO","W","STRK","BLAST","BLUR","BEAM","SAGA","RLB",
}

POS_WORDS = {"pump","moon","mooning","breakout","bull","bullish","send","sending","fly","flying",
             "rip","ripping","up only","ath","rocket","🚀","✅","🔥","📈","strong"}
NEG_WORDS = {"dump","bear","bearish","rekt","rip down","down bad","nuke","nuked","crash","crashing",
             "sell","selling","🟥","🔻","📉","weak","rug"}

#TICKER NORMALIZATION

PAIR_SEPARATORS = ["/", "-", "_", ":"]
STABLE_SUFFIXES = ["USDT", "USDC", "USD"] 
PERP_SUFFIXES   = ["-PERP", "PERP", "-PERPETUAL", "PERPETUAL", "-PERPETUAL", "_PERP", ".P"]  # strip if contract suffix

ALIAS_TO_CANON: Dict[str, str] = {
    # Bitcoin & family
    "XBT":"BTC","WBTC":"BTC","BTCB":"BTC",
    # ETH & L2 ecosystem
    "WETH":"ETH","ETH2":"ETH",
    "ARB-USD":"ARB","ARBITRUM":"ARB","ARBIT":"ARB",
    "OP-USD":"OP","OPTIMISM":"OP",
    "MATIC":"MATIC","POL":"POL", 
    # BCH / BSV / forks
    "BCC":"BCH","BCHABC":"BCH","BCHSV":"BSV",
    # Ripple/XRP
    "XRP-USD":"XRP","RIPPLE":"XRP",
    # Dogecoin & dog coins
    "XDG":"DOGE","DOG":"DOGE","SHIBAINU":"SHIB","SHIBA":"SHIB",
    # Solana & friends
    "W SOL":"SOL","SOL1":"SOL",
    # Old symbols / exchange variants
    "NAN0":"NANO","IOTA":"IOTA","MIOTA":"IOTA","XEM":"XEM","XLM":"XLM","XMR":"XMR",
    "BTTOLD":"BTT","BTTNEW":"BTT",
    # Stablecoins (canonical tickers kept)
    "USDT":"USDT","USDC":"USDC","DAI":"DAI","FDUSD":"FDUSD","TUSD":"TUSD","USDP":"USDP","PYUSD":"PYUSD","USTC":"USTC",
    # Chainlink etc.
    "LINK":"LINK","LNK":"LINK",
    # Perp/contract aliases occasionally seen
    "BTC-PERP":"BTC","ETH-PERP":"ETH","SOL-PERP":"SOL","ONDO-PERP":"ONDO","PEPE-PERP":"PEPE",
    # Popular L1/L2
    "ADA":"ADA","AVAX":"AVAX","NEAR":"NEAR","ATOM":"ATOM","ALGO":"ALGO","XTZ":"XTZ","FIL":"FIL","ETC":"ETC","LTC":"LTC",
    # DeFi blue chips
    "UNI":"UNI","AAVE":"AAVE","CAKE":"CAKE","COMP":"COMP","MKR":"MKR","CRV":"CRV","CVX":"CVX","SNX":"SNX","YFI":"YFI",
    # Oracles / infra
    "PYTH":"PYTH","BAND":"BAND","API3":"API3","TRB":"TRB",
    # Exchange tokens
    "BNB":"BNB","HT":"HT","OKB":"OKB","LEO":"LEO","GT":"GT","KCS":"KCS","CRO":"CRO",
    # Memes & new waves (selection)
    "PEPE":"PEPE","WIF":"WIF","BONK":"BONK","FLOKI":"FLOKI","DOGS":"DOGS","MOG":"MOG","PONKE":"PONKE",
    "TURBO":"TURBO","PUPS":"PUPS","SATS":"SATS",
    # AI / RWA / micro sectors
    "FET":"FET","TAO":"TAO","RNDR":"RNDR","ARKM":"ARKM","GLM":"GLM","GRT":"GRT","OCEAN":"OCEAN","AGIX":"AGIX","NMR":"NMR",
    "ONDO":"ONDO","POLYX":"POLYX","PENDLE":"PENDLE","ENA":"ENA",
    # Other frequent symbols
    "INJ":"INJ","TIA":"TIA","SEI":"SEI","RUNE":"RUNE","ORDI":"ORDI","JUP":"JUP","DYDX":"DYDX","SUI":"SUI","APT":"APT",
    "BLAST":"BLAST","BLUR":"BLUR","BEAM":"BEAM","SAGA":"SAGA","RLB":"RLB","STRK":"STRK","W":"W",
    # Legacy/alt spellings
    "IOTX":"IOTX","IOTA-USD":"IOTA","DOT-USD":"DOT","TRX-USD":"TRX","TRON":"TRX","EGLD":"EGLD","ICP":"ICP",
    "FTM":"FTM","IMX":"IMX","KAS":"KAS","KAVA":"KAVA","AR":"AR","ROSE":"ROSE","MINA":"MINA","HNT":"HNT",
    "CHZ":"CHZ","SAND":"SAND","MANA":"MANA","APE":"APE","GALA":"GALA",
}

STRICT_CANON = False
SUPPORTED_CANON: set[str] = set(ALIAS_TO_CANON.values()) | {
    "BTC","ETH","SOL","XRP","DOGE","BNB","ADA","AVAX","DOT","MATIC","NEAR","ATOM","FIL","ETC","LTC","LINK",
    "UNI","AAVE","CRV","SNX","MKR","COMP","CVX","YFI","TRX","XTZ","ALGO","ICP","EGLD","FTM","APT","SUI","SEI","TIA",
    "INJ","RUNE","ORDI","JUP","DYDX","WIF","PEPE","FET","TAO","RNDR","ARKM","PYTH","SATS","GRT","OCEAN","AGIX",
    "KAS","KAVA","AR","ROSE","MINA","HNT","CHZ","SAND","MANA","APE","GALA","ONDO","POL","POLYX","PENDLE","ENA","BLUR",
    "BEAM","SAGA","RLB","STRK","W","OP","ARB"
}

def _strip_contract_suffix(sym: str) -> str:
    for suf in PERP_SUFFIXES:
        if sym.endswith(suf):
            base = sym[: -len(suf)]
            return base if base else "PERP"
    return sym

def _strip_stable_quote_pair(sym: str) -> str:
    # Pair forms: BTCUSDT, BTC-USD, BTC/USD, BTC_USDC, BTC:USDT, USDTBTC (rare), and inverse like ONDO-PERP/USDT
    u = sym.upper()
    for sep in PAIR_SEPARATORS:
        if sep in u:
            parts = [p for p in u.split(sep) if p]
            if len(parts) == 2:
                a, b = parts[0], parts[1]
                if b in STABLE_SUFFIXES:
                    return a
                if any(b.endswith(suf) or b == suf for suf in PERP_SUFFIXES):
                    return a
                if a in STABLE_SUFFIXES:
                    return b
            if len(parts) >= 3:
                for p in parts:
                    if p not in STABLE_SUFFIXES and not any(p.endswith(suf) or p == suf for suf in PERP_SUFFIXES):
                        return p
    for q in STABLE_SUFFIXES:
        if u.endswith(q) and len(u) > len(q) + 1:
            return u[: -len(q)]
    return u

def normalize_ticker(raw: str) -> str:

    if not raw:
        return "NOISE"
    s = raw.strip().upper().replace("$","").replace("#","")
    s = s.replace(" ", "")
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

# CACHE & RULES

def _label_cache_connect() -> sqlite3.Connection:
    con = sqlite3.connect(LABEL_CACHE_PATH)
    con.execute("""
    CREATE TABLE IF NOT EXISTS label_cache (
        tweet_hash TEXT PRIMARY KEY,
        ticker     TEXT NOT NULL,
        sentiment  TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    return con

def _label_cache_get(con: sqlite3.Connection, tweet_hash: str) -> Optional[Tuple[str,str]]:
    row = con.execute("SELECT ticker, sentiment FROM label_cache WHERE tweet_hash = ?", (tweet_hash,)).fetchone()
    return (row[0], row[1]) if row else None

def _label_cache_put(con: sqlite3.Connection, tweet_hash: str, ticker: str, sentiment: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO label_cache (tweet_hash, ticker, sentiment, created_at) VALUES (?,?,?,?)",
        (tweet_hash, ticker, sentiment, datetime.utcnow().isoformat())
    )
    con.commit()

def _stable_tweet_hash(text: str, images_field: Any) -> str:
    txt = (text or "").strip()
    imgs: List[str] = []
    if isinstance(images_field, str):
        try:
            arr = ast.literal_eval(images_field)
            if isinstance(arr, list):
                imgs = [str(x) for x in arr[:2]]
        except Exception:
            pass
    h = hashlib.sha256()
    h.update(txt.encode("utf-8"))
    if imgs:
        h.update(("|".join(imgs)).encode("utf-8"))
    return h.hexdigest()

def _cheap_ticker(text: str) -> Optional[str]:
    if not text:
        return None
    m = DOLLAR_TICKER_RE.search(text)
    if m:
        return normalize_ticker(m.group(1))
    m = HASH_TICKER_RE.search(text)
    if m:
        sym = normalize_ticker(m.group(1))
        if sym in COMMON_CRYPTO:
            return sym
    candidates = set(PLAIN_TICKER_RE.findall(text.upper()))
    candidates = {normalize_ticker(c) for c in candidates}
    candidates = {c for c in candidates if c in COMMON_CRYPTO}
    if len(candidates) == 1:
        return next(iter(candidates))
    return None

def _cheap_sentiment(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    pos = any(w in t for w in POS_WORDS)
    neg = any(w in t for w in NEG_WORDS)
    if pos and not neg:
        return "bullish"
    if neg and not pos:
        return "bearish"
    return None

#OPENAI

def _llm_request(messages: List[Dict[str, Any]], max_tokens: int, temperature: float,
                 retries: int = 4, base_delay: float = 1.0) -> Tuple[str, int, int]:
    """
    Robust request with backoff. Returns (assistant_content, tokens_in, tokens_out)
    """
    last_err = None
    for attempt in range(retries):
        try:
            resp = openai.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"}
            )
            content = (resp.choices[0].message.content or "").strip()
            # Usage telemetry (if available)
            usage = getattr(resp, "usage", None)
            tokens_in  = getattr(usage, "prompt_tokens", 0) if usage else 0
            tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0
            return content, tokens_in, tokens_out
        except Exception as e:
            last_err = e
            wait = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            print(f"[LLM] Error ({attempt+1}/{retries}): {e} → retry in {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"OpenAI request failed after retries: {last_err}")

def llm_batch_label(items: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, str]], int, int]:
    """
    items: [{id, tweet}]
    returns: (labels_map, tokens_in, tokens_out)
             labels_map = {id: {"ticker":"...", "sentiment":"bullish|bearish|neutral"}}
    """
    user_payload = {
        "task": "label_crypto_tweets",
        "schema": {"id":"str","ticker":"str","sentiment":"str"},
        "rules": [
            "Return a JSON object with key 'labels' as a list of {id,ticker,sentiment}.",
            "Ticker: a crypto symbol like BTC/ETH/ONDO; return 'MARKET' if general market; 'NOISE' if unrelated.",
            "Sentiment: one of bullish, bearish, neutral.",
            "If multiple tickers, pick the primary focus.",
            "No extra keys or commentary."
        ],
        "items": [{"id": it["id"], "tweet": it["tweet"][:1000]} for it in items]
    }

    messages = [
        {"role": "system", "content": "You are a precise JSON labeling engine. Always return strict JSON."},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}
    ]
    raw, tin, tout = _llm_request(messages, max_tokens=min(400, 60 + 18*len(items)), temperature=0.1)
    try:
        parsed = json.loads(raw)
        out: Dict[str, Dict[str, str]] = {}
        for rec in parsed.get("labels", []):
            _id = str(rec.get("id"))
            ticker_raw = str(rec.get("ticker","")).strip()
            sentiment   = str(rec.get("sentiment","")).lower().strip()
            # Normalize ticker robustly
            ticker = normalize_ticker(ticker_raw)
            if sentiment not in ("bullish","bearish","neutral"):
                sentiment = "neutral"
            out[_id] = {"ticker": ticker if ticker in ("MARKET","NOISE") else ticker, "sentiment": sentiment}
        return out, tin, tout
    except Exception as e:
        print(f"[LLM] Parse error → fallback neutral/noise. Raw: {raw[:200]}")
        return ({it["id"]: {"ticker":"NOISE","sentiment":"neutral"} for it in items}, tin, tout)

# SCRAPER

def twitter_login(driver, username, password) -> bool:
    driver.get("https://twitter.com/login")
    time.sleep(5)
    try:
        if "home" in driver.current_url.lower() or "timeline" in driver.current_url.lower():
            print("Already logged in")
            return True
        user_input = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.NAME, "text")))
        user_input.clear(); user_input.send_keys(username); user_input.send_keys(Keys.ENTER); time.sleep(3)
        try:
            maybe_user = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.NAME, "text")))
            if maybe_user:
                maybe_user.clear(); maybe_user.send_keys(username); maybe_user.send_keys(Keys.ENTER); time.sleep(2)
        except Exception:
            pass
        pass_input = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.NAME, "password")))
        pass_input.clear(); pass_input.send_keys(password); pass_input.send_keys(Keys.ENTER); time.sleep(8)
        ok = ("home" in driver.current_url.lower()) or ("timeline" in driver.current_url.lower())
        print("Login successful" if ok else f"Login may have failed, URL: {driver.current_url}")
        return ok
    except Exception as e:
        print(f"Login failed: {e}")
        return False

def check_rate_limit(driver) -> bool:
    try:
        page_source = driver.page_source.lower()
        for indicator in ["something went wrong","try again","rate limit","temporarily restricted",
                          "unusual activity","too many requests"]:
            if indicator in page_source:
                return True
        return False
    except Exception as e:
        print(f"Error checking rate limit: {e}")
        return False

def _js_top(driver):
    driver.execute_script("try{window.history.scrollRestoration='manual'}catch(e){}")
    driver.execute_script("window.scrollTo(0,0); document.documentElement.scrollTop=0; document.body.scrollTop=0;")

def _wait_first_article(driver, timeout=20) -> bool:
    try:
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, '//article[@role="article"]')))
        return True
    except TimeoutException:
        return False

def _first_visible_tweet_dt(driver) -> Optional[datetime]:
    try:
        node = driver.find_element(By.XPATH, '(//article[@role="article"]//time[@datetime])[1]')
        iso = node.get_attribute("datetime")
        if iso:
            return datetime.fromisoformat(iso.replace("Z","+00:00"))
    except Exception:
        pass
    return None

def _is_on_user_timeline(driver, username: str) -> bool:
    url = (driver.current_url or "").lower()
    return (("/"+username.lower()) in url) and ("/status/" not in url)

def _is_pinned(el) -> bool:
    try:
        if el.find_elements(By.XPATH, './/*[contains(@aria-label,"Pinned")]'):
            return True
        labels = el.find_elements(By.XPATH, './/span[contains(text(), "Pinned")]')
        if any(lbl.is_displayed() for lbl in labels):
            return True
        if el.find_elements(By.XPATH, './/*[@data-testid="icon-pin"]'):
            return True
    except Exception:
        pass
    return False

def _anchor_scroll_to_last(driver, elements):
    try:
        if elements:
            driver.execute_script("arguments[0].scrollIntoView({block:'end'});", elements[-1])
            time.sleep(random.uniform(1.0,1.8))
        else:
            driver.execute_script("window.scrollBy(0,1400);"); time.sleep(random.uniform(1.0,1.6))
    except Exception:
        driver.execute_script("window.scrollBy(0,1400);"); time.sleep(random.uniform(1.0,1.6))

def extract_tweet_text(element, driver) -> str:
    try:
        methods = [
            lambda el: el.find_element(By.XPATH, './/div[@data-testid="tweetText"]').get_attribute("textContent"),
            lambda el: el.find_element(By.XPATH, './/div[@data-testid="tweetText"]').text,
            lambda el: "".join(
                (span.get_attribute("textContent") or span.text or "")
                for span in el.find_elements(By.XPATH, './/div[@data-testid="tweetText"]//span')
            ),
            lambda el: el.find_element(By.XPATH, './/div[@data-testid="tweetText"]').get_attribute("innerHTML"),
        ]
        for i, fn in enumerate(methods):
            try:
                text = fn(element)
                if text and text.strip():
                    if i == 3:
                        text = re.sub(r"<[^>]+>", "", text)
                        text = text.replace("&amp;","&").replace("&lt;","<").replace("&gt;",">")
                    return text.strip()
            except Exception:
                continue
        return ""
    except Exception as e:
        print(f"Error extracting text: {e}")
        return ""

def get_tweets_for_user(username, driver, max_days=7, max_scrolls=30):
    url = f"https://twitter.com/{username}"
    driver.get(url); time.sleep(6)

    if check_rate_limit(driver):
        print(f"Rate limited for {username}. Waiting 5 minutes…")
        time.sleep(300); driver.refresh(); time.sleep(8)
        if check_rate_limit(driver):
            print(f"Still rate limited for {username}, skipping…")
            return [], [], []

    _js_top(driver)
    if not _wait_first_article(driver, timeout=25):
        print("No tweets rendered; reloading…")
        driver.get(url); time.sleep(6); _js_top(driver); _wait_first_article(driver, timeout=25)

    first_dt = _first_visible_tweet_dt(driver)
    if first_dt:
        recent_guard = datetime.now(timezone.utc) - timedelta(hours=36)
        if first_dt < recent_guard:
            print(f"First visible tweet looks old ({first_dt}), reloading…")
            driver.get(url); time.sleep(6); _js_top(driver); _wait_first_article(driver, timeout=25)

    tweets, tweet_dates, tweet_images = [], [], []
    seen_tweets = set()
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=max_days)

    scrolls = 0
    consecutive_no_new = 0
    old_tweets_total = 0

    while scrolls < max_scrolls and consecutive_no_new < 3:
        if scrolls > 0 and scrolls % 5 == 0 and check_rate_limit(driver):
            print(f"Rate limited mid-scrape for {username} at scroll {scrolls}")
            break

        if not _is_on_user_timeline(driver, username):
            print(f"Navigation detected! Back to {username}")
            driver.get(url); time.sleep(5); _js_top(driver); _wait_first_article(driver, timeout=20)
            continue

        try:
            elements = driver.find_elements(By.XPATH, '//article[@role="article"]')
            print(f"Scroll {scrolls}: Found {len(elements)} tweet elements")
        except Exception as e:
            print(f"Error finding tweet elements: {e}")
            time.sleep(5); scrolls += 1; continue

        if not elements:
            print("No tweet elements yet, waiting…")
            time.sleep(6); consecutive_no_new += 1; scrolls += 1; continue

        added_this_scroll = 0
        old_this_scroll = 0

        for i, el in enumerate(elements):
            try:
                try:
                    author_links = el.find_elements(By.XPATH, './/div[@data-testid="User-Name"]//a[contains(@href,"/")]')
                    if not author_links:
                        author_links = el.find_elements(By.XPATH, f'.//a[contains(@href, "/{username}")]')
                    if not any(("/"+username.lower()) in (a.get_attribute("href") or "").lower() for a in author_links):
                        continue
                except Exception:
                    continue

                if _is_pinned(el):
                    continue

                final_text = extract_tweet_text(el, driver)
                if not final_text:
                    continue

                if not _is_on_user_timeline(driver, username):
                    print("Page navigation detected; restoring…")
                    driver.get(url); time.sleep(5); _js_top(driver); break

                try:
                    tnodes = el.find_elements(By.XPATH, ".//time[@datetime]")
                    if not tnodes: 
                        continue
                    tweet_iso = tnodes[0].get_attribute("datetime")
                    if not tweet_iso:
                        continue
                    tweet_dt = datetime.fromisoformat(tweet_iso.replace("Z","+00:00"))
                except Exception:
                    continue

                if tweet_dt < cutoff_date:
                    old_this_scroll += 1
                    continue

                try:
                    imgs = el.find_elements(By.XPATH, './/div[@data-testid="tweetPhoto"]//img[@src]')
                    img_urls = [img.get_attribute("src") for img in imgs]
                except Exception:
                    img_urls = []

                key = (final_text, tweet_dt.isoformat())
                if key not in seen_tweets:
                    seen_tweets.add(key)
                    tweets.append(final_text)
                    tweet_dates.append(tweet_dt)
                    tweet_images.append(img_urls)
                    added_this_scroll += 1

                    disp = final_text[:150] + "…" if len(final_text) > 150 else final_text
                    print(f"{username} {tweet_dt.date()} [{len(final_text)}]: {disp}")

            except Exception as e:
                print(f"Element error {i}: {e}")
                if not _is_on_user_timeline(driver, username):
                    print("Lost the page; restoring…")
                    driver.get(url); time.sleep(5); _js_top(driver); break
                continue

        old_tweets_total += old_this_scroll
        if old_this_scroll:
            print(f"Old this scroll: {old_this_scroll}  (total old: {old_tweets_total})")

        if old_tweets_total > 15 and added_this_scroll == 0:
            print("Too many old tweets and no new ones; stopping.")
            break

        if added_this_scroll == 0:
            consecutive_no_new += 1
            print(f"No new tweets ({consecutive_no_new}/3)")
        else:
            consecutive_no_new = 0
            print(f"Added {added_this_scroll} new tweets this scroll")

        try:
            print("Scrolling (anchor)…")
            elements = driver.find_elements(By.XPATH, '//article[@role="article"]')
            _anchor_scroll_to_last(driver, elements)
            if not _is_on_user_timeline(driver, username):
                print("Navigation detected post-scroll; restoring…")
                driver.get(url); time.sleep(5); _js_top(driver); _wait_first_article(driver, timeout=15); continue
        except Exception as e:
            print(f"Scroll error: {e}")

        scrolls += 1
        time.sleep(random.uniform(2,4))

    print(f"Collected {len(tweets)} tweets for {username}")
    if old_tweets_total:
        print(f"  Skipped {old_tweets_total} tweets older than {max_days} days")
    return tweets, tweet_dates, tweet_images

def append_to_csv(username, tweets, tweet_dates, tweet_images, csv_path):
    if not tweets:
        print(f"No tweets to append for {username}")
        return
    user_df = pd.DataFrame(
        {"username":[username]*len(tweets), "tweet":tweets, "tweet_time":tweet_dates, "images":tweet_images}
    )
    if os.path.exists(csv_path):
        try:
            existing = pd.read_csv(csv_path)
            combined = pd.concat([existing, user_df], ignore_index=True)
        except Exception as e:
            print(f"Error reading existing CSV: {e}")
            combined = user_df
    else:
        combined = user_df
    try:
        combined.to_csv(csv_path, index=False)
        print(f"✓ Appended {len(tweets)} tweets for {username} → {csv_path}")
    except Exception as e:
        print(f"Error saving CSV: {e}")

def scrape_multiple_users(usernames, driver, output_csv, max_days=7, max_scrolls=20):
    ok_users, bad_users = [], []
    for i, username in enumerate(usernames):
        print("\n" + "="*50)
        print(f"Scraping {username}… ({i+1}/{len(usernames)})")
        print("="*50)

        if i > 0:
            wait_s = random.uniform(45, 90)
            print(f"Cooling down {wait_s:.1f}s before next user…")
            time.sleep(wait_s)

        try:
            tweets, dates, images = get_tweets_for_user(username, driver, max_days=max_days, max_scrolls=max_scrolls)
            if tweets:
                append_to_csv(username, tweets, dates, images, output_csv)
                ok_users.append(username)
            else:
                bad_users.append(username)
        except Exception as e:
            print(f"User {username} error: {e}")
            bad_users.append(username)
            print("Waiting 90s due to error…")
            time.sleep(90)

    print("\n" + "="*50)
    print("SCRAPING SUMMARY")
    print("="*50)
    print(f"Successful: {len(ok_users)}  |  Failed: {len(bad_users)}")
    if ok_users: print("OK:", ", ".join(ok_users))
    if bad_users: print("Failed:", ", ".join(bad_users))

def create_driver():
    from selenium.webdriver.chrome.options import Options
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver

# PROCESS (with telemetry)

def process_tweets_complete(batch_size: int = 20):
    """
    Load raw CSV, label with cache + rules + batched LLM, normalize tickers,
    filter NOISE (keep MARKET), and write final CSV.
    Also prints LABEL_STATS telemetry at the end.
    """
    if not os.path.exists(INPUT_CSV_PATH):
        print(f"Raw CSV not found at {INPUT_CSV_PATH}")
        return pd.DataFrame(columns=["username","tweet","tweet_time","images","ticker","sentiment"])

    raw_df = pd.read_csv(INPUT_CSV_PATH)
    print(f"Loaded {len(raw_df)} tweets from {os.path.basename(INPUT_CSV_PATH)}")

    for col in ["username","tweet","tweet_time"]:
        if col not in raw_df.columns:
            raise RuntimeError(f"Raw CSV missing required column: {col}")
    if "images" not in raw_df.columns:
        raw_df["images"] = "[]"

    con = _label_cache_connect()

    cache_hits = 0
    llm_candidates: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []

    # Stage 1: cache + cheap rules
    for i, row in raw_df.iterrows():
        tweet = str(row["tweet"] or "")
        images = row.get("images","[]")
        thash = _stable_tweet_hash(tweet, images)

        cached = _label_cache_get(con, thash)
        if cached:
            cache_hits += 1
            ticker, sentiment = cached
            records.append({"i":i, "ticker":ticker, "sentiment":sentiment})
            continue

        tk = _cheap_ticker(tweet)
        st = _cheap_sentiment(tweet)

        if tk:
            if st is None:
                llm_candidates.append({"id": str(i), "tweet": tweet})
            else:
                _label_cache_put(con, thash, tk, st)
                records.append({"i":i,"ticker":tk,"sentiment":st})
        else:
            llm_candidates.append({"id": str(i), "tweet": tweet})

    tokens_in_total = 0
    tokens_out_total = 0
    batches = 0

    for start in range(0, len(llm_candidates), batch_size):
        chunk = llm_candidates[start:start+batch_size]
        if not chunk:
            continue
        labels, tin, tout = llm_batch_label(chunk)
        tokens_in_total  += tin
        tokens_out_total += tout
        batches += 1

        for item in chunk:
            idx = int(item["id"])
            tweet = str(raw_df.loc[idx,"tweet"] or "")
            images = raw_df.loc[idx,"images"]
            thash = _stable_tweet_hash(tweet, images)

            res = labels.get(item["id"], {"ticker":"NOISE","sentiment":"neutral"})
            ticker = res.get("ticker","NOISE").upper()
            sentiment = res.get("sentiment","neutral")
            if ticker not in ("MARKET","NOISE"):
                ticker = normalize_ticker(ticker)
            if sentiment not in ("bullish","bearish","neutral"):
                sentiment = "neutral"

            _label_cache_put(con, thash, ticker, sentiment)
            records.append({"i":idx, "ticker":ticker, "sentiment":sentiment})

        print(f"[Label] Batched {start+len(chunk)}/{len(llm_candidates)} (tokens_in={tin}, tokens_out={tout})")

    # Merge results
    res_map = { rec["i"]: (rec["ticker"], rec["sentiment"]) for rec in records }
    labeled_rows = []
    for i, row in raw_df.iterrows():
        ticker, sentiment = res_map.get(i, ("NOISE","neutral"))
        labeled_rows.append({
            "username": row["username"],
            "tweet": row["tweet"],
            "tweet_time": row["tweet_time"],
            "images": row.get("images","[]"),
            "ticker": ticker,
            "sentiment": sentiment,
        })
    labeled_df = pd.DataFrame(labeled_rows)

    # Filter NOISE; keep MARKET
    relevant = labeled_df[labeled_df["ticker"] != "NOISE"].copy()
    relevant.to_csv(OUTPUT_CSV_PATH, index=False)
    print(f"✅ Saved {len(relevant)} labeled tweets → {OUTPUT_CSV_PATH} (dropped {len(labeled_df)-len(relevant)} NOISE)")

    # Telemetry
    total = len(raw_df)
    llm_count = len(llm_candidates)
    avg_batch = (llm_count / batches) if batches else 0.0
    hit_rate = (cache_hits / total * 100.0) if total else 0.0
    print(f"LABEL_STATS: total={total}, cache_hits={cache_hits} ({hit_rate:.1f}%), "
          f"llm_items={llm_count}, batches={batches}, avg_batch={avg_batch:.1f}, "
          f"tokens_in={tokens_in_total}, tokens_out={tokens_out_total}")

    if len(relevant):
        print("Ticker distribution (top 10):")
        for t, c in relevant["ticker"].value_counts().head(10).items():
            print(f"  {t}: {c}")
        print("Sentiment distribution:")
        for s, c in relevant["sentiment"].value_counts().items():
            pct = 100.0 * c / len(relevant)
            print(f"  {s}: {c} ({pct:.1f}%)")

    return relevant

# ENTRYPOINT 

# temporary list
DEFAULT_USERS = [
    "Bluntz_Capital","TheWhiteWhaleHL","pierre_crypt0","Tradermayne","LomahCrypto",
    "Trader_XO","trader1sz","TedPillows","crypto_goos","Crypto_Chase",
    "KeyboardMonkey3","IncomeSharks","trader_koala","galaxyBTC","AltcoinSherpa",
    "CryptoAnup","blknoiz06","lBattleRhino","TheCryptoProfes","izebel_eth",
    "CryptoCaesarTA","Ashcryptoreal","cryptorangutang",
    "Numb3rsguy_","EtherWizz_",
    "CredibleCrypto","Pentosh1","basedkarbon","DJohnson_CPA","fundstrat",
    "CryptoHayes","ThinkingBitmex","TheBootMex","BastilleBtc","JamesWynnReal",
    "JustinCBram","MissionGains","ColeGarnersTake","R89Capital","RookieXBT",
    "ChainLinkGod","not_zkole","TimeFreedomROB","G7_base_eth","defi_mochi",
    "dennis_qian","noBScrypto",
]

def _resolve_user_list() -> list[str]:
    if SCRAPE_USERS_ENV:
        parts = [p.strip() for p in SCRAPE_USERS_ENV.split(",") if p.strip()]
        if parts:
            return parts
    return DEFAULT_USERS

def run_once():
    """
    One complete run:
    1) Start Chrome, login, scrape users → data/twitter_scraping_results.csv
    2) Label with cache+rules+batched LLM (with normalization & telemetry) → data/tweets_processed_complete.csv
    3) Return processed DataFrame
    """
    users = _resolve_user_list()
    print(f"Users to scrape: {len(users)} (override with SCRAPE_USERS in .env)")

    driver = create_driver()
    try:
        if not twitter_login(driver, TWITTER_USER, TWITTER_PASS):
            print("⚠️  Twitter login failed; continuing (only labeling if raw CSV exists)…")
        else:
            scrape_multiple_users(users, driver, INPUT_CSV_PATH, max_days=3, max_scrolls=20)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return process_tweets_complete(batch_size=20)

if __name__ == "__main__":
    run_once()
