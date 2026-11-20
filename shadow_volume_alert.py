#!/usr/bin/env python3
"""
SHADOW VOLUME ALERT
===================
Telegram bot that watches trending Solana meme coin pools and alerts when the
5-minute volume of a pool jumps sharply compared with the previous 5-minute
reading.

* Free public APIs only (no keys): DexScreener + GeckoTerminal + RugCheck
* Runs on Android Termux, no VPS / website / database
* Scans automatically every 5 minutes (configurable)
* Previous readings, alert history and watchlist are saved in state.json,
  so a restart keeps the comparison history.

Usage:
    python shadow_volume_alert.py              # normal mode (runs forever)
    python shadow_volume_alert.py --once       # run a single scan and exit
    python shadow_volume_alert.py --dry-run    # never send to Telegram, print alerts
    python shadow_volume_alert.py --test-telegram   # send one test message and exit
    python shadow_volume_alert.py --selftest   # offline logic test (no network)

Telegram commands: /start /status /settings /last /pause /resume /help
Alerts include a RugCheck safety check and buy/sell pressure.
"""

import argparse
import html
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler

import requests

VERSION = "1.1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.env")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "shadow_volume_alert.log")

DEXSCREENER = "https://api.dexscreener.com"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
TELEGRAM = "https://api.telegram.org"
RUGCHECK = "https://api.rugcheck.xyz/v1"
GT_GAP = 6.5  # seconds between GeckoTerminal calls (free tier is strict)

# Base tokens that are not meme coins (SOL, stablecoins, liquid staking, majors)
NON_MEME_MINTS = {
    "So11111111111111111111111111111111111111112",   # SOL / WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",   # bSOL
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",  # PYUSD
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",   # USD1
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  # WBTC
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # WETH
}

# Name fragments of wrapped/bridged assets and tokenized stocks (not meme coins)
NON_MEME_NAME_WORDS = ("wrapped ", "securities", "xstock", "tokenized", "(wormhole)", "bridged ",
                       "(portal)", "staked sol", "liquid staking")

DEFAULTS = {
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "SCAN_INTERVAL_SECONDS": "300",
    "MIN_VOLUME_5M_USD": "10000",
    "MIN_LIQUIDITY_USD": "10000",
    "MIN_TXNS_5M": "20",
    "VOLUME_MULTIPLIER": "2.5",
    "MIN_PREVIOUS_VOLUME_USD": "100",
    "MIN_READING_GAP_SECONDS": "240",
    "MAX_READING_GAP_SECONDS": "420",
    "ALERT_COOLDOWN_MINUTES": "60",
    "WATCHLIST_HOURS": "3",
    "MAX_POOLS_PER_SCAN": "450",
    "GT_TRENDING_PAGES": "3",
    "CROSS_CHECK_GECKOTERMINAL": "true",
    "CROSS_CHECK_TOLERANCE": "0.5",
    "SEND_STARTUP_MESSAGE": "true",
    "SKIP_HIGH_RISK": "false",
    "MIN_BUY_RATIO": "0",
    "EXCLUDE_SYMBOLS": ("SOL,WSOL,USDC,USDT,PYUSD,USD1,USDS,USDG,EURC,FDUSD,DAI,MSOL,JITOSOL,BSOL,JUPSOL,INF,"
                        "BTC,WBTC,CBBTC,ZBTC,TBTC,ETH,WETH,ZEC,XRP,BNB,SUI,HYPE,LINK,DOGE,LTC,ADA,AVAX,"
                        "JUP,JLP,JTO,PYTH,RAY,ORCA,HNT,RENDER,W,DRIFT,KMNO,MET,TNSR,CLOUD,SAND,GOLD,XAUT,PAXG"),
}

log = logging.getLogger("shadow")
_running = True
STATE_LOCK = threading.RLock()  # protects the state shared by the scan loop and the command thread


# ----------------------------------------------------------------------------
# Config / logging / state
# ----------------------------------------------------------------------------
def load_config(path=CONFIG_FILE):
    """Read KEY=VALUE lines from config.env. Environment variables override."""
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                cfg[key.strip().upper()] = value.strip().strip('"').strip("'")
    for key in DEFAULTS:
        if os.environ.get(key):
            cfg[key] = os.environ[key]

    def num(key, cast=float):
        try:
            return cast(cfg[key])
        except (TypeError, ValueError):
            log.warning("Bad value for %s=%r, using default %s", key, cfg[key], DEFAULTS[key])
            return cast(DEFAULTS[key])

    def flag(key):
        return str(cfg[key]).strip().lower() in ("1", "true", "yes", "on")

    token = cfg["TELEGRAM_BOT_TOKEN"].strip()
    chat_id = cfg["TELEGRAM_CHAT_ID"].strip()
    if token.upper().startswith("PUT_") or token.upper().startswith("YOUR_"):
        token = ""
    if chat_id.upper().startswith("PUT_") or chat_id.upper().startswith("YOUR_"):
        chat_id = ""
    return {
        "token": token,
        "chat_id": chat_id,
        "interval": max(60, num("SCAN_INTERVAL_SECONDS", int)),
        "min_volume": num("MIN_VOLUME_5M_USD"),
        "min_liquidity": num("MIN_LIQUIDITY_USD"),
        "min_txns": num("MIN_TXNS_5M", int),
        "multiplier": num("VOLUME_MULTIPLIER"),
        "min_prev_volume": num("MIN_PREVIOUS_VOLUME_USD"),
        "min_gap": num("MIN_READING_GAP_SECONDS", int),
        "max_gap": num("MAX_READING_GAP_SECONDS", int),
        "cooldown": num("ALERT_COOLDOWN_MINUTES", int) * 60,
        "watch_seconds": num("WATCHLIST_HOURS") * 3600,
        "max_pools": num("MAX_POOLS_PER_SCAN", int),
        "gt_pages": max(1, min(10, num("GT_TRENDING_PAGES", int))),
        "cross_check": flag("CROSS_CHECK_GECKOTERMINAL"),
        "tolerance": num("CROSS_CHECK_TOLERANCE"),
        "startup_msg": flag("SEND_STARTUP_MESSAGE"),
        "skip_high_risk": flag("SKIP_HIGH_RISK"),
        "min_buy_ratio": max(0.0, num("MIN_BUY_RATIO")),
        "exclude_symbols": {x.strip().upper() for x in cfg["EXCLUDE_SYMBOLS"].split(",") if x.strip()},
    }


def setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as exc:
        log.warning("Cannot write log file %s: %s", LOG_FILE, exc)


def empty_state():
    return {"readings": {}, "alerts": {}, "watch": {}, "alert_log": [], "paused": False, "tg_offset": 0}


def load_state(path=None):
    path = path or STATE_FILE
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = empty_state()
        for key, default in list(state.items()):
            if isinstance(data.get(key), type(default)):
                state[key] = data[key]
        return state
    except (OSError, ValueError) as exc:
        # Keep the broken file for inspection and start fresh
        backup = path + ".broken"
        try:
            os.replace(path, backup)
        except OSError:
            pass
        log.error("State file unreadable (%s). Saved as %s, starting fresh.", exc, backup)
        return empty_state()


def save_state(state, path=None):
    """Atomic write: write to a temp file then rename over the old one."""
    path = path or STATE_FILE
    directory = os.path.dirname(path) or "."
    with STATE_LOCK:
        text = json.dumps(state, separators=(",", ":"))
    fd, tmp = tempfile.mkstemp(prefix=".state_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        log.error("Could not save state: %s", exc)
        try:
            os.remove(tmp)
        except OSError:
            pass


def prune_state(state, cfg, now):
    keep_readings = max(cfg["max_gap"] * 3, 3600)
    state["readings"] = {k: v for k, v in state["readings"].items()
                         if now - v.get("ts", 0) < keep_readings}
    state["alerts"] = {k: v for k, v in state["alerts"].items()
                       if now - v < max(cfg["cooldown"], 3600) * 2}
    state["watch"] = {k: v for k, v in state["watch"].items()
                      if now - v < cfg["watch_seconds"]}


# ----------------------------------------------------------------------------
# HTTP with retries / rate limit handling
# ----------------------------------------------------------------------------
class Http:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "ShadowVolumeAlert/%s (+termux)" % VERSION,
        })
        self.last_call = {}

    def _pace(self, host, min_gap):
        wait = self.last_call.get(host, 0) + min_gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last_call[host] = time.time()

    def get_json(self, url, params=None, min_gap=0.25, retries=4):
        """GET a JSON document. Returns None on permanent failure (never raises)."""
        host = url.split("/")[2]
        backoff = 5
        for attempt in range(1, retries + 1):
            if not _running:
                return None
            self._pace(host, min_gap)
            try:
                resp = self.session.get(url, params=params, timeout=20)
            except requests.RequestException as exc:
                log.warning("Network error %s (try %d/%d): %s", host, attempt, retries, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    log.warning("Invalid JSON from %s (try %d/%d)", host, attempt, retries)
            elif resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else backoff * 3
                wait = min(max(wait, 10), 120)
                log.warning("Rate limited by %s, waiting %ss (try %d/%d)", host, wait, attempt, retries)
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue
            elif resp.status_code in (400, 401, 403, 404):
                log.warning("HTTP %s from %s for %s", resp.status_code, host, url[:120])
                return None
            else:
                log.warning("HTTP %s from %s (try %d/%d)", resp.status_code, host, attempt, retries)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        log.error("Giving up on %s after %d tries", url[:120], retries)
        return None


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def to_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------------
# Data sources
# ----------------------------------------------------------------------------
def gt_trending_pool_addresses(http, pages):
    """GeckoTerminal trending + new Solana pools -> pool addresses."""
    found = []
    queries = [("trending_pools", {"page": p, "duration": "5m"}) for p in range(1, pages + 1)]
    queries.append(("trending_pools", {"page": 1, "duration": "1h"}))
    queries.append(("new_pools", {"page": 1}))
    for path, params in queries:
        data = http.get_json("%s/networks/solana/%s" % (GECKOTERMINAL, path), params=params, min_gap=GT_GAP)
        if not data or not isinstance(data.get("data"), list):
            continue
        for item in data["data"]:
            addr = (item.get("attributes") or {}).get("address")
            if addr:
                found.append(addr)
    return found


def ds_promoted_token_addresses(http):
    """DexScreener latest boosts, top boosts and latest profiles -> Solana token mints."""
    tokens = []
    for path in ("/token-boosts/latest/v1", "/token-boosts/top/v1", "/token-profiles/latest/v1"):
        data = http.get_json(DEXSCREENER + path, min_gap=1.1)
        if not isinstance(data, list):
            continue
        for item in data:
            if isinstance(item, dict) and item.get("chainId") == "solana" and item.get("tokenAddress"):
                tokens.append(item["tokenAddress"])
    return list(dict.fromkeys(tokens))


def ds_best_pairs_for_tokens(http, token_addresses, min_liquidity):
    """For each token mint, pick its most liquid Solana pair address."""
    best = {}
    for batch in chunks(token_addresses, 30):
        data = http.get_json("%s/tokens/v1/solana/%s" % (DEXSCREENER, ",".join(batch)), min_gap=0.25)
        if not isinstance(data, list):
            continue
        for pair in data:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                continue
            mint = (pair.get("baseToken") or {}).get("address")
            liq = to_float((pair.get("liquidity") or {}).get("usd"))
            if not mint or not pair.get("pairAddress") or liq < min_liquidity * 0.5:
                continue
            if mint not in best or liq > best[mint][0]:
                best[mint] = (liq, pair["pairAddress"])
    return [v[1] for v in best.values()]


def ds_fetch_pairs(http, pair_addresses):
    """Fetch full DexScreener pair data. Returns {pairAddress: pair_dict}."""
    result = {}
    for batch in chunks(pair_addresses, 30):
        data = http.get_json("%s/latest/dex/pairs/solana/%s" % (DEXSCREENER, ",".join(batch)), min_gap=0.25)
        if not isinstance(data, dict):
            continue
        for pair in data.get("pairs") or []:
            if isinstance(pair, dict) and pair.get("pairAddress") and pair.get("chainId") == "solana":
                result[pair["pairAddress"]] = pair
    return result


def gt_fetch_pools(http, pool_addresses):
    """GeckoTerminal pool data for cross-checking. Returns {address: attributes} or None on API failure."""
    result = {}
    for batch in chunks(pool_addresses, 30):
        data = http.get_json("%s/networks/solana/pools/multi/%s" % (GECKOTERMINAL, ",".join(batch)), min_gap=GT_GAP)
        if data is None or not isinstance(data.get("data"), list):
            return None
        for item in data["data"]:
            attrs = item.get("attributes") or {}
            if attrs.get("address"):
                result[attrs["address"]] = attrs
    return result


# ----------------------------------------------------------------------------
# Signal logic
# ----------------------------------------------------------------------------
def extract_metrics(pair):
    txns = (pair.get("txns") or {}).get("m5") or {}
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    return {
        "pair": pair.get("pairAddress", ""),
        "mint": base.get("address", ""),
        "name": base.get("name") or "?",
        "symbol": base.get("symbol") or "?",
        "quote": quote.get("symbol") or "?",
        "dex": pair.get("dexId") or "?",
        "vol5m": to_float((pair.get("volume") or {}).get("m5")),
        "vol1h": to_float((pair.get("volume") or {}).get("h1")),
        "liquidity": to_float((pair.get("liquidity") or {}).get("usd")),
        "buys": int(to_float(txns.get("buys"))),
        "sells": int(to_float(txns.get("sells"))),
        "price": pair.get("priceUsd"),
        "change5m": (pair.get("priceChange") or {}).get("m5"),
        "change1h": (pair.get("priceChange") or {}).get("h1"),
        "mcap": to_float(pair.get("marketCap") or pair.get("fdv")),
        "created": pair.get("pairCreatedAt"),
        "url": pair.get("url") or "https://dexscreener.com/solana/%s" % pair.get("pairAddress", ""),
    }


def evaluate(m, prev, cfg, now):
    """Return (passes, reason, multiplier). prev is the stored reading dict or None."""
    if (m["mint"] in NON_MEME_MINTS or m["symbol"].strip().upper() in cfg["exclude_symbols"]
            or any(word in m["name"].lower() for word in NON_MEME_NAME_WORDS)):
        return False, "not a meme coin", 0.0
    txns = m["buys"] + m["sells"]
    if m["liquidity"] < cfg["min_liquidity"]:
        return False, "liquidity %.0f < %.0f" % (m["liquidity"], cfg["min_liquidity"]), 0.0
    if m["vol5m"] < cfg["min_volume"]:
        return False, "5m volume %.0f < %.0f" % (m["vol5m"], cfg["min_volume"]), 0.0
    if txns < cfg["min_txns"]:
        return False, "5m txns %d < %d" % (txns, cfg["min_txns"]), 0.0
    if cfg.get("min_buy_ratio", 0) > 0 and m["buys"] < cfg["min_buy_ratio"] * max(m["sells"], 1):
        return False, "buy/sell ratio below %.2f" % cfg["min_buy_ratio"], 0.0
    if not prev:
        return False, "no previous reading yet (baseline)", 0.0
    gap = now - prev.get("ts", 0)
    if gap < cfg["min_gap"] or gap > cfg["max_gap"]:
        return False, "previous reading %ds old (allowed %d-%ds)" % (gap, cfg["min_gap"], cfg["max_gap"]), 0.0
    prev_vol = to_float(prev.get("vol5m"))
    if prev_vol <= 0 or prev_vol < cfg["min_prev_volume"]:
        return False, "previous volume %.0f too small to compare" % prev_vol, 0.0
    mult = m["vol5m"] / prev_vol
    if mult < cfg["multiplier"]:
        return False, "increase %.2fx < %.2fx" % (mult, cfg["multiplier"]), mult
    return True, "volume spike %.2fx" % mult, mult


def cross_check_gt(m, gt_attrs, cfg):
    """Independent confirmation from GeckoTerminal for the same pool."""
    if not gt_attrs:
        return False, "pool not found on GeckoTerminal"
    tol = cfg["tolerance"]
    gt_vol = to_float((gt_attrs.get("volume_usd") or {}).get("m5"))
    gt_liq = to_float(gt_attrs.get("reserve_in_usd"))
    tx = (gt_attrs.get("transactions") or {}).get("m5") or {}
    gt_txns = int(to_float(tx.get("buys"))) + int(to_float(tx.get("sells")))
    if gt_vol < cfg["min_volume"] * tol:
        return False, "GeckoTerminal 5m volume only $%.0f" % gt_vol
    if gt_liq < cfg["min_liquidity"] * tol:
        return False, "GeckoTerminal liquidity only $%.0f" % gt_liq
    if gt_txns < cfg["min_txns"] * tol:
        return False, "GeckoTerminal 5m txns only %d" % gt_txns
    return True, "GT 5m vol $%s, liq $%s, txns %d" % (fmt_usd(gt_vol), fmt_usd(gt_liq), gt_txns)


# ----------------------------------------------------------------------------
# Formatting / Telegram
# ----------------------------------------------------------------------------
def fmt_usd(value):
    value = to_float(value)
    if value >= 1_000_000_000:
        return "%.2fB" % (value / 1e9)
    if value >= 1_000_000:
        return "%.2fM" % (value / 1e6)
    if value >= 1_000:
        return "%.1fK" % (value / 1e3)
    return "%.0f" % value


def fmt_price(price):
    p = to_float(price)
    if p <= 0:
        return "n/a"
    if p >= 1:
        return "$%.4f" % p
    return "$%.10f" % p if p < 0.0001 else "$%.6f" % p


def fmt_pct(value):
    return "%+.2f%%" % to_float(value) if value is not None else "n/a"


def build_alert(m, prev_vol, mult, gt_note, safety=None):
    e = html.escape
    buys, sells = m["buys"], m["sells"]
    total = buys + sells
    buy_pct = (buys * 100.0 / total) if total else 0
    ratio, pressure = buy_pressure(buys, sells)
    age = ""
    if m["created"]:
        hours = (time.time() * 1000 - to_float(m["created"])) / 3_600_000
        age = "%.1fh" % hours if hours < 48 else "%.0fd" % (hours / 24)
    lines = [
        "🚨🚨 <b>SHADOW VOLUME ALERT</b> 🚨🚨",
        "",
        "🪙 <b>%s</b> ($%s) / %s" % (e(m["name"]), e(m["symbol"]), e(m["quote"])),
        "🔥 <b>5m volume: $%s</b>  (<b>%.2fx</b> vs previous $%s)" % (fmt_usd(m["vol5m"]), mult, fmt_usd(prev_vol)),
        "💧 Liquidity: $%s" % fmt_usd(m["liquidity"]),
        "🟢 Buys (5m): %d   🔴 Sells (5m): %d   (%.0f%% buys)" % (buys, sells, buy_pct),
        "⚖️ Buy/sell ratio: %s  %s" % ("%.2f" % ratio if ratio is not None else ("∞" if buys else "n/a"), pressure),
        "💵 Price: %s   5m: %s   1h: %s" % (fmt_price(m["price"]), fmt_pct(m["change5m"]), fmt_pct(m["change1h"])),
    ]
    if m["mcap"] >= 1000:
        lines.append("🏦 Market cap: $%s" % fmt_usd(m["mcap"]))
    lines.append("📊 1h volume: $%s   DEX: %s%s" % (fmt_usd(m["vol1h"]), e(m["dex"]),
                                                  ("   Age: %s" % age) if age else ""))
    lines += safety_lines(safety, m["mint"])
    lines += [
        "✅ Verified: %s" % e(gt_note),
        "",
        "🧾 Mint: <code>%s</code>" % e(m["mint"]),
        "🔗 Pair: <code>%s</code>" % e(m["pair"]),
        '📈 <a href="%s">DexScreener chart</a> | <a href="https://www.geckoterminal.com/solana/pools/%s">GeckoTerminal</a>'
        % (e(m["url"], quote=True), e(m["pair"], quote=True)),
        "",
        "<i>Data signal only, not financial advice. Meme coins are high risk.</i>",
    ]
    return "\n".join(lines)


class Telegram:
    def __init__(self, token, chat_id, dry_run, session=None):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run or not token or not chat_id
        # Each thread creates its own Telegram object, so sessions are never shared between threads
        self.session = session or requests.Session()

    def _url(self, method):
        return "%s/bot%s/%s" % (TELEGRAM, self.token, method)

    def check(self):
        """Verify the bot token. Returns username, "" if the token is rejected, None on network error."""
        try:
            r = self.session.get(self._url("getMe"), timeout=20)
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            log.error("Cannot reach Telegram: %s", exc)
            return None
        if not data.get("ok"):
            log.error("Telegram token rejected: %s", data.get("description"))
            return "" if r.status_code in (401, 404) else None
        return data["result"].get("username") or "bot"

    def call(self, method, payload, timeout=20):
        """Call a Telegram method once. Returns (http_status, json); status 0 means network error."""
        try:
            r = self.session.post(self._url(method), json=payload, timeout=timeout)
            return r.status_code, r.json()
        except (requests.RequestException, ValueError) as exc:
            return 0, {"ok": False, "description": str(exc)}

    def send(self, text, chat_id=None, reply_to=None):
        """Send an HTML message. Returns True on success."""
        if self.dry_run:
            log.info("[DRY-RUN] Telegram message:\n%s", text)
            return True
        payload = {"chat_id": chat_id or self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
            payload["allow_sending_without_reply"] = True
        for attempt in range(1, 6):
            status, data = self.call("sendMessage", payload)
            if data.get("ok"):
                return True
            if status == 0:
                log.warning("Telegram send error (try %d/5): %s", attempt, data.get("description"))
                time.sleep(5 * attempt)
                continue
            if status == 429:
                wait = int((data.get("parameters") or {}).get("retry_after", 10)) + 1
                log.warning("Telegram flood limit, waiting %ss", wait)
                time.sleep(min(wait, 300))
                continue
            if status in (400, 401, 403):
                log.error("Telegram refused message (%s): %s. Check TELEGRAM_CHAT_ID and that "
                          "the bot can post there.", status, data.get("description"))
                return False
            log.warning("Telegram error %s (try %d/5): %s", status, attempt, data.get("description"))
            time.sleep(5 * attempt)
        return False

    def setup_commands(self):
        """Remove any webhook (it blocks getUpdates) and register the command menu."""
        status, data = self.call("deleteWebhook", {"drop_pending_updates": False})
        if not data.get("ok"):
            log.warning("deleteWebhook failed (%s): %s", status, data.get("description"))
        commands = [{"command": c, "description": d} for c, d in BOT_COMMANDS]
        status, data = self.call("setMyCommands", {"commands": commands})
        if data.get("ok"):
            log.info("Telegram command menu registered (%d commands)", len(commands))
        else:
            log.warning("setMyCommands failed (%s): %s", status, data.get("description"))
        return bool(data.get("ok"))


# ----------------------------------------------------------------------------
# Telegram commands
# ----------------------------------------------------------------------------
BOT_COMMANDS = [
    ("start", "Welcome and alert rules"),
    ("status", "Uptime, last scan, alerts today"),
    ("settings", "Current alert thresholds"),
    ("last", "Last 5 alerts"),
    ("pause", "Pause alerts (scanning continues)"),
    ("resume", "Resume alerts"),
    ("help", "List of commands"),
]

# Live counters shared by the scan loop and the command thread (not saved to disk)
RUNTIME = {"started": time.time(), "scans": 0, "last_scan": 0.0, "last_scan_pools": 0,
           "next_scan": 0.0, "scanning": False}


def fmt_time(ts):
    if not ts:
        return "never"
    zone = time.strftime("%Z", time.localtime(ts))
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) + (" " + zone if zone else "")


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return "%dd %dh %dm" % (days, hours, minutes)
    if hours:
        return "%dh %dm" % (hours, minutes)
    if minutes:
        return "%dm %ds" % (minutes, secs) if secs else "%dm" % minutes
    return "%ds" % secs


def rules_text(cfg):
    lines = ["• 5m volume ≥ <b>$%s</b>" % fmt_usd(cfg["min_volume"]),
             "• Liquidity ≥ <b>$%s</b>" % fmt_usd(cfg["min_liquidity"]),
             "• 5m transactions ≥ <b>%d</b>" % cfg["min_txns"],
             "• Volume spike ≥ <b>%.2fx</b> vs previous 5m reading" % cfg["multiplier"]]
    if cfg["min_buy_ratio"] > 0:
        lines.append("• Buy/sell ratio ≥ <b>%.2f</b>" % cfg["min_buy_ratio"])
    if cfg["skip_high_risk"]:
        lines.append("• High-risk tokens (RugCheck) skipped")
    lines.append("• Scan every <b>%s</b>" % fmt_duration(cfg["interval"]))
    return "\n".join(lines)


def alerts_today(state, now=None):
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    return sum(1 for a in state.get("alert_log", [])
               if time.strftime("%Y-%m-%d", time.localtime(a.get("ts", 0))) == today)


def handle_command(text, cfg, state, runtime=None, now=None):
    """Return the HTML reply for a command, or None if the text is not a command."""
    runtime = RUNTIME if runtime is None else runtime
    now = now or time.time()
    if not text or not text.startswith("/"):
        return None
    cmd = text.split()[0][1:].split("@")[0].lower()
    e = html.escape
    paused_txt = "PAUSED ⏸" if state.get("paused") else "ACTIVE ▶️"
    if cmd == "start":
        return ("👋 <b>SHADOW VOLUME ALERT</b>\n"
                "I scan trending Solana meme coin pools and alert you when 5-minute volume suddenly jumps. "
                "Every alert is re-checked on DexScreener, confirmed by GeckoTerminal and safety-checked "
                "with RugCheck.\n\n<b>Alert rules</b>\n%s\n\nAlerts are <b>%s</b>. Send /help for commands."
                % (rules_text(cfg), paused_txt))
    if cmd == "help":
        return "<b>Commands</b>\n" + "\n".join("/%s - %s" % (c, e(d)) for c, d in BOT_COMMANDS)
    if cmd == "settings":
        return ("⚙️ <b>Settings</b>\n%s\n• Min previous 5m volume: $%s\n• Previous reading age: %s to %s\n"
                "• Cooldown per coin: %s\n• Watchlist: %s\n• GeckoTerminal cross-check: %s (tolerance %.0f%%)\n"
                "• Skip high-risk tokens: %s\n• Min buy/sell ratio: %s\n• Max pools per scan: %d\n\n"
                "<i>Change these in config.env, then restart the bot.</i>"
                % (rules_text(cfg), fmt_usd(cfg["min_prev_volume"]), fmt_duration(cfg["min_gap"]),
                   fmt_duration(cfg["max_gap"]), fmt_duration(cfg["cooldown"]),
                   fmt_duration(cfg["watch_seconds"]), "on" if cfg["cross_check"] else "off",
                   cfg["tolerance"] * 100, "yes" if cfg["skip_high_risk"] else "no",
                   ("%.2f" % cfg["min_buy_ratio"]) if cfg["min_buy_ratio"] > 0 else "off", cfg["max_pools"]))
    if cmd == "status":
        alert_log = state.get("alert_log", [])
        last = alert_log[-1] if alert_log else None
        if runtime.get("scanning"):
            next_txt = "scanning now"
        elif runtime.get("next_scan"):
            next_txt = "in " + fmt_duration(runtime["next_scan"] - now)
        else:
            next_txt = "soon"
        last_txt = ("%s %.2fx at %s" % (e(last.get("symbol", "?")), last.get("mult", 0), fmt_time(last.get("ts")))
                    if last else "none yet")
        return ("📡 <b>Status</b>\n• Alerts: <b>%s</b>\n• Uptime: %s\n• Scans done: %d\n"
                "• Last scan: %s (%d pools read)\n• Next scan: %s\n• Pools tracked: %d\n"
                "• Alerts today: %d\n• Last alert: %s"
                % (paused_txt, fmt_duration(now - runtime.get("started", now)), runtime.get("scans", 0),
                   fmt_time(runtime.get("last_scan")), runtime.get("last_scan_pools", 0), next_txt,
                   len(state.get("readings", {})), alerts_today(state, now), last_txt))
    if cmd == "last":
        recent = state.get("alert_log", [])[-5:]
        if not recent:
            return "No alerts yet."
        lines = ["🧾 <b>Last %d alerts</b>" % len(recent)]
        for a in reversed(recent):
            lines.append('• %s\n  <b>%s</b> ($%s) %.2fx, 5m vol $%s, liq $%s, risk %s - <a href="%s">chart</a>'
                         % (fmt_time(a.get("ts")), e(a.get("name", "?")), e(a.get("symbol", "?")),
                            a.get("mult", 0), fmt_usd(a.get("vol5m")), fmt_usd(a.get("liquidity")),
                            e(a.get("risk") or "n/a"), e(a.get("url", ""), quote=True)))
        return "\n".join(lines)
    if cmd in ("pause", "resume"):
        with STATE_LOCK:
            was_paused = bool(state.get("paused"))
            state["paused"] = cmd == "pause"
        save_state(state)
        if cmd == "pause":
            return ("⏸ Alerts were already paused." if was_paused else
                    "⏸ Alerts <b>paused</b>. Scanning continues so history stays fresh. Send /resume to restart.")
        return "▶️ Alerts <b>resumed</b>." if was_paused else "▶️ Alerts are already active."
    return "Unknown command. Send /help for the list."


def is_authorized(chat, cfg_chat_id):
    """Only the configured chat may use commands (numeric id or @channelusername)."""
    if not chat:
        return False
    wanted = str(cfg_chat_id).strip()
    if str(chat.get("id")) == wanted:
        return True
    username = chat.get("username")
    return bool(username) and wanted.startswith("@") and wanted[1:].lower() == username.lower()


def process_updates(tg, updates, cfg, state, started_at):
    """Handle a list of Telegram updates. Returns the next offset, or None if there were none."""
    next_offset = None
    for upd in updates:
        uid = upd.get("update_id")
        if isinstance(uid, int):
            next_offset = max(next_offset or 0, uid + 1)
        msg = upd.get("message") or upd.get("channel_post")
        if not msg or not isinstance(msg.get("text"), str) or not msg["text"].startswith("/"):
            continue
        chat = msg.get("chat") or {}
        if not is_authorized(chat, cfg["chat_id"]):
            log.info("Ignored command from unauthorized chat %s", chat.get("id"))
            continue
        if msg.get("date", 0) < started_at - 600:
            log.info("Ignored old command %r (sent while the bot was off)", msg["text"][:30])
            continue
        try:
            reply = handle_command(msg["text"].strip(), cfg, state)
        except Exception:
            log.exception("Command handler error")
            reply = "⚠️ Sorry, something went wrong with that command."
        if reply:
            log.info("Command %s from chat %s", msg["text"].split()[0][:30], chat.get("id"))
            tg.send(reply, chat_id=chat.get("id"), reply_to=msg.get("message_id"))
    return next_offset


def poll_once(tg, cfg, state, started_at, timeout=25):
    """One getUpdates long-poll. Returns how many seconds to wait before polling again."""
    with STATE_LOCK:
        offset = int(state.get("tg_offset") or 0)
    payload = {"timeout": timeout, "allowed_updates": ["message", "channel_post"]}
    if offset:
        payload["offset"] = offset
    status, data = tg.call("getUpdates", payload, timeout=timeout + 15)
    if data.get("ok"):
        result = data.get("result")
        new_offset = process_updates(tg, result if isinstance(result, list) else [], cfg, state, started_at)
        if new_offset:
            with STATE_LOCK:
                state["tg_offset"] = new_offset
            save_state(state)
        return 0
    if status == 429:
        wait = int((data.get("parameters") or {}).get("retry_after", 10)) + 1
        log.warning("Telegram getUpdates flood limit, waiting %ss", wait)
        return min(wait, 300)
    if status == 409:
        log.warning("getUpdates conflict (%s). Removing webhook; is another copy of the bot running?",
                    data.get("description"))
        tg.call("deleteWebhook", {"drop_pending_updates": False})
        return 30
    if status in (401, 404):
        log.error("getUpdates unauthorized: %s", data.get("description"))
        return 300
    log.warning("getUpdates failed (%s): %s", status or "network", data.get("description"))
    return 10


def command_listener(cfg, state):
    """Background thread: answers commands within seconds and never blocks scanning."""
    tg = Telegram(cfg["token"], cfg["chat_id"], False)
    started_at = time.time()
    tg.setup_commands()
    log.info("Command listener started (send /help in Telegram)")
    while _running:
        try:
            wait = poll_once(tg, cfg, state, started_at)
        except Exception:
            log.exception("Command listener error")
            wait = 10
        end = time.time() + wait
        while _running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))


# ----------------------------------------------------------------------------
# Safety (RugCheck) and buy pressure
# ----------------------------------------------------------------------------
def rugcheck_report(http, mint, pair_address=None):
    """Free RugCheck report -> short safety dict, or None if the API is unavailable."""
    data = http.get_json("%s/tokens/%s/report" % (RUGCHECK, mint), min_gap=1.5, retries=2)
    if not isinstance(data, dict) or "risks" not in data:
        return None
    token = data.get("token") or {}
    mint_auth = data.get("mintAuthority", token.get("mintAuthority"))
    freeze_auth = data.get("freezeAuthority", token.get("freezeAuthority"))
    risks, seen = [], set()
    for r in sorted(data.get("risks") or [], key=lambda x: to_float(x.get("score")), reverse=True):
        name = r.get("name") or "?"
        if name in seen:
            continue
        seen.add(name)
        risks.append({"name": name, "level": r.get("level") or "", "value": r.get("value") or ""})
    levels = {r["level"] for r in risks}
    if data.get("rugged"):
        level = "RUGGED"
    elif "danger" in levels or mint_auth or freeze_auth:
        level = "HIGH"
    elif "warn" in levels:
        level = "MEDIUM"
    else:
        level = "LOW"
    lp_locked = None
    for market in data.get("markets") or []:
        if pair_address and market.get("pubkey") == pair_address:
            lp_locked = (market.get("lp") or {}).get("lpLockedPct")
    return {"score": data.get("score_normalised"), "level": level, "mint_authority": mint_auth,
            "freeze_authority": freeze_auth, "risks": risks, "lp_locked": lp_locked,
            "holders": data.get("totalHolders")}


def is_high_risk(safety):
    return bool(safety) and safety["level"] in ("HIGH", "RUGGED")


def safety_lines(safety, mint):
    e = html.escape
    link = '<a href="https://rugcheck.xyz/tokens/%s">RugCheck</a>' % e(mint, quote=True)
    if not safety:
        return ["🛡 Safety: unavailable (RugCheck did not answer) | %s" % link]
    icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "RUGGED": "☠️"}[safety["level"]]
    score = safety["score"]
    lines = ["🛡 Safety: %s <b>%s risk</b>%s | %s" % (icon, safety["level"],
                                                   " (score %s)" % e(str(score)) if score is not None else "", link),
             "   Mint authority: %s   Freeze authority: %s" % (
                 "⚠️ ENABLED" if safety["mint_authority"] else "✅ revoked",
                 "⚠️ ENABLED" if safety["freeze_authority"] else "✅ revoked")]
    extra = []
    if safety["lp_locked"] is not None:
        extra.append("LP locked (this pool): %.0f%%" % to_float(safety["lp_locked"]))
    if safety["holders"]:
        extra.append("Holders: %s" % "{:,}".format(int(to_float(safety["holders"]))))
    if extra:
        lines.append("   " + "   ".join(extra))
    if safety["risks"]:
        top = ["%s%s" % (r["name"], " (%s)" % r["value"] if r["value"] else "") for r in safety["risks"][:3]]
        lines.append("   Top risks: %s" % e("; ".join(top)))
    else:
        lines.append("   Top risks: none reported")
    return lines


def buy_pressure(buys, sells):
    """Return (ratio, label). ratio is buys/sells (None if there were no sells)."""
    if sells <= 0:
        return (None, "🟢 Strong buy pressure (no sells)") if buys > 0 else (None, "⚪ No trades")
    ratio = buys / float(sells)
    if ratio >= 1.5:
        label = "🟢 Strong buy pressure"
    elif ratio >= 1.1:
        label = "🟢 Buy pressure"
    elif ratio > 1 / 1.1:
        label = "⚪ Balanced"
    elif ratio > 1 / 1.5:
        label = "🔴 Sell pressure"
    else:
        label = "🔴 Strong sell pressure"
    return ratio, label


# ----------------------------------------------------------------------------
# Scan cycle
# ----------------------------------------------------------------------------
def record_alert(state, m, mult, now, safety):
    with STATE_LOCK:
        state["alerts"]["pair:" + m["pair"]] = now
        state["alerts"]["mint:" + m["mint"]] = now
        state["alert_log"].append({"ts": now, "name": m["name"], "symbol": m["symbol"], "mult": round(mult, 2),
                                   "vol5m": m["vol5m"], "liquidity": m["liquidity"], "pair": m["pair"],
                                   "url": m["url"], "risk": safety["level"] if safety else "unavailable"})
        state["alert_log"] = state["alert_log"][-50:]


def run_scan(http, tg, state, cfg):
    started = time.time()
    # 1) Discover candidate pools
    gt_pools = gt_trending_pool_addresses(http, cfg["gt_pages"])
    promoted = ds_promoted_token_addresses(http)
    ds_pools = ds_best_pairs_for_tokens(http, promoted, cfg["min_liquidity"])
    with STATE_LOCK:
        watched = sorted(state["watch"], key=lambda k: state["watch"][k], reverse=True)
    candidates = list(dict.fromkeys(gt_pools + ds_pools + watched))[:cfg["max_pools"]]
    log.info("Discovery: %d GeckoTerminal pools, %d DexScreener promoted tokens -> %d pairs, "
             "%d watchlist -> %d unique pools", len(set(gt_pools)), len(promoted), len(ds_pools),
             len(watched), len(candidates))
    if not candidates:
        log.warning("No pools discovered this cycle (APIs down?). Will retry next cycle.")
        return {"scanned": 0, "alerts": 0}

    # 2) Read all pools from one consistent source (DexScreener)
    pairs = ds_fetch_pairs(http, candidates)
    now = time.time()
    fresh_new = set(gt_pools + ds_pools)
    hits, near = [], []
    stats = {"scanned": len(pairs), "eligible": 0, "baseline": 0, "alerts": 0}
    with STATE_LOCK:
        for addr in candidates:
            if addr in fresh_new:
                state["watch"][addr] = now
        for addr, pair in pairs.items():
            m = extract_metrics(pair)
            prev = state["readings"].get(addr)
            ok, reason, mult = evaluate(m, prev, cfg, now)
            if m["vol5m"] >= cfg["min_volume"] and m["liquidity"] >= cfg["min_liquidity"]:
                stats["eligible"] += 1
                if "baseline" in reason:
                    stats["baseline"] += 1
            if ok:
                hits.append((m, to_float(prev.get("vol5m")), mult))
            elif mult >= 1.5:
                near.append("%s %.2fx (%s)" % (m["symbol"], mult, reason))
            state["readings"][addr] = {"ts": now, "vol5m": m["vol5m"]}
    RUNTIME["last_scan_pools"] = len(pairs)
    if near:
        log.info("Near misses: %s", "; ".join(near[:10]))

    # 3) Dedup, then verify hits in batches (fresh DexScreener re-read + GeckoTerminal + RugCheck)
    todo, seen_mints = [], set()
    for m, prev_vol, mult in sorted(hits, key=lambda h: h[2], reverse=True):
        with STATE_LOCK:
            last = max(state["alerts"].get("pair:" + m["pair"], 0), state["alerts"].get("mint:" + m["mint"], 0))
        if now - last < cfg["cooldown"] or m["mint"] in seen_mints:
            log.info("Duplicate suppressed: %s (%s)", m["symbol"],
                     "alerted %d min ago" % ((now - last) // 60) if last else "same coin, other pool")
            continue
        seen_mints.add(m["mint"])
        todo.append((m, prev_vol))
    if todo and state.get("paused"):
        log.info("Alerts paused (/resume to enable): not sending %s", ", ".join(m["symbol"] for m, _ in todo))
        todo = []
    if todo:
        fresh = ds_fetch_pairs(http, [m["pair"] for m, _ in todo])
        gt = gt_fetch_pools(http, [m["pair"] for m, _ in todo]) if cfg["cross_check"] else {}
        fake_prev_ts = time.time() - (cfg["min_gap"] + cfg["max_gap"]) / 2
        for m, prev_vol in todo:
            again = fresh.get(m["pair"])
            if not again:
                log.info("Skip %s: could not re-confirm on DexScreener", m["symbol"])
                continue
            m2 = extract_metrics(again)
            # Re-check the fresh numbers against the same previous reading
            ok, reason, mult2 = evaluate(m2, {"ts": fake_prev_ts, "vol5m": prev_vol}, cfg, time.time())
            if not ok:
                log.info("Skip %s: failed re-check (%s)", m["symbol"], reason)
                continue
            gt_note = "DexScreener re-check passed"
            if cfg["cross_check"]:
                if gt is None:
                    log.info("Skip %s: GeckoTerminal cross-check unavailable right now", m2["symbol"])
                    continue
                ok, note = cross_check_gt(m2, gt.get(m2["pair"]), cfg)
                if not ok:
                    log.info("Skip %s: GeckoTerminal does not confirm (%s)", m2["symbol"], note)
                    continue
                gt_note = "DexScreener + GeckoTerminal (%s)" % note
            safety = rugcheck_report(http, m2["mint"], m2["pair"])
            if safety is None:
                log.info("RugCheck unavailable for %s, alert will say 'safety: unavailable'", m2["symbol"])
            elif cfg["skip_high_risk"] and is_high_risk(safety):
                log.info("Skip %s: RugCheck %s risk (SKIP_HIGH_RISK=true)", m2["symbol"], safety["level"])
                continue
            if state.get("paused"):
                log.info("Alerts paused during scan, %s not sent", m2["symbol"])
                continue
            if tg.send(build_alert(m2, prev_vol, mult2, gt_note, safety)):
                record_alert(state, m2, mult2, now, safety)
                stats["alerts"] += 1
                log.info("ALERT sent: %s %.2fx 5m vol $%s, risk %s", m2["symbol"], mult2, fmt_usd(m2["vol5m"]),
                         safety["level"] if safety else "unavailable")
                save_state(state)
            time.sleep(1)  # stay well under Telegram's per-chat limit

    with STATE_LOCK:
        prune_state(state, cfg, time.time())
    save_state(state)
    log.info("Scan done in %.0fs: %d pools read, %d pass volume+liquidity, %d new baselines, "
             "%d alerts. Stored readings: %d", time.time() - started, stats["scanned"],
             stats["eligible"], stats["baseline"], stats["alerts"], len(state["readings"]))
    return stats


# ----------------------------------------------------------------------------
# Offline self test (no network, never sends anything)
# ----------------------------------------------------------------------------
def selftest():
    cfg = load_config("/nonexistent")
    now = 1_000_000.0

    def pair(vol, liq=20000, buys=30, sells=10, mint="MemeMint111"):
        return {"pairAddress": "Pair111", "chainId": "solana", "dexId": "pumpswap",
                "baseToken": {"address": mint, "name": "Test <Coin>", "symbol": "TST"},
                "quoteToken": {"symbol": "SOL"}, "priceUsd": "0.00012",
                "txns": {"m5": {"buys": buys, "sells": sells}},
                "volume": {"m5": vol, "h1": vol * 3}, "liquidity": {"usd": liq},
                "priceChange": {"m5": 12.5}, "marketCap": 450000, "url": "https://dexscreener.com/solana/pair111"}

    prev = {"ts": now - 300, "vol5m": 4000}
    cases = [
        ("spike passes", pair(12000), prev, True),
        ("below multiplier", pair(12000), {"ts": now - 300, "vol5m": 5000}, False),
        ("low volume", pair(9000), {"ts": now - 300, "vol5m": 1000}, False),
        ("low liquidity", pair(12000, liq=5000), prev, False),
        ("low txns", pair(12000, buys=10, sells=5), prev, False),
        ("no baseline", pair(12000), None, False),
        ("stale previous", pair(12000), {"ts": now - 1200, "vol5m": 4000}, False),
        ("too recent previous", pair(12000), {"ts": now - 60, "vol5m": 4000}, False),
        ("SOL base excluded", pair(12000, mint="So11111111111111111111111111111111111111112"), prev, False),
        ("major symbol excluded", dict(pair(12000), baseToken={"address": "Zx1", "name": "Zcash", "symbol": "ZEC"}),
         prev, False),
        ("tokenized stock excluded", dict(pair(12000), baseToken={
            "address": "TTW1", "name": "Take-Two Interactive Software - Backpack Securities", "symbol": "TTWO"}),
         prev, False),
    ]
    failed = 0
    for name, p, pr, expect in cases:
        ok, reason, mult = evaluate(extract_metrics(p), pr, cfg, now)
        status = "OK " if ok == expect else "FAIL"
        failed += ok != expect
        print("%s %-22s -> %s (%s)" % (status, name, ok, reason))
    sample_safety = {"score": 1, "level": "LOW", "mint_authority": None, "freeze_authority": None,
                     "risks": [], "lp_locked": 100, "holders": 1234}
    msg = build_alert(extract_metrics(pair(12000)), 4000, 3.0, "selftest", sample_safety)
    assert "Strong buy pressure" in msg and "revoked" in msg
    assert "Safety: unavailable" in build_alert(extract_metrics(pair(12000)), 4000, 3.0, "selftest", None)
    assert buy_pressure(30, 10)[1].endswith("Strong buy pressure") and "sell" in buy_pressure(5, 20)[1]
    ratio_cfg = dict(cfg, min_buy_ratio=1.5)
    assert evaluate(extract_metrics(pair(12000, buys=20, sells=20)), prev, ratio_cfg, now)[0] is False
    assert evaluate(extract_metrics(pair(12000, buys=40, sells=10)), prev, ratio_cfg, now)[0] is True
    assert "&lt;Coin&gt;" in msg, "HTML escaping failed"
    gt_ok = {"volume_usd": {"m5": "9000"}, "reserve_in_usd": "21000",
             "transactions": {"m5": {"buys": 25, "sells": 9}}}
    assert cross_check_gt(None, gt_ok, cfg)[0] is True
    assert cross_check_gt(None, {"volume_usd": {"m5": "100"}, "reserve_in_usd": "21000"}, cfg)[0] is False
    path = os.path.join(tempfile.mkdtemp(), "state.json")
    st = empty_state()
    st["readings"]["x"] = {"ts": time.time(), "vol5m": 1.0}
    save_state(st, path)
    assert load_state(path)["readings"]["x"]["vol5m"] == 1.0
    print("\nSample alert:\n" + msg)
    print("\nSELFTEST %s" % ("PASSED" if not failed else "FAILED (%d)" % failed))
    return 0 if not failed else 1


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def _stop(signum, _frame):
    global _running
    _running = False
    log.info("Signal %s received, stopping after current step...", signum)


def main():
    parser = argparse.ArgumentParser(description="SHADOW VOLUME ALERT - Solana meme coin volume spike bot")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    parser.add_argument("--interval", type=int, help="override scan interval in seconds (testing)")
    parser.add_argument("--cycles", type=int, default=0, help="stop after N scans (testing)")
    parser.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    parser.add_argument("--selftest", action="store_true", help="offline logic test")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    setup_logging()
    cfg = load_config()
    if args.interval:
        cfg["interval"] = max(10, args.interval)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("SHADOW VOLUME ALERT v%s starting", VERSION)
    tg = Telegram(cfg["token"], cfg["chat_id"], args.dry_run)
    if tg.dry_run:
        log.warning("DRY-RUN mode: alerts are printed here, nothing is sent to Telegram "
                    "(set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config.env to go live).")
    else:
        username = tg.check()
        while username is None and _running:
            log.info("Retrying Telegram connection in 30s (no internet?)")
            time.sleep(30)
            username = tg.check()
        if not username:
            log.error("Fix TELEGRAM_BOT_TOKEN in config.env. Exiting.")
            return 2
        log.info("Telegram bot @%s ready, chat id %s", username, cfg["chat_id"])

    if args.test_telegram:
        ok = tg.send("✅ <b>SHADOW VOLUME ALERT</b> test message. Your bot token and chat ID work!")
        log.info("Test message %s", "sent" if ok else "FAILED")
        return 0 if ok else 1

    state = load_state()
    log.info("Loaded state: %d previous readings, %d alert records, %d watched pools",
             len(state["readings"]), len(state["alerts"]), len(state["watch"]))
    log.info("Rules: 5m vol >= $%s, liquidity >= $%s, 5m txns >= %d, increase >= %.2fx, every %ds",
             fmt_usd(cfg["min_volume"]), fmt_usd(cfg["min_liquidity"]), cfg["min_txns"],
             cfg["multiplier"], cfg["interval"])

    if cfg["startup_msg"] and not args.once:
        tg.send("🟢 <b>SHADOW VOLUME ALERT</b> is online\n"
                "Scanning Solana meme pools every %d min.\n"
                "Rules: 5m volume ≥ $%s, liquidity ≥ $%s, 5m txns ≥ %d, spike ≥ %.1fx vs previous 5m.\n"
                "First scan builds the baseline; alerts can start after the second scan.\n"
                "Send /help for commands.%s"
                % (cfg["interval"] // 60 or 1, fmt_usd(cfg["min_volume"]), fmt_usd(cfg["min_liquidity"]),
                   cfg["min_txns"], cfg["multiplier"],
                   "\n⏸ Alerts are PAUSED. Send /resume to enable them." if state.get("paused") else ""))

    if state.get("paused"):
        log.warning("Alerts are PAUSED (send /resume in Telegram to enable them)")
    if not tg.dry_run and not args.once:
        threading.Thread(target=command_listener, args=(cfg, state), name="commands", daemon=True).start()

    http = Http()
    cycle = 0
    RUNTIME["started"] = time.time()
    while _running:
        cycle += 1
        start = time.time()
        log.info("----- Scan #%d -----", cycle)
        RUNTIME["scanning"] = True
        try:
            run_scan(http, tg, state, cfg)
        except Exception:  # keep the bot alive no matter what
            log.exception("Unexpected error during scan; continuing")
            save_state(state)
        RUNTIME.update(scanning=False, scans=cycle, last_scan=time.time())
        if args.once or (args.cycles and cycle >= args.cycles):
            break
        sleep_for = max(5, cfg["interval"] - (time.time() - start))
        RUNTIME["next_scan"] = time.time() + sleep_for
        log.info("Next scan in %.0fs", sleep_for)
        end = time.time() + sleep_for
        while _running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))
    save_state(state)
    log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
