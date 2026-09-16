"""
Kite Option-Selling Dashboard — local backend
------------------------------------------------
Run:  python backend.py
Then open: https://algo.wecon.in

What this does
- Logs you into Kite Connect (daily login, token expires every day - that's Kite's design, not a bug here)
- Pulls your F&O stock universe from Kite's instrument list, ranks by historical volatility / ATR
- Also supports index options directly: NIFTY, BANKNIFTY, FINNIFTY — just type the symbol
- Lets you pick WHICH expiry (current month, next month, etc.) rather than only the nearest one
- Shows the full live option chain, lets you build and adjust a delta-based Iron Condor OR a naked
  Strangle (you control target delta, hedge width, and lot count before committing)
- Tracks entered trades with live daily P&L, re-estimated probability of success, and max loss
- Lets you actually PLACE the real orders for a tracked position in your Zerodha account — but only
  after an explicit confirmation step showing exactly what will be sent, and gives you an order list
  with cancel/modify so you stay in control the whole time
- Optional lightweight news headlines per stock as a basic event-risk / "threat intelligence" signal

IMPORTANT
- Nothing here is investment advice. Verify every number on your broker terminal before trading.
- Kite access tokens expire every day at ~6am IST. You will need to log in again each trading day.
- Naked strangles carry theoretically unlimited risk on the call side.
- ORDER EXECUTION IS REAL. Placing orders through this tool sends real orders to your live Zerodha
  account using real money. Nothing is placed without you explicitly confirming on the preview screen.
  If one leg of a multi-leg order fails, you may be left holding a partial, unhedged position — the
  tool stops immediately on the first failure and tells you to check your Zerodha app right away.
- REQUIRES kiteconnect >= 5.1.1 (`pip install --upgrade kiteconnect`). Exchanges now reject MARKET/
  SL-M orders placed via the API without a market_protection value (SEBI's retail algo-trading rules);
  this file always sends one, but older SDK versions don't accept the parameter at all — see the
  try/except around kite.place_order() in place_basket_orders() for the fallback behavior.
"""

import os
import math
import time
import json
import threading
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify, send_from_directory, redirect, session, render_template_string
import secrets
import uuid
import hmac
import sqlite3

try:
    from kiteconnect import KiteConnect
    from kiteconnect.exceptions import TokenException
except ImportError:
    raise SystemExit("Missing dependency. Run: pip install kiteconnect flask numpy requests")

import numpy as np
import requests
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Dict, Any, Tuple

# ---------------------------------------------------------------------------
# CONFIG — fill these in from https://developers.kite.trade (your app)
# SECURITY: set these as real environment variables (or a .env file loaded before
# this process starts) — do NOT hardcode real keys/secrets directly in this file,
# especially if this file is ever shared, committed to git, or pasted anywhere.
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("KITE_API_KEY", "vecsucwn1tckme31")
API_SECRET = os.environ.get("KITE_API_SECRET", "mehksxgc3gsbj3zz7kpacrb9ezrkvzro")
REDIRECT_URL = os.environ.get("REDIRECT_URL", "https://algo.wecon.in/api/callback")

# If your network does TLS interception (common on office/government networks — you'll see
# "self-signed certificate in certificate chain" errors), set this env var to allow the news
# feature specifically to fall back to an unverified request. This does NOT affect Kite API calls
# at all (those always stay fully verified) — it only relaxes verification for public news RSS
# feeds, which carry no credentials or sensitive data.
ALLOW_INSECURE_NEWS = os.environ.get("ALLOW_INSECURE_NEWS", "false").lower() == "true"

RISK_FREE_RATE = 0.07
MIN_DAYS_TO_EXPIRY = 7
DEFAULT_TARGET_DELTA = 0.18
DEFAULT_WING_WIDTH_PCT = 0.05
CHAIN_STRIKE_RANGE_PCT = 0.25

# --- Double Calendar Spread defaults ---
# A double calendar is: SELL a near-term call + SELL a near-term put (usually a bit OTM each side),
# and BUY a far-term call + far-term put at the SAME two strikes. It's a net-DEBIT, defined-risk
# trade that profits from the near leg decaying faster than the far leg (positive theta, long vega) —
# the "sweet spot" is the underlying sitting between the two short strikes at near expiry.
DEFAULT_CALENDAR_OTM_PCT = 0.03      # each strike this far OTM from spot, in "otm_pct" strike mode
DEFAULT_CALENDAR_TARGET_DELTA = 0.25 # used instead of otm_pct in "delta" strike mode
CALENDAR_TARGET_GAP_DAYS = 30        # preferred day-gap between near and far expiry when auto-picking
CALENDAR_CURVE_POINTS = 41           # number of spot points sampled for the payoff curve
CALENDAR_CURVE_RANGE_PCT = 0.15      # curve spans spot x (1 +/- this), i.e. +/-15% around current spot
# Exit-suggestion thresholds for tracked calendar positions (informational only, never auto-exits)
CALENDAR_STOP_LOSS_DEBIT_MULTIPLE = 0.5   # suggest exit if loss reaches this multiple of debit paid
CALENDAR_NEAR_EXPIRY_DAYS_WARNING = 3     # suggest exit/roll when this close to near-leg expiry (gamma risk)

# --- Exit / stop-loss suggestion rule (informational only — this tool never auto-exits) ---
# Trigger a suggested-exit flag when EITHER condition is met, whichever occurs first:
#   1) total position loss reaches this multiple of the premium originally received, or
#   2) either short leg's delta magnitude rises to at least this threshold.
STOP_LOSS_PREMIUM_MULTIPLE = 2.0
STOP_LOSS_DELTA_THRESHOLD = 0.35

# --- Approximate Zerodha F&O options charges (informational estimate only) ---
# These are commonly published rates as of this writing — brokerage/tax rules DO change over
# time (STT rates in particular have changed via budget announcements before). Verify current
# rates at https://zerodha.com/charges and your actual contract note before relying on this for
# anything beyond a rough planning estimate. All values are editable here.
CHARGES = {
    "brokerage_flat": 20.0,          # per executed order, or 0.03% of turnover, whichever is LOWER
    "brokerage_pct": 0.0003,
    "stt_sell_pct": 0.001,           # Securities Transaction Tax, options SELL side, on premium turnover
    "exchange_txn_pct": 0.0003503,   # NSE F&O exchange transaction charge, on premium turnover (both sides)
    "sebi_pct": 0.0000001,           # SEBI turnover fee (₹10 per crore == 0.0001%), both sides
    "gst_pct": 0.18,                 # GST on (brokerage + exchange txn charges + SEBI fee)
    "stamp_duty_buy_pct": 0.00003,   # stamp duty, BUY side only, on premium turnover
}

# --- Stock-picking screener v2: IV-rank, liquidity, ban-list, news (all best-effort) ---
# Kite has no historical-IV endpoint, so a genuine IV Rank/Percentile has to be built up by us,
# one snapshot per day, in a small local file. Until enough days have accumulated, iv_rank will
# be null and we fall back to a same-day cross-sectional IV percentile (how rich this stock's IV
# is TODAY relative to the other F&O stocks scanned today) so the field is never just empty.
# NSE/BSE trade on IST (UTC+5:30) regardless of what timezone this server's OS happens to be set
# to. Every "today", market-hours check, and historical-data window in this file needs to line up
# with the EXCHANGE's clock, not the server's -- so all of that goes through this helper instead of
# the bare now_ist(), which would silently use the server's local timezone and could otherwise
# leave charts/scans looking "stuck" a few hours behind (or ahead) if the server isn't set to IST.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    """Current wall-clock time in IST, as a naive datetime (matching what Kite's historical-data
    API expects, and what date/market-hours comparisons elsewhere in this file assume)."""
    return datetime.now(IST).replace(tzinfo=None)


IV_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "iv_history.json")
IVR_LOOKBACK_DAYS = 252
IVR_MIN_HISTORY_DAYS = 60        # genuine rank only after a meaningful history is available

# Iron-Condor-specific screening. The ATM pair is still useful as a first liquidity gate,
# but the final ranking is based on the ACTUAL four legs proposed for the condor.
MIN_ATM_TOTAL_OI = 500
MAX_ATM_SPREAD_PCT = 4.0
IC_MIN_LEG_OI = 25
IC_MAX_LEG_SPREAD_PCT = 15.0
IC_MIN_TOTAL_VOLUME = 10
IC_MIN_SHORT_OI = 200
IC_MAX_SHORT_SPREAD_PCT = 7.0
IC_MIN_SHORT_VOLUME = 10
IC_MIN_CREDIT_TO_MAX_LOSS = 0.08
IC_MIN_CUSHION_EM = 0.75
IC_CHAIN_STRIKE_RANGE_PCT = 0.30
IC_PREFERRED_DTE_LOW = 21
IC_PREFERRED_DTE_HIGH = 35
IC_MIN_DTE = 14
IC_MAX_DTE = 50
IC_DEFAULT_SHORT_DELTA_LOW = 0.15
IC_DEFAULT_SHORT_DELTA_HIGH = 0.20
IC_WING_WIDTHS_PCT = (0.02, 0.025, 0.035, 0.05, 0.07, 0.10)

# Final IC score: volatility richness 25, range quality 20, expected-move cushion 20,
# four-leg liquidity 15, trade economics 15, event risk 5.
IC_SCORE_WEIGHTS = {
    # Delta symmetry is deliberately explicit: a 0.18-delta IC should not silently
    # become a 0.25/0.18 structure merely because the latter collects more premium.
    "iv": 0.20, "range": 0.15, "cushion": 0.25,
    "liquidity": 0.10, "economics": 0.15, "delta": 0.10, "event": 0.05,
}
SCORE_WEIGHTS = {"iv_richness": 0.40, "calmness": 0.35, "liquidity": 0.25}  # legacy display

NEWS_FOR_TOP_N = 10
FO_BAN_LIST_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"

# ---------------------------------------------------------------------------
POSITION_DEBIT_PROFIT_TARGET = 0.40      # take/trim at +40% of debit instead of waiting for a rare double
POSITION_DEBIT_STOP_PCT = 0.30         # cap premium decay at 30% of debit
POSITION_DEBIT_EXPIRY_WARNING_DAYS = 7            # avoid the steepest theta/gamma part of the curve
def load_iv_history():
    if not os.path.exists(IV_HISTORY_FILE):
        return {}
    try:
        with open(IV_HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_iv_history(history):
    with open(IV_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def update_iv_history_and_get_rank(symbol, atm_iv_pct, history, today_str):
    """Appends today's ATM IV snapshot (idempotent per day so re-running the screener the same
    day doesn't distort the series), trims to the lookback window, and returns
    (iv_rank_pct_or_None, days_of_history)."""
    series = history.setdefault(symbol, [])
    series[:] = [pt for pt in series if pt["date"] != today_str]
    series.append({"date": today_str, "iv": atm_iv_pct})
    series.sort(key=lambda p: p["date"])
    if len(series) > IVR_LOOKBACK_DAYS:
        del series[:-IVR_LOOKBACK_DAYS]

    if len(series) < IVR_MIN_HISTORY_DAYS:
        return None, len(series)
    values = [p["iv"] for p in series]
    below_or_equal = sum(1 for v in values if v <= atm_iv_pct)
    rank_pct = 100.0 * below_or_equal / len(values)
    return round(rank_pct, 1), len(series)


def get_fo_ban_list():
    """Best-effort fetch of NSE's daily F&O ban list. NSE's site actively blocks plain
    requests without a real browser session/cookie handshake, and the URL/format can change —
    if this fails, we say so explicitly rather than silently treating everything as 'not banned'.
    Returns (set_of_symbols_or_None, error_message_or_None)."""
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/csv,*/*",
        }
        session.get("https://www.nseindia.com", headers=headers, timeout=8)  # cookie warm-up
        resp = session.get(FO_BAN_LIST_URL, headers=headers, timeout=8)
        resp.raise_for_status()
        lines = [l.strip() for l in resp.text.splitlines() if l.strip()]
        symbols = set()
        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            for p in parts:
                if p.isupper() and p.isalnum() and len(p) > 1:
                    symbols.add(p)
        return symbols, None
    except Exception as e:
        return None, (f"Could not fetch NSE F&O ban list ({e}). Verify manually at "
                       f"https://www.nseindia.com/companies-listing/corporate-filings-actions "
                       f"before trading — this filter is best-effort only.")


def _quote_cache_get(keys):
    now = time.monotonic()
    out = {}
    for k in keys:
        item = _QUOTE_CACHE.get(k)
        if item and now - item["ts"] <= _QUOTE_CACHE_TTL:
            out[k] = item["quote"]
    return out


def kite_quote_bulk(keys, *, chunk_size=500, retries=1, force_refresh=False):
    """All HTTP calls pass the shared broker gate. Never sleep/retry a 429 here."""
    keys=list(dict.fromkeys(k for k in keys if k))
    if not keys:return {}
    now=time.monotonic();ttl=min(_QUOTE_CACHE_TTL,.9 if force_refresh else _QUOTE_CACHE_TTL)
    result={k:_QUOTE_CACHE[k]['quote'] for k in keys if k in _QUOTE_CACHE and now-_QUOTE_CACHE[k]['ts']<=ttl}
    missing=[k for k in keys if k not in result]
    # Include owned options in scanner batches so their supervision can reuse fresh books.
    owned=[]
    guard=globals().get('SHARED_RISK')
    if guard:
        for engine in guard.engines.values():
            owned.extend(p['exchange']+':'+p['contract'] for p in engine.active() if p['qty']>0)
    size=max(1,min(int(chunk_size),max(1,500-len(set(owned)))))
    for start in range(0,len(missing),size):
        chunk=list(dict.fromkeys(owned+missing[start:start+size]))[:500]
        try:
            raw=kite.quote(chunk) or {}
            for k,q in raw.items():_QUOTE_CACHE[k]={'quote':q,'ts':time.monotonic()}
            result.update({k:q for k,q in raw.items() if k in keys})
        except BrokerDeferred:
            break
        except Exception as e:
            logger.warning('Quote request failed: %s',e);break
    return result


def seed_spot_cache_from_prices(rows):
    now = time.monotonic()
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        price = r.get("ltp") or r.get("last_close")
        if sym and price:
            _QUOTE_CACHE[f"NSE:{sym}"] = {
                "quote": {"last_price": float(price)}, "ts": now
            }


def pick_atm_contracts(nfo_opts_for_symbol, last_close, today):
    """Given this symbol's NFO-OPT instruments, last close price, and today's date, picks the
    nearest valid expiry (same MIN_DAYS_TO_EXPIRY rule as the strategy builder) and the strike
    closest to last_close. Returns (ce_tradingsymbol, pe_tradingsymbol, strike, expiry, T) or None."""
    if not nfo_opts_for_symbol:
        return None
    all_expiries = sorted({o["expiry"] for o in nfo_opts_for_symbol})
    valid = [e for e in all_expiries if (e - today).days >= MIN_DAYS_TO_EXPIRY]
    if not valid:
        return None
    expiry = valid[0]
    chain = [o for o in nfo_opts_for_symbol if o["expiry"] == expiry]
    strikes = sorted({o["strike"] for o in chain})
    if not strikes:
        return None
    atm_strike = min(strikes, key=lambda k: abs(k - last_close))
    ce = next((o for o in chain if o["strike"] == atm_strike and o["instrument_type"] == "CE"), None)
    pe = next((o for o in chain if o["strike"] == atm_strike and o["instrument_type"] == "PE"), None)
    if not ce or not pe:
        return None
    T = max((expiry - today).days, 0) / 365.0
    return ce["tradingsymbol"], pe["tradingsymbol"], atm_strike, expiry, T


def quote_spread_pct(q):
    """Bid-ask spread as % of mid, from a Kite quote's depth. None if depth unavailable."""
    if not q:
        return None
    depth = q.get("depth", {}) or {}
    buys = [b for b in depth.get("buy", []) if b.get("price", 0) > 0]
    sells = [s for s in depth.get("sell", []) if s.get("price", 0) > 0]
    if not buys or not sells:
        return None
    bid, ask = buys[0]["price"], sells[0]["price"]
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid * 100.0


def get_atm_iv_and_liquidity_bulk(candidates, nfo):
    """candidates: list of {'symbol', 'last_close'}. Batches Kite quote() calls (chunks of 200
    instruments, well under Kite's per-call limit) instead of one call per stock, since fetching
    a full option chain per stock (like get_chain_for_symbol does for a single symbol) would mean
    hundreds of extra round-trips here. Returns {symbol: {atm_iv_pct, atm_oi_total, spread_pct,
    expiry}} — a symbol is omitted if its ATM contracts couldn't be resolved or quoted."""
    today = now_ist().date()
    opts_by_symbol = {}
    for o in nfo:
        if o["segment"] == "NFO-OPT":
            opts_by_symbol.setdefault(o["name"], []).append(o)

    picks = {}  # symbol -> (ce_ts, pe_ts, strike, expiry, T)
    needed_keys = []
    for c in candidates:
        sym = c["symbol"]
        pick = pick_atm_contracts(opts_by_symbol.get(sym, []), c["last_close"], today)
        if pick:
            picks[sym] = pick
            ce_ts, pe_ts, _, _, _ = pick
            needed_keys.append(f"NFO:{ce_ts}")
            needed_keys.append(f"NFO:{pe_ts}")

    quotes = kite_quote_bulk(needed_keys, chunk_size=500, retries=1)

    out = {}
    for sym, (ce_ts, pe_ts, strike, expiry, T) in picks.items():
        ce_q = quotes.get(f"NFO:{ce_ts}")
        pe_q = quotes.get(f"NFO:{pe_ts}")
        ce_ltp, pe_ltp = extract_price(ce_q), extract_price(pe_q)
        if ce_ltp is None or pe_ltp is None:
            continue
        last_close = next(c["last_close"] for c in candidates if c["symbol"] == sym)
        ce_iv = implied_vol(ce_ltp, last_close, strike, T, "CE")
        pe_iv = implied_vol(pe_ltp, last_close, strike, T, "PE")
        atm_iv_pct = (ce_iv + pe_iv) / 2 * 100
        ce_oi = (ce_q or {}).get("oi", 0) or 0
        pe_oi = (pe_q or {}).get("oi", 0) or 0
        spreads = [s for s in (quote_spread_pct(ce_q), quote_spread_pct(pe_q)) if s is not None]
        spread_pct = round(sum(spreads) / len(spreads), 2) if spreads else None
        out[sym] = {"atm_iv_pct": round(atm_iv_pct, 1), "atm_oi_total": int(ce_oi + pe_oi),
                     "atm_spread_pct": spread_pct, "atm_expiry": str(expiry)}
    return out


def _percentile_rank(value, all_values):
    """0-100, higher = higher value relative to the group. None-safe."""
    vals = [v for v in all_values if v is not None]
    if value is None or not vals:
        return 50.0  # neutral when data is missing, rather than silently zero-weighting it
    below_or_equal = sum(1 for v in vals if v <= value)
    return 100.0 * below_or_equal / len(vals)


# --- Event calendar (informational only) ---
# A hand-maintained list of known macro event dates that commonly move markets, used to warn
# against opening NEW positions right around them, and to flag existing positions that run into
# one before expiry. RBI MPC and FOMC dates below were sourced from RBI/Federal Reserve published
# calendars — always re-verify at rbi.org.in and federalreserve.gov since schedules can shift.
# Election result days and geopolitical events are NOT reliably predictable in advance and are not
# auto-populated — add them yourself via POST /api/event-calendar/add as they become known.
EVENT_CALENDAR_FILE = os.path.join(os.path.dirname(__file__), "event_calendar.json")
ENTRY_WARNING_WINDOW_DAYS = 2   # warn on new entries if an event falls within this many days
TRADE_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "trade_history.json")

# Index symbols you can type directly (in addition to any F&O stock) — maps to the exact
# Kite quote key Kite uses for that index's live spot price.
INDEX_SYMBOLS = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX": "BSE:SENSEX",
}

INDEX_OPTION_EXCHANGE = {"SENSEX": "BFO", "NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO", "MIDCPNIFTY": "NFO"}

OPTION_EXCHANGE_SEGMENT = {"NFO": "NFO-OPT", "BFO": "BFO-OPT"}

app = Flask(__name__, static_folder="static", static_url_path="")

# Website access: set WEBSITE_PASSWORD and WEBSITE_SECRET_KEY in the server environment.
# Serve ALL dashboard paths through Flask (including / and /index.html).
# WEBSITE_SECRET_KEY should be a long random value shared by all server workers.
WEBSITE_PASSWORD = os.environ.get("WEBSITE_PASSWORD", "")
app.secret_key = os.environ.get("WEBSITE_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("WEBSITE_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8), MAX_CONTENT_LENGTH=2 * 1024 * 1024)
_SITE_FAILURES = {}
_SITE_LOGIN_LOCK = threading.Lock()
# A durable, shared session generation revokes signed cookies on every device.
# Keep this file across deployments; it contains no password or trading data.
_SITE_AUTH_DB = os.environ.get('WEBSITE_AUTH_DB') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'website_auth.sqlite3')

def _site_generation(revoke=False):
    from contextlib import closing
    with closing(sqlite3.connect(_SITE_AUTH_DB, timeout=10)) as connection:
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS website_auth (id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL)')
            connection.execute('INSERT OR IGNORE INTO website_auth VALUES(1, ?)', (secrets.token_hex(32),))
            if revoke:
                connection.execute('UPDATE website_auth SET generation=? WHERE id=1', (secrets.token_hex(32),))
            return connection.execute('SELECT generation FROM website_auth WHERE id=1').fetchone()[0]

_SITE_LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Website sign in</title>
<style>body{margin:0;background:#0b1220;color:#edf3fa;font:16px system-ui;display:grid;place-items:center;min-height:100vh}
main{width:min(360px,80vw);padding:32px;background:#162235;border:1px solid #334155;border-radius:18px}
h1{margin-top:0}p{color:#abbad0}input,button{box-sizing:border-box;width:100%;padding:13px;margin-top:12px;border-radius:8px;border:1px solid #64748b;font:inherit}
button{background:#b1ef65;color:#142009;cursor:pointer;font-weight:700}.error{color:#ffb4b4}</style></head>
<body><main><h1>Website sign in</h1><p>Enter your password to open the dashboard.</p>
<form method="post" action="/website-login"><input type="hidden" name="csrf" value="{{ csrf }}">
<label for="password">Password</label><input id="password" type="password" name="password" autocomplete="current-password" required autofocus>
<button type="submit">Enter website</button></form><p class="error" role="alert">{{ error }}</p></main></body></html>"""

def _site_authenticated():
    return bool(WEBSITE_PASSWORD and session.get('website_authenticated') and
                session.get('website_generation') == _site_generation())

@app.before_request
def require_website_password():
    if request.endpoint == 'website_login':
        return None
    if not _site_authenticated():
        if request.path.startswith('/api/'):
            return jsonify(error='Website password required', website_login_required=True), 401
        return redirect('/website-login')

@app.after_request
def protect_website_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response

@app.route('/website-login', methods=['GET', 'POST'])
def website_login():
    if _site_authenticated():
        return redirect('/')
    error = ''; status = 200
    session.setdefault('website_csrf', secrets.token_urlsafe(32))
    if not WEBSITE_PASSWORD:
        error = 'Website access is not configured. Set WEBSITE_PASSWORD on the server.'; status = 503
    elif request.method == 'POST':
        token = request.form.get('csrf', '')
        if not hmac.compare_digest(token, session['website_csrf']):
            error = 'Please refresh this page and try again.'; status = 400
        else:
            address = request.remote_addr or 'unknown'
            with _SITE_LOGIN_LOCK:
                now = time.monotonic()
                for key in list(_SITE_FAILURES):
                    if now - _SITE_FAILURES[key][1] >= 300: del _SITE_FAILURES[key]
                count, started = _SITE_FAILURES.get(address, (0, now))
                if count >= 5:
                    error = 'Too many attempts. Please try again in five minutes.'; status = 429
                elif hmac.compare_digest(request.form.get('password', '').encode(), WEBSITE_PASSWORD.encode()):
                    _SITE_FAILURES.pop(address, None)
                    session.clear(); session['website_authenticated'] = True; session.permanent = True
                    session['website_generation'] = _site_generation()
                    session['website_csrf'] = secrets.token_urlsafe(32)
                    return redirect('/')
                else:
                    _SITE_FAILURES[address] = (count + 1, started)
                    error = 'Incorrect password.'; status = 401
    return render_template_string(_SITE_LOGIN_HTML, error=error, csrf=session['website_csrf']), status

@app.route('/website-logout', methods=['POST'])
def website_logout():
    token = request.headers.get('X-Website-CSRF', '') or request.form.get('csrf', '')
    if not token or not hmac.compare_digest(token, session.get('website_csrf', '')):
        return jsonify(error='Refresh the website before locking all devices.'), 403
    # Revoke first; never report a successful global lock unless it was committed.
    _site_generation(revoke=True)
    session.clear()
    if request.headers.get('X-Website-CSRF'):
        return jsonify(locked=True)
    return redirect('/website-login')

@app.route('/api/website-status')
def website_status():
    return jsonify(authenticated=True, csrf=session['website_csrf'])

@app.route('/index.html')
def website_index_html():
    return send_from_directory(os.path.dirname(__file__), 'index.html')


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("kite_dashboard")
class BrokerDeferred(RuntimeError):
    """Local scheduling refused a call before any request was sent to Kite."""


class BrokerRequestGate:
    # Conservative spacing, below Kite's documented endpoint and mutation limits.
    def __init__(self):
        self.lock=threading.Lock();self.next_at={};self.cool_until=0.;self.failures=0;self.cache={};self.generation=0
        self.intervals={'quote':1.05,'history':.36,'write':.20,'read':.13}
    def status(self):
        with self.lock:return dict(cooldown_seconds=round(max(0.,self.cool_until-time.monotonic()),1),rate_limit_hits=self.failures)
    def call(self,fn,route,method,*args,**kwargs):
        bucket='write' if method.upper() not in ('GET','HEAD') else 'quote' if route.startswith('market.quote') else 'history' if 'historical' in route else 'read'
        # Only share short-lived read-only account snapshots; never share order mutations.
        cacheable=method.upper()=='GET' and route in ('orders','trades','portfolio.positions')
        key=(route,json.dumps([args,kwargs],sort_keys=True,default=str))
        deadline=time.monotonic()+1.5
        while True:
            with self.lock:
                now=time.monotonic()
                cached=self.cache.get(key)
                if cacheable and cached and now-cached[0]<.5:
                    import copy
                    return copy.deepcopy(cached[1])
                delay=max(self.cool_until-now,self.next_at.get(bucket,0)-now,0.)
                if delay<=0:
                    self.next_at[bucket]=now+self.intervals[bucket]
                    if bucket=='write':self.generation+=1;self.cache.clear()
                    generation=self.generation;break
                if self.cool_until>now or now+delay>deadline:raise BrokerDeferred('Broker request deferred locally; supervision will retry. No request was sent.')
            time.sleep(min(delay,.05))
        try:result=fn(route,method,*args,**kwargs)
        except Exception as e:
            if getattr(e,'code',None)==429 or 'too many requests' in str(e).lower():
                with self.lock:
                    self.failures+=1;self.cool_until=time.monotonic()+min(60.,10.*(2**min(self.failures-1,3)));self.cache.clear()
            raise  # Never automatically repeat an order whose outcome may be uncertain.
        with self.lock:
            if bucket=='write':self.generation+=1;self.cache.clear()
            elif cacheable and generation==self.generation:
                import copy
                self.cache[key]=(time.monotonic(),copy.deepcopy(result))
        return result


BROKER_GATE=BrokerRequestGate()


class GovernedKiteConnect(KiteConnect):
    def _request(self,route,method,*args,**kwargs):
        return BROKER_GATE.call(super()._request,route,method,*args,**kwargs)


kite = GovernedKiteConnect(api_key=API_KEY, timeout=10)

# Zerodha Quote API protection. Quote is limited to 1 request/second and a maximum of
# 500 instruments per request. Keep one process-wide gate so Screen 1, Screen 2 and
# other quote consumers cannot accidentally burst the API.
_QUOTE_LOCK = threading.Lock()
_QUOTE_LAST_AT = 0.0
_QUOTE_COOLDOWN_UNTIL = 0.0
_QUOTE_CACHE = {}  # key -> {"quote": dict, "ts": monotonic_seconds}
_QUOTE_CACHE_TTL = float(os.environ.get("KITE_QUOTE_CACHE_TTL", "3.0"))
_QUOTE_MIN_INTERVAL = float(os.environ.get("KITE_QUOTE_MIN_INTERVAL", "1.05"))
_QUOTE_429_COOLDOWN = float(os.environ.get("KITE_QUOTE_429_COOLDOWN", "10.5"))

SESSION = {"access_token": None, "logged_in_at": None}
INSTRUMENT_CACHE = {"nfo": None, "nse": None, "bfo": None, "bse": None, "fetched_at": None}
SCREENER_CACHE = {"results": None, "fetched_at": None}
IC_SCREENER_CACHE = {"results": None, "fetched_at": None}

POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")
_positions_lock = threading.Lock()


def load_positions():
    if not os.path.exists(POSITIONS_FILE):
        return []
    try:
        with open(POSITIONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_positions(positions):
    with _positions_lock:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2, default=str)


def find_position(pos_id):
    for p in load_positions():
        if p["id"] == pos_id:
            return p
    return None


# Event calendar storage
def load_event_calendar():
    if not os.path.exists(EVENT_CALENDAR_FILE):
        return []
    try:
        with open(EVENT_CALENDAR_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_event_calendar(events):
    with open(EVENT_CALENDAR_FILE, "w") as f:
        json.dump(events, f, indent=2)


def seed_event_calendar_if_missing():
    """Seeds a starter calendar the first time this runs. Sourced from officially published
    RBI and US Federal Reserve calendars as of this writing — verify/update at rbi.org.in and
    federalreserve.gov, since meeting schedules can shift and this list isn't auto-refreshed."""
    if os.path.exists(EVENT_CALENDAR_FILE):
        return
    events = [
        # RBI Monetary Policy Committee — FY 2026-27 schedule (published by RBI)
        {"date": "2026-08-05", "label": "RBI MPC Policy Announcement", "type": "rbi_policy", "source": "RBI FY26-27 calendar"},
        {"date": "2026-10-07", "label": "RBI MPC Policy Announcement", "type": "rbi_policy", "source": "RBI FY26-27 calendar"},
        {"date": "2026-12-04", "label": "RBI MPC Policy Announcement", "type": "rbi_policy", "source": "RBI FY26-27 calendar"},
        # US Federal Reserve FOMC — 2026 schedule (decision announced on 2nd day, ~2pm ET)
        {"date": "2026-07-29", "label": "FOMC Rate Decision", "type": "fed_policy", "source": "federalreserve.gov 2026 calendar"},
        {"date": "2026-09-16", "label": "FOMC Rate Decision", "type": "fed_policy", "source": "federalreserve.gov 2026 calendar"},
        {"date": "2026-10-28", "label": "FOMC Rate Decision", "type": "fed_policy", "source": "federalreserve.gov 2026 calendar"},
        {"date": "2026-12-09", "label": "FOMC Rate Decision", "type": "fed_policy", "source": "federalreserve.gov 2026 calendar"},
        # Union Budget — fixed Feb 1 convention in India since 2017
        {"date": "2027-02-01", "label": "Union Budget Day", "type": "budget", "source": "fixed annual convention"},
    ]
    save_event_calendar(events)


def get_upcoming_events(days_ahead=45):
    events = load_event_calendar()
    today = now_ist().date()
    upcoming = []
    for idx, e in enumerate(events):
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        days_away = (d - today).days
        if 0 <= days_away <= days_ahead:
            upcoming.append({**e, "days_away": days_away, "index": idx})
    upcoming.sort(key=lambda e: e["days_away"])
    return upcoming


def get_entry_warning():
    """Checks for any flagged event within ENTRY_WARNING_WINDOW_DAYS — used to warn (not block)
    against opening a brand-new position right around a known macro event."""
    near = [e for e in get_upcoming_events(days_ahead=ENTRY_WARNING_WINDOW_DAYS)]
    if not near:
        return None
    labels = ", ".join(f"{e['label']} ({e['date']})" for e in near)
    return (f"Heads up: {labels} within the next {ENTRY_WARNING_WINDOW_DAYS} days. Many traders avoid "
            f"opening new option-selling positions right around major policy/event days due to volatility risk. "
            f"This is informational only — the tool does not block the trade.")


def get_event_before_expiry(expiry_str):
    """For an existing tracked position — any flagged event between today and its expiry."""
    try:
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    except Exception:
        return None
    today = now_ist().date()
    days_to_expiry = max((expiry_date - today).days, 0)
    events = get_upcoming_events(days_ahead=days_to_expiry)
    return events[0] if events else None


# ---------------------------------------------------------------------------
# Trade history (archived on full close)
# ---------------------------------------------------------------------------
def load_trade_history():
    if not os.path.exists(TRADE_HISTORY_FILE):
        return []
    try:
        with open(TRADE_HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_trade_history(history):
    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, default=str)


def archive_closed_position(position, close_results):
    history = load_trade_history()
    est_realized_pnl = sum(
        r.get("estimated_realized_pnl", 0) for r in close_results if r["status"] in ("placed", "filled")
    )
    exit_orders_for_charges = [
        {"price": r.get("reference_price") or 0, "quantity": r.get("quantity", 0),
         "transaction_type": r.get("transaction_type", "SELL")}
        for r in close_results if r["status"] in ("placed", "filled")
    ]
    exit_charges = estimate_charges(exit_orders_for_charges)
    entry_charges_total = position.get("entry_estimated_charges") or 0
    round_trip_charges = round(entry_charges_total + exit_charges["total"], 2)
    net_realized_after_charges = round(est_realized_pnl - round_trip_charges, 2)

    history.append({
        "id": position["id"], "symbol": position["symbol"], "strategy_type": position.get("strategy_type"),
        "added_on": position.get("added_on"), "closed_on": now_ist().date().isoformat(),
        "entry_max_profit": position.get("entry_max_profit"), "entry_max_loss": position.get("entry_max_loss"),
        "estimated_realized_pnl": round(est_realized_pnl, 2),
        "entry_charges": entry_charges_total, "estimated_exit_charges": exit_charges["total"],
        "estimated_round_trip_charges": round_trip_charges,
        "net_realized_pnl_after_charges": net_realized_after_charges,
        "close_orders": close_results,
        "note": "estimated_realized_pnl is based on quoted prices at close time, not confirmed fill "
                "prices — check your Zerodha contract note for the exact realized P&L and charges.",
    })
    save_trade_history(history)


# Seed the event calendar once at import time — works whether launched via
# `python backend.py` directly or imported by Gunicorn (`gunicorn backend:app`).
seed_event_calendar_if_missing()


# ---------------------------------------------------------------------------
# Black-Scholes helpers
# ---------------------------------------------------------------------------
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, r, sigma, opt_type):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = (S - K) if opt_type == "CE" else (K - S)
        return max(0.0, intrinsic)
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == "CE":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_delta(S, K, T, r, sigma, opt_type):
    if T <= 0 or sigma <= 0:
        return 1.0 if (opt_type == "CE" and S > K) else (0.0 if opt_type == "CE" else (-1.0 if S < K else 0.0))
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1) if opt_type == "CE" else (norm_cdf(d1) - 1)


def implied_vol(price, S, K, T, opt_type, r=RISK_FREE_RATE):
    if price <= 0 or T <= 0:
        return 0.0
    lo, hi = 0.001, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p = bs_price(S, K, T, r, mid, opt_type)
        if p > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# Greeks — Gamma / Theta / Vega (Delta/Price/IV are above) + portfolio aggregation, used by the
# Dynamic Delta-Neutral Adjustment Engine further down. Reuses the exact same bs_price / bs_delta /
# norm_cdf already defined above so every Greek across the whole app is priced identically.
#
# Sign convention: a SHORT leg (you sold it) contributes the NEGATIVE of the raw per-unit option
# Greek to the portfolio; a LONG leg (bought, e.g. a hedge) contributes the raw (positive) Greek.
# This is expressed by passing `quantity` already signed (negative = short, positive = long) — every
# Greek is multiplied by that signed quantity, so the sign logic lives in exactly one place.
# ---------------------------------------------------------------------------
def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1_d2(S, K, T, r, sigma):
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_gamma(S, K, T, r, sigma):
    """Identical for calls and puts at the same strike/expiry. Per 1-point move in the underlying."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return norm_pdf(d1) / (S * sigma * math.sqrt(T))


def bs_vega(S, K, T, r, sigma):
    """Per 1 percentage point (0.01) change in IV — the convention traders actually quote."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return S * norm_pdf(d1) * math.sqrt(T) / 100.0


def bs_theta(S, K, T, r, sigma, opt_type):
    """Per calendar day (annualized theta / 365)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    term1 = -(S * norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
    if opt_type == "CE":
        term2 = -r * K * math.exp(-r * T) * norm_cdf(d2)
    else:
        term2 = r * K * math.exp(-r * T) * norm_cdf(-d2)
    return (term1 + term2) / 365.0


@dataclass
class LegGreeks:
    tradingsymbol: str
    role: str            # "sell_call" | "sell_put" | "buy_call" | "buy_put" | ...
    opt_type: str         # "CE" | "PE"
    strike: float
    quantity: int          # SIGNED: negative for short legs, positive for long legs
    ltp: float
    delta: float
    gamma: float
    theta: float
    vega: float
    mtm: float


@dataclass
class PortfolioGreeks:
    symbol: str
    spot: float
    net_delta: float = 0.0
    net_gamma: float = 0.0
    net_theta: float = 0.0
    net_vega: float = 0.0
    mtm: float = 0.0
    legs: List[LegGreeks] = field(default_factory=list)

    def as_dict(self):
        return {
            "symbol": self.symbol, "spot": self.spot,
            "net_delta": round(self.net_delta, 2), "net_gamma": round(self.net_gamma, 4),
            "net_theta": round(self.net_theta, 2), "net_vega": round(self.net_vega, 2),
            "mtm": round(self.mtm, 2),
            "legs": [vars(l) for l in self.legs],
        }


class PortfolioGreeksEngine:
    """Computes per-leg and portfolio-level Greeks from plain dicts. The caller is responsible for
    fetching live spot/LTP/IV and passing them in."""

    def __init__(self, risk_free_rate=RISK_FREE_RATE):
        self.r = risk_free_rate

    def leg_greeks(self, *, opt_type, strike, spot, T, iv, ltp, quantity, role, tradingsymbol,
                   entry_premium=None):
        delta = bs_delta(spot, strike, T, self.r, iv, opt_type) * quantity
        gamma = bs_gamma(spot, strike, T, self.r, iv) * quantity
        theta = bs_theta(spot, strike, T, self.r, iv, opt_type) * quantity
        vega = bs_vega(spot, strike, T, self.r, iv) * quantity
        mtm = 0.0
        if entry_premium is not None:
            # Short leg profits when ltp falls below entry premium; long leg profits when ltp rises.
            per_unit_pnl = (entry_premium - ltp) if quantity < 0 else (ltp - entry_premium)
            mtm = per_unit_pnl * abs(quantity)
        return LegGreeks(tradingsymbol, role, opt_type, strike, quantity, ltp,
                          round(delta, 4), round(gamma, 6), round(theta, 4), round(vega, 4),
                          round(mtm, 2))

    def portfolio_greeks(self, symbol, spot, legs):
        """legs: iterable of dicts, each with opt_type, strike, T, iv, ltp, quantity (signed), role,
        tradingsymbol, and optionally entry_premium (for MTM)."""
        pg = PortfolioGreeks(symbol=symbol, spot=spot)
        for leg in legs:
            lg = self.leg_greeks(**leg)
            pg.legs.append(lg)
            pg.net_delta += lg.delta
            pg.net_gamma += lg.gamma
            pg.net_theta += lg.theta
            pg.net_vega += lg.vega
            pg.mtm += lg.mtm
        return pg


# ---------------------------------------------------------------------------
# Risk Management — configurable gates for the Delta Neutral Adjustment Engine. Every check here is
# a pure function of (limits, current numbers) -> (allowed: bool, reason: str); nothing in this
# section places or cancels orders, it only decides whether the Adjustment Engine / Execution code
# further down is ALLOWED to act.
# ---------------------------------------------------------------------------
@dataclass
class RiskLimits:
    max_adjustments_per_day: int = 6
    max_loss_per_position: float = 15000.0        # Rs, absolute MTM loss on a single position
    max_daily_mtm_loss: float = 25000.0            # Rs, absolute MTM loss across ALL positions today
    min_premium_for_adjustment: float = 8.0        # Rs; don't roll/adjust into a leg worth less than this
    profit_targets_pct: Tuple[float, ...] = (25.0, 50.0, 70.0, 90.0)   # staged profit-booking levels
    stop_loss_pct: float = 200.0                   # % of credit received; exit if MTM loss exceeds this


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def can_adjust(self, *, adjustments_today: int, position_mtm: float, daily_mtm: float,
                    proposed_leg_premium: Optional[float] = None) -> Tuple[bool, str]:
        if adjustments_today >= self.limits.max_adjustments_per_day:
            return False, f"Max adjustments/day reached ({self.limits.max_adjustments_per_day})"
        if position_mtm <= -abs(self.limits.max_loss_per_position):
            return False, f"Position MTM loss (Rs {position_mtm:.0f}) exceeds max loss per position"
        if daily_mtm <= -abs(self.limits.max_daily_mtm_loss):
            return False, f"Daily MTM loss (Rs {daily_mtm:.0f}) exceeds max daily loss for this symbol"
        if proposed_leg_premium is not None and proposed_leg_premium < self.limits.min_premium_for_adjustment:
            return False, (f"Proposed leg premium Rs {proposed_leg_premium:.2f} is below the minimum "
                            f"Rs {self.limits.min_premium_for_adjustment} required to bother adjusting")
        return True, "OK"

    def breached_daily_loss(self, daily_mtm: float) -> bool:
        return daily_mtm <= -abs(self.limits.max_daily_mtm_loss)

    def profit_target_hit(self, credit_received: float, current_mtm: float) -> Optional[float]:
        """Returns the highest configured profit-target % that's been reached, or None. Positions
        are meant to be closed in FULL the moment any configured target is hit."""
        if credit_received <= 0:
            return None
        pct_captured = (current_mtm / credit_received) * 100.0
        hit = [t for t in sorted(self.limits.profit_targets_pct) if pct_captured >= t]
        return max(hit) if hit else None

    def stop_loss_hit(self, credit_received: float, current_mtm: float) -> bool:
        if credit_received <= 0:
            return False
        loss_pct = (-current_mtm / credit_received) * 100.0
        return loss_pct >= self.limits.stop_loss_pct


# ---------------------------------------------------------------------------
# Adjustment Engine — Scenario A/B logic and a transparent multi-factor scorer (this IS the "AI
# Recommendation Engine" from the spec, implemented as an inspectable weighted-score model rather
# than an opaque trained model, so every number that drives a decision is loggable and auditable).
#
# --- Why net delta goes NEGATIVE when the market moves UP (easy to get backwards) ---
# Selling a call is a short-delta position (lose as price rises, like being short the underlying);
# selling a put is a long-delta position (lose as price falls, like being long the underlying). In a
# roughly delta-neutral short strangle/condor, both are sized to net close to zero. If the underlying
# RISES: the short call moves closer to the money -> its delta magnitude grows -> your (negative)
# delta exposure from that leg grows more negative; the short put moves further OTM -> its delta
# shrinks toward zero -> your (positive) exposure from that leg shrinks. Both effects push net
# portfolio delta MORE NEGATIVE as spot rises (consistent with losing money as price rises = negative
# delta). The mirror is true on the way down:
#     net_delta very NEGATIVE  <=>  market has moved UP, the CALL side is under stress  (Scenario A)
#     net_delta very POSITIVE  <=>  market has moved DOWN, the PUT side is under stress (Scenario B)
# ---------------------------------------------------------------------------
@dataclass
class AdjustmentCandidate:
    action: str                  # "roll_put_up" | "roll_call_down" | "roll_call_further_otm" |
                                  # "roll_put_further_otm" | "convert_iron_fly" | "add_hedge" | "no_action"
    description: str
    legs_to_close: list = field(default_factory=list)   # leg dicts to buy back / sell to close
    legs_to_open: list = field(default_factory=list)    # leg dicts describing the new legs
    expected_delta_after: float = 0.0
    additional_premium: float = 0.0     # Rs collected (positive) or paid (negative) net of this adjustment
    margin_impact: float = 0.0          # Rs, additional margin this adjustment is expected to require
    risk_reduction_score: float = 0.0   # 0-1: how much closer to delta-neutral this gets you
    probability_of_profit: float = 0.0  # 0-1
    expected_drawdown: float = 0.0      # Rs, rough worst-case add-on risk from taking this action
    score: float = 0.0
    reasoning: str = ""


class AdjustmentEngine:
    def __init__(self, delta_threshold=10.0, gamma_threshold=None, weights=None):
        self.delta_threshold = delta_threshold
        self.gamma_threshold = gamma_threshold
        # Weighted composite score — every factor normalized to a comparable 0..1-ish scale before
        # weighting, so no single factor dominates purely because of its raw units (Rs vs a
        # probability vs a percentage). Weights are configurable from Settings.
        self.weights = weights or {
            "expected_profit": 0.30, "risk_reduction": 0.30, "additional_premium": 0.15,
            "margin_impact": 0.10, "probability_of_profit": 0.10, "expected_drawdown": 0.05,
        }

    def needs_adjustment(self, net_delta: float, net_gamma: Optional[float] = None):
        """Ignore small delta changes — only trigger on a genuine, configured threshold breach."""
        if abs(net_delta) < self.delta_threshold:
            return False, "Delta within threshold, no action needed"
        return True, f"Net delta {net_delta:+.1f} exceeds threshold +/-{self.delta_threshold}"

    def scenario(self, net_delta: float) -> str:
        """See class docstring above for the sign derivation. "up" = call side under stress (market
        has risen); "down" = put side under stress (market has fallen)."""
        return "up" if net_delta < 0 else "down"

    def generate_candidates(self, position, portfolio_greeks, candidate_fetcher: Callable) -> List[AdjustmentCandidate]:
        """Builds every viable AdjustmentCandidate for the current breach, per the priority list:
        roll the threatened short strike further away, OR roll the calmer side's short strike closer
        (collects more premium and adds offsetting delta), OR convert to an Iron Fly if that
        materially flattens delta, OR add a standalone hedge if no roll alone brings delta back in
        range. `candidate_fetcher(scenario, position, portfolio_greeks) -> list[dict]` supplies the
        actual tradable strikes/premiums (live option chain)."""
        scenario = self.scenario(portfolio_greeks.net_delta)
        raw = candidate_fetcher(scenario, position, portfolio_greeks) or []
        candidates = [AdjustmentCandidate(**c) for c in raw]
        if not candidates:
            candidates.append(AdjustmentCandidate(
                action="no_action",
                description="No viable roll/hedge candidate found within the configured strike/delta range",
                expected_delta_after=portfolio_greeks.net_delta))
        return candidates

    def _score_candidate(self, cand: AdjustmentCandidate) -> AdjustmentCandidate:
        expected_profit_norm = min(max(cand.additional_premium / 500.0, -1.0), 1.0)
        risk_reduction_norm = min(max(cand.risk_reduction_score, 0.0), 1.0)
        additional_premium_norm = min(max(cand.additional_premium / 500.0, -1.0), 1.0)
        margin_norm = 1.0 - min(max(cand.margin_impact / 50000.0, 0.0), 1.0)
        pop_norm = min(max(cand.probability_of_profit, 0.0), 1.0)
        drawdown_norm = 1.0 - min(max(cand.expected_drawdown / 20000.0, 0.0), 1.0)

        w = self.weights
        score = (w["expected_profit"] * expected_profit_norm
                 + w["risk_reduction"] * risk_reduction_norm
                 + w["additional_premium"] * additional_premium_norm
                 + w["margin_impact"] * margin_norm
                 + w["probability_of_profit"] * pop_norm
                 + w["expected_drawdown"] * drawdown_norm)
        cand.score = round(score, 4)
        cand.reasoning = (
            f"{cand.action}: expected_profit={expected_profit_norm:.2f}, risk_reduction={risk_reduction_norm:.2f}, "
            f"premium={additional_premium_norm:.2f}, margin={margin_norm:.2f}, pop={pop_norm:.2f}, "
            f"drawdown={drawdown_norm:.2f} -> composite score {cand.score}"
        )
        return cand

    def recommend(self, position, portfolio_greeks, candidate_fetcher: Callable):
        """Full pipeline: threshold check -> generate candidates -> score -> pick the best.
        Returns (recommended: AdjustmentCandidate | None, all_candidates: list, trigger_reason: str)."""
        trigger, reason = self.needs_adjustment(portfolio_greeks.net_delta, portfolio_greeks.net_gamma)
        if not trigger:
            return None, [], reason
        candidates = self.generate_candidates(position, portfolio_greeks, candidate_fetcher)
        for c in candidates:
            self._score_candidate(c)
        candidates.sort(key=lambda c: c.score, reverse=True)
        best = candidates[0] if candidates else None
        return best, candidates, reason


# ---------------------------------------------------------------------------
# Execution wrapper — thin and deliberately dependency-injected: it takes backend's OWN
# place_basket_orders function as an argument rather than calling the broker directly, so there is
# exactly one place in this file that ever calls the real broker order-placement API.
# ---------------------------------------------------------------------------
@dataclass
class ExecutionResult:
    ok: bool
    orders: list = field(default_factory=list)
    error: str = None


class AdjustmentExecutor:
    def __init__(self, place_orders_fn: Callable[[List[Dict[str, Any]], str, str], list], product="NRML"):
        self.place_orders_fn = place_orders_fn
        self.product = product

    def _build_legs(self, candidate):
        legs = []
        for leg in candidate.legs_to_close:
            legs.append({
                "leg": f"close_{leg['role']}", "tradingsymbol": leg["tradingsymbol"],
                "transaction_type": "BUY" if leg["quantity"] < 0 else "SELL",
                "quantity": abs(leg["quantity"]),
            })
        for leg in candidate.legs_to_open:
            legs.append({
                "leg": f"open_{leg['role']}", "tradingsymbol": leg["tradingsymbol"],
                "transaction_type": "SELL" if leg["role"].startswith("sell") else "BUY",
                "quantity": abs(leg["quantity"]),
            })
        return legs

    def execute(self, candidate, execution_mode="track"):
        """In "track" mode (default, matches the auto-trade engine's paper-trading pattern), no real
        order is sent — fills are simulated immediately so downstream P&L/logging behaves exactly as
        it would live."""
        legs = self._build_legs(candidate)
        if not legs:
            return ExecutionResult(ok=True, orders=[])
        if execution_mode == "track":
            simulated = [{"status": "placed", "order_id": f"TRACK-ADJ-{i}", **leg}
                         for i, leg in enumerate(legs)]
            return ExecutionResult(ok=True, orders=simulated)
        results = self.place_orders_fn(legs, self.product, "MARKET")
        failed = [r for r in results if r.get("status") != "placed"]
        return ExecutionResult(ok=not failed, orders=results,
                                error=None if not failed else f"{len(failed)} leg(s) failed to place")


# ---------------------------------------------------------------------------
# Adjustment logging — structured, append-only JSON-lines log of every adjustment considered/taken.
# ---------------------------------------------------------------------------
class AdjustmentLogger:
    def __init__(self, log_path):
        self.log_path = log_path
        self._lock = threading.Lock()
        d = os.path.dirname(log_path)
        if d:
            os.makedirs(d, exist_ok=True)

    def log_adjustment(self, *, ts, symbol, spot, delta_before, delta_after, action,
                        premium_collected, reason, execution_mode, candidates_considered=None):
        entry = {
            "ts": ts, "symbol": symbol, "spot": spot,
            "delta_before": round(delta_before, 2), "delta_after": round(delta_after, 2),
            "action": action,
            "premium_collected": round(premium_collected, 2) if premium_collected else 0.0,
            "reason": reason, "execution_mode": execution_mode,
            "candidates_considered": candidates_considered or [],
        }
        with self._lock:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        return entry

    def read_recent(self, limit=200):
        if not os.path.exists(self.log_path):
            return []
        with self._lock:
            with open(self.log_path, "r") as f:
                lines = f.readlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        out.reverse()
        return out



# ---------------------------------------------------------------------------
# Delta Neutral Engine config + JSON-file persisted state — mirrors the AUTOTRADE_DEFAULTS /
# persistent configuration pattern used by the dashboard
# (arm/disarm, a fixed set of configurable keys, daily counters that roll over at day-change).
# ---------------------------------------------------------------------------
DELTA_ENGINE_STATE_FILE = os.path.join(os.path.dirname(__file__), "delta_engine_state.json")
_delta_engine_state_lock = threading.Lock()

DELTA_ENGINE_DEFAULTS = {
    "enabled": False,                # armed or not — monitoring/adjustment never runs unless True
    # "track" (default, paper — no real orders) or "live". Explicitly selected execution mode
    # safety pattern exactly: NOT settable via the bulk config route, only via a dedicated ack-gated
    # route, so a stray "Save settings" click can never flip this to real orders.
    "execution_mode": "track",
    "symbols": ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"],
    "delta_threshold": 10.0,         # absolute net portfolio delta that triggers an adjustment
    "gamma_threshold": None,         # optional secondary confirmation; None = delta alone triggers
    "max_adjustments_per_day": 6,
    "max_loss_per_position": 15000.0,
    "max_daily_mtm_loss": 25000.0,
    "min_premium_for_adjustment": 8.0,
    "profit_targets_pct": [25, 50, 70, 90],
    "stop_loss_pct": 200.0,
    "hedge_distance_pct": 2.5,
    "delta_range_low": 0.15,
    "delta_range_high": 0.20,
    "expiry_selection": "nearest",   # "nearest" | "next"
    "spot_poll_seconds": 1,          # underlying spot LTP refresh cadence (the "tick" loop)
    "greeks_poll_seconds": 5,        # option-chain/premium refresh cadence (heavier call, rate-limited)
    # Every risk counter below is keyed BY SYMBOL, not pooled -- a bad day on BANKNIFTY never eats
    # into NIFTY's adjustment budget or vice versa, and each is evaluated independently.
    "adjustments_today_by_symbol": {},   # {"NIFTY": 2, "BANKNIFTY": 0, ...}
    "daily_mtm_by_symbol": {},           # {"NIFTY": -1200.0, ...}
    "paused_symbols_today": [],          # symbols whose OWN daily-loss limit tripped -- new adjustments
                                          # are skipped for just that symbol for the rest of the day;
                                          # profit-target/stop-loss closing still applies to it as normal
    "day": None,
    "last_recommendation": None,
    "last_scan_at": None,
    "last_error": None,
    "disarm_reason": None,
}

DELTA_ENGINE_CONFIGURABLE_KEYS = (
    "symbols", "delta_threshold", "gamma_threshold", "max_adjustments_per_day",
    "max_loss_per_position", "max_daily_mtm_loss", "min_premium_for_adjustment",
    "profit_targets_pct", "stop_loss_pct", "hedge_distance_pct", "delta_range_low",
    "delta_range_high", "expiry_selection", "spot_poll_seconds", "greeks_poll_seconds",
)


def dn_load_state():
    if not os.path.exists(DELTA_ENGINE_STATE_FILE):
        return dict(DELTA_ENGINE_DEFAULTS)
    try:
        with open(DELTA_ENGINE_STATE_FILE, "r") as f:
            state = json.load(f)
        merged = dict(DELTA_ENGINE_DEFAULTS)
        merged.update(state)
        return merged
    except Exception:
        return dict(DELTA_ENGINE_DEFAULTS)


def dn_save_state(state):
    with _delta_engine_state_lock:
        with open(DELTA_ENGINE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Trading-logic enhancements: IV/HV, Expected Move, Probability of Touch,
# Trend Detection (EMA/ADX/RSI), Volatility Regime. All heuristic / best-effort —
# these support decision-making, they don't replace it.
# ---------------------------------------------------------------------------
def classify_iv_hv(iv_pct, hv_pct):
    """IV/HV ratio + label. Rich IV relative to how much the stock actually moves is the
    core edge in option selling — HV alone or IV alone can both be misleading."""
    if not iv_pct or not hv_pct:
        return None
    ratio = iv_pct / hv_pct
    if ratio > 1.30:
        label = "Excellent"
    elif ratio >= 1.10:
        label = "Good"
    elif ratio >= 1.0:
        label = "Fair"
    else:
        label = "Avoid"
    return {"iv_pct": iv_pct, "hv_pct": hv_pct, "ratio": round(ratio, 2), "label": label}


def get_iv_trend_from_history(symbol, history):
    """5/10/20-trading-day IV trend from the same iv_history.json used for IV Rank."""
    series = sorted(history.get(symbol, []), key=lambda p: p["date"])
    if len(series) < 2:
        return None
    today_iv = series[-1]["iv"]

    def n_ago(n):
        idx = len(series) - 1 - n
        return series[idx]["iv"] if idx >= 0 else None

    iv_5, iv_10, iv_20 = n_ago(5), n_ago(10), n_ago(20)
    trend = "Stable"
    if iv_5 is not None:
        if today_iv > iv_5 * 1.05:
            trend = "Rising"
        elif today_iv < iv_5 * 0.95:
            trend = "Falling"
    return {"iv_now": today_iv, "iv_5d_ago": iv_5, "iv_10d_ago": iv_10, "iv_20d_ago": iv_20, "trend": trend}


def expected_move(spot, atm_iv_pct, days_to_expiry):
    """Expected Move = Spot x IV x sqrt(DTE/365). The standard 1-sigma range option sellers use
    to decide whether a strike has enough of a cushion."""
    if spot is None or atm_iv_pct is None or days_to_expiry is None:
        return None
    T = max(days_to_expiry, 0) / 365.0
    em = spot * (atm_iv_pct / 100.0) * math.sqrt(T)
    return {"expected_move": round(em, 2), "expected_move_pct": round(em / spot * 100, 2) if spot else None,
            "upper": round(spot + em, 2), "lower": round(spot - em, 2)}


def probability_of_touch(delta):
    """Standard trading-desk approximation: POT is roughly 2x the delta of the strike (since
    touching the strike at any point is roughly twice as likely as finishing beyond it at expiry)."""
    if delta is None:
        return None
    return round(min(100.0, abs(delta) * 2 * 100), 1)


def _ema(values, period):
    if len(values) < period:
        return None
    ema = float(np.mean(values[:period]))
    alpha = 2.0 / (period + 1)
    for v in values[period:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def _rma(values, period):
    """Wilder's smoothed moving average, used by ADX."""
    if len(values) < period:
        return np.array([])
    rma = np.zeros(len(values) - period + 1)
    rma[0] = np.mean(values[:period])
    alpha = 1.0 / period
    for i in range(1, len(rma)):
        rma[i] = rma[i - 1] + alpha * (values[period - 1 + i] - rma[i - 1])
    return rma


def _adx(highs, lows, closes, period=14):
    if len(closes) < period * 2 + 1:
        return None
    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = np.maximum(highs[1:] - lows[1:],
                     np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
    atr_rma, plus_rma, minus_rma = _rma(tr, period), _rma(plus_dm, period), _rma(minus_dm, period)
    n = min(len(atr_rma), len(plus_rma), len(minus_rma))
    if n == 0:
        return None
    atr_safe = np.where(atr_rma[-n:] == 0, 1e-9, atr_rma[-n:])
    plus_di = 100 * plus_rma[-n:] / atr_safe
    minus_di = 100 * minus_rma[-n:] / atr_safe
    dx = 100 * np.abs(plus_di - minus_di) / np.where((plus_di + minus_di) == 0, 1e-9, (plus_di + minus_di))
    if len(dx) < period:
        return float(np.mean(dx))
    adx_series = _rma(dx, period)
    return float(adx_series[-1]) if len(adx_series) else float(np.mean(dx))


def resolve_token_for_symbol(symbol):
    """Shared instrument-token lookup for stocks AND indices, including BSE SENSEX."""
    symbol = symbol.upper()
    if symbol in INDEX_SYMBOLS:
        wanted = INDEX_SYMBOLS[symbol].split(":")[1]
        exchange = INDEX_SYMBOLS[symbol].split(":")[0]
        instruments = get_bse_instruments() if exchange == "BSE" else get_instruments()[1]
        for i in instruments:
            if i.get("segment") == "INDICES" and i.get("tradingsymbol") == wanted:
                return i["instrument_token"], None
        return None, f"Could not resolve index token for {symbol}"
    _, nse = get_instruments()
    matches = [i for i in nse if i["exchange"] == "NSE" and i["tradingsymbol"] == symbol]
    if not matches:
        return None, f"{symbol} not found on NSE"
    return matches[0]["instrument_token"], None


def classify_trend_regime(ema20, ema50, ema100, adx, rsi):
    trending = adx is not None and adx >= 25
    bullish_stack = ema50 is not None and ema20 > ema50 and (ema100 is None or ema50 > ema100)
    bearish_stack = ema50 is not None and ema20 < ema50 and (ema100 is None or ema50 < ema100)
    if trending and bullish_stack and rsi is not None and rsi > 55:
        return "Strong Uptrend", True
    if trending and bearish_stack and rsi is not None and rsi < 45:
        return "Strong Downtrend", True
    if adx is not None and adx < 20 and rsi is not None and 40 <= rsi <= 60:
        return "Range Bound", False
    if adx is not None and 20 <= adx < 25:
        return "Transitioning", False
    return "Volatile / Mixed", False


def get_trend_regime(symbol):
    """EMA20/50/100 + ADX14 + RSI14 off ~220 days of daily candles. Premium selling (Iron
    Condor/Strangle) works best in Range Bound markets — strong trends should generally be
    avoided or handled with directional spreads instead."""
    token, err = resolve_token_for_symbol(symbol)
    if err:
        return {"error": err}
    to_date = now_ist()
    from_date = to_date - timedelta(days=220)
    try:
        candles = kite.historical_data(token, from_date, to_date, "day")
    except Exception as e:
        return {"error": f"Historical data fetch failed: {e}"}
    if len(candles) < 30:
        return {"error": "Not enough historical data to classify trend (need 30+ trading days)"}
    closes = np.array([c["close"] for c in candles])
    highs = np.array([c["high"] for c in candles])
    lows = np.array([c["low"] for c in candles])
    ema20, ema50, ema100 = _ema(closes, 20), _ema(closes, 50), _ema(closes, 100)
    adx, rsi = _adx(highs, lows, closes, 14), _rsi(closes, 14)
    if ema20 is None or adx is None or rsi is None:
        return {"error": "Not enough historical data for a reliable trend read"}
    regime, avoid_selling = classify_trend_regime(ema20, ema50, ema100, adx, rsi)
    return {"symbol": symbol.upper(), "ema20": round(ema20, 2),
            "ema50": round(ema50, 2) if ema50 is not None else None,
            "ema100": round(ema100, 2) if ema100 is not None else None,
            "adx14": round(adx, 1), "rsi14": round(rsi, 1),
            "regime": regime, "avoid_premium_selling": avoid_selling,
            "note": "Heuristic (EMA slope + ADX strength + RSI), not a guaranteed signal."}


def get_india_vix():
    try:
        q = kite_quote_bulk(["NSE:INDIA VIX"])["NSE:INDIA VIX"]
        return q["last_price"], None
    except Exception as e:
        return None, str(e)


def classify_volatility_regime(vix, iv_rank_pct):
    """Commonly-cited India VIX bands. Thresholds are approximate conventions, not a rule
    from any exchange — re-check against current market context."""
    if vix is None:
        return {"label": "Unknown", "recommendation": "Suitable",
                "note": "India VIX unavailable right now; regime not classified."}
    if vix < 12:
        label = "Low Volatility"
    elif vix < 18:
        label = "Normal"
    elif vix < 25:
        label = "High Volatility"
    else:
        label = "Extreme"
    if label == "Low Volatility":
        rec = "Reduce Size" if (iv_rank_pct is not None and iv_rank_pct < 30) else "Suitable"
    elif label == "Normal":
        rec = "Suitable"
    elif label == "High Volatility":
        rec = "Reduce Size"
    else:
        rec = "Avoid"
    return {"label": label, "india_vix": round(vix, 2), "recommendation": rec}


def suggest_strategy_family(iv_rank_pct, trend):
    """Simple rule table: strong trend -> directional spread; otherwise pick the non-directional
    structure that fits the current IV regime."""
    trend_label = trend.get("regime") if trend and not trend.get("error") else None
    if trend_label in ("Strong Uptrend", "Strong Downtrend"):
        base = "Bull Put Spread (directional credit spread)" if trend_label == "Strong Uptrend" \
            else "Bear Call Spread (directional credit spread)"
        return {"suggested": base,
                "reason": f"{trend_label} detected — avoid non-directional premium selling "
                          f"(Iron Condor/Strangle) into a strong trend."}
    if iv_rank_pct is None:
        return {"suggested": None,
                "reason": "IV rank unavailable (run the Screener first) — can't classify IV regime yet."}
    if iv_rank_pct >= 70:
        return {"suggested": "Short Strangle or Iron Fly",
                "reason": f"IV rank {iv_rank_pct}% is high — rich premium supports a more aggressive structure."}
    if iv_rank_pct >= 40:
        return {"suggested": "Iron Condor",
                "reason": f"IV rank {iv_rank_pct}% is moderate — a defined-risk Iron Condor is the standard fit."}
    return {"suggested": "Single-side Credit Spread, or skip",
            "reason": f"IV rank {iv_rank_pct}% is low — premium is thin here."}


def recommended_position_size(capital, risk_pct, max_loss_per_lot):
    if not capital or not risk_pct or not max_loss_per_lot or max_loss_per_lot <= 0:
        return None
    max_risk_amount = capital * risk_pct / 100.0
    lots = int(max_risk_amount // max_loss_per_lot)
    return {"max_risk_amount": round(max_risk_amount, 2), "recommended_lots": max(lots, 0)}


def extract_price(quote):
    """Fall back to bid/ask midpoint when last_price is 0 (illiquid/deep-OTM contracts that
    haven't traded today but still have resting orders) instead of silently dropping the strike."""
    if not quote:
        return None
    ltp = quote.get("last_price", 0)
    if ltp and ltp > 0:
        return ltp
    depth = quote.get("depth", {}) or {}
    buys = [b for b in depth.get("buy", []) if b.get("price", 0) > 0]
    sells = [s for s in depth.get("sell", []) if s.get("price", 0) > 0]
    bid = buys[0]["price"] if buys else None
    ask = sells[0]["price"] if sells else None
    if bid and ask:
        return (bid + ask) / 2
    if ask:
        return ask
    if bid:
        return bid
    return None


def quote_stats(q):
    """Return best bid/ask, mid, spread %, top-level quantity and quote volume from a Kite quote."""
    if not q:
        return {"bid": None, "ask": None, "mid": None, "spread_pct": None, "bid_qty": 0, "ask_qty": 0,
                "volume": 0, "oi": 0}
    depth = q.get("depth", {}) or {}
    buys = [x for x in depth.get("buy", []) if x.get("price", 0) > 0]
    sells = [x for x in depth.get("sell", []) if x.get("price", 0) > 0]
    bid = buys[0]["price"] if buys else None
    ask = sells[0]["price"] if sells else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None else extract_price(q)
    spread = ((ask - bid) / mid * 100) if bid is not None and ask is not None and mid else None
    return {"bid": bid, "ask": ask, "mid": mid, "spread_pct": spread,
            "bid_qty": int(buys[0].get("quantity", 0)) if buys else 0,
            "ask_qty": int(sells[0].get("quantity", 0)) if sells else 0,
            "volume": int(q.get("volume", 0) or 0), "oi": int(q.get("oi", 0) or 0)}


def option_quality(o, q):
    st = quote_stats(q)
    return {**o, "ltp": extract_price(q), "bid": st["bid"], "ask": st["ask"],
            "mid": st["mid"], "spread_pct": round(st["spread_pct"], 2) if st["spread_pct"] is not None else None,
            "volume": st["volume"], "oi": st["oi"], "bid_qty": st["bid_qty"], "ask_qty": st["ask_qty"]}


def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def model_expiry_probability_between(spot, lower, upper, iv_pct, days):
    """Approximate risk-neutral probability of expiry spot between two prices using lognormal BS distribution."""
    if not all(v is not None for v in (spot, lower, upper, iv_pct, days)) or spot <= 0 or lower <= 0 or upper <= lower or iv_pct <= 0:
        return None
    T = max(days, 1) / 365.0
    sigma = iv_pct / 100.0
    vol = sigma * math.sqrt(T)
    mu = math.log(spot) + (RISK_FREE_RATE - 0.5 * sigma * sigma) * T
    if vol <= 0:
        return None
    z_lo = (math.log(lower) - mu) / vol
    z_hi = (math.log(upper) - mu) / vol
    return round(max(0.0, min(1.0, normal_cdf(z_hi) - normal_cdf(z_lo))) * 100, 1)


def score_band(value, bands):
    for threshold, score in bands:
        if value <= threshold:
            return score
    return bands[-1][1]


def ic_event_risk(symbol, expiry_str, headlines=None):
    """Only use data actually available to this application: seeded/manual event calendar + headline scan.
    Zerodha does not provide an earnings calendar through Kite Connect, so this is deliberately not presented
    as an earnings-date guarantee."""
    flags = []
    expiry_event = get_event_before_expiry(expiry_str)
    if expiry_event:
        flags.append({"type": "calendar", "label": expiry_event.get("label"), "date": expiry_event.get("date")})
    risky_words = ("result", "earnings", "dividend", "bonus", "split", "merger", "demerger", "acquisition",
                   "buyback", "order", "regulatory", "court", "approval", "rating", "downgrade", "upgrade")
    for h in headlines or []:
        title = (h.get("title") or "").lower()
        if any(w in title for w in risky_words):
            flags.append({"type": "headline", "label": h.get("title"), "date": h.get("pub_date")})
    score = 100
    if any(x["type"] == "calendar" for x in flags): score -= 50
    if any(x["type"] == "headline" for x in flags): score -= 25
    return max(0, score), flags


def build_ic_candidate_from_chain(symbol, spot, expiry, chain, target_delta=0.18, wing_width_pct=None,
                                   lots=1, force_symmetric=False, call_delta=None, put_delta=None):
    """Build an IC from already-quoted chain data. Optimises liquidity/economics while preserving
    the user's target delta; this is the core used by both the screener and strategy builder."""
    calls = sorted([o for o in chain if o["instrument_type"] == "CE" and o.get("delta") is not None], key=lambda x: x["strike"])
    puts = sorted([o for o in chain if o["instrument_type"] == "PE" and o.get("delta") is not None], key=lambda x: x["strike"])
    if not calls or not puts: return None
    ct = float(call_delta if call_delta is not None else target_delta)
    pt = float(put_delta if put_delta is not None else target_delta)
    # Keep the requested short-delta meaning intact.  In normal mode the two short
    # legs are searched within +/-0.03 delta of the requested target.  This prevents
    # a 0.25-delta call from replacing a 0.18-delta call simply because its premium is
    # larger.  Any permitted asymmetry is then explicitly scored below.
    delta_tolerance = 0.03
    call_candidates = [o for o in calls if max(0.10,ct-delta_tolerance) <= abs(o["delta"]) <= min(0.30,ct+delta_tolerance)]
    put_candidates = [o for o in puts if max(0.10,pt-delta_tolerance) <= abs(o["delta"]) <= min(0.30,pt+delta_tolerance)]
    if not call_candidates: call_candidates = [min(calls, key=lambda o: abs(abs(o["delta"]) - ct))]
    if not put_candidates: put_candidates = [min(puts, key=lambda o: abs(abs(o["delta"]) - pt))]
    best = None
    widths = [wing_width_pct] if wing_width_pct else list(IC_WING_WIDTHS_PCT)
    for sc in call_candidates:
        for sp in put_candidates:
            for w in widths:
                ca = [o for o in calls if o["strike"] > sc["strike"]]
                pb = [o for o in puts if o["strike"] < sp["strike"]]
                if not ca or not pb: continue
                lc = min(ca, key=lambda o: abs(o["strike"] - sc["strike"] * (1 + w)))
                lp = min(pb, key=lambda o: abs(o["strike"] - sp["strike"] * (1 - w)))
                if any(o.get("mid") is None for o in (sc, sp, lc, lp)): continue
                credit = (sc["mid"] + sp["mid"]) - (lc["mid"] + lp["mid"])
                cw, pw = lc["strike"] - sc["strike"], sp["strike"] - lp["strike"]
                max_loss = max(cw, pw) - credit
                if credit <= 0 or max_loss <= 0: continue
                dte = max((expiry - now_ist().date()).days, 1)
                atm_iv = np.mean([o.get("iv", 0) for o in chain if abs(o["strike"]-spot) == min(abs(x["strike"]-spot) for x in chain)])
                em = expected_move(spot, atm_iv, dte)
                if not em: continue
                ce_cushion = (sc["strike"] - spot) / em["expected_move"] if em["expected_move"] else 0
                pe_cushion = (spot - sp["strike"]) / em["expected_move"] if em["expected_move"] else 0
                be_low, be_high = sp["strike"] - credit, sc["strike"] + credit
                pop = model_expiry_probability_between(spot, be_low, be_high, atm_iv, dte)
                leg_spreads = [o.get("spread_pct") for o in (sc, sp, lc, lp) if o.get("spread_pct") is not None]
                liq_score = sum(min(100, max(0, 100 - x * 10)) for x in leg_spreads) / len(leg_spreads) if leg_spreads else 0
                oi_score = sum(min(100, math.log10(max(o.get("oi",0),1)) / 4 * 100) for o in (sc,sp,lc,lp)) / 4
                liquidity = 0.65 * liq_score + 0.35 * oi_score
                econ = min(100, max(0, (credit / max_loss) / 0.5 * 100))
                dte_score = 100 if IC_PREFERRED_DTE_LOW <= dte <= IC_PREFERRED_DTE_HIGH else max(40, 100 - abs(dte - 28) * 3)

                # Risk-adjust the premium advantage.  More premium is useful only if
                # it is accompanied by enough safety cushion.  A closer short strike
                # therefore has to earn its extra premium rather than winning on raw
                # credit alone.
                min_cushion = min(ce_cushion, pe_cushion)
                cushion_component = score_band(min_cushion, [(0.75,20),(0.90,35),(1.00,50),(1.15,70),(1.30,85),(1.50,95),(999,100)])
                delta_gap = abs(abs(sc.get("delta", 0)) - abs(sp.get("delta", 0)))
                delta_target_error = (abs(abs(sc.get("delta", 0)) - ct) + abs(abs(sp.get("delta", 0)) - pt)) / 2.0
                delta_symmetry_score = max(0, 100 - (delta_gap / 0.04) * 100)
                delta_target_score = max(0, 100 - (delta_target_error / 0.03) * 100)
                delta_score = 0.65 * delta_symmetry_score + 0.35 * delta_target_score
                # Effective economics: credit/max-loss is discounted when cushion is poor.
                risk_adjusted_econ = econ * (0.45 + 0.55 * cushion_component / 100.0)
                score = (0.25 * delta_score + 0.30 * cushion_component + 0.20 * risk_adjusted_econ +
                         0.15 * liquidity + 0.10 * dte_score)
                candidate = {"sell_call": sc, "buy_call": lc, "sell_put": sp, "buy_put": lp,
                             "credit": credit, "max_loss": max_loss, "call_wing": cw, "put_wing": pw,
                             "expected_move": em, "ce_cushion": ce_cushion, "pe_cushion": pe_cushion,
                             "probability_of_profit": pop, "liquidity_score": liquidity,
                             "economics_score": econ, "risk_adjusted_economics_score": risk_adjusted_econ,
                             "dte_score": dte_score, "delta_gap": delta_gap,
                             "delta_target_error": delta_target_error, "delta_symmetry_score": delta_score,
                             "selection_score": score, "atm_iv": atm_iv}
                if best is None or candidate["selection_score"] > best["selection_score"]: best = candidate
    return best


def fetch_chain_quotes_for_expiry(symbol, expiry, opts, spot_override=None, quotes_override=None):
    """Build one quoted option chain. If quotes_override is supplied, NO REST quote request is
    made here; this is what lets the IC screener batch thousands of option instruments into a
    small number of Kite /quote calls instead of making one request per stock/expiry."""
    keys = [f"NFO:{o['tradingsymbol']}" for o in opts]
    quotes = quotes_override if quotes_override is not None else kite_quote_bulk(keys, chunk_size=500, retries=1)
    spot, err = (spot_override, None) if spot_override else get_spot_price(symbol)
    if err: return None, err
    T = max((expiry-now_ist().date()).days, 0) / 365.0
    chain=[]
    for o in opts:
        q=quotes.get(f"NFO:{o['tradingsymbol']}")
        mid=quote_stats(q).get("mid")
        if mid is None: continue
        iv=implied_vol(mid, spot, o['strike'], T, o['instrument_type'])
        if iv is None or not math.isfinite(iv) or iv <= 0:
            continue
        chain.append(option_quality({**o, "iv": round(iv*100,1), "delta": round(bs_delta(spot,o['strike'],T,RISK_FREE_RATE,iv,o['instrument_type']),3)}, q))
    return {"spot":spot,"T":T,"chain":chain,"lot_size":opts[0]["lot_size"] if opts else None}, None



def extract_bid_ask(quote):
    """Return Zerodha best bid and best ask from live market depth."""
    if not quote:
        return None, None
    depth = quote.get("depth", {}) or {}
    buys = [b for b in (depth.get("buy") or []) if float(b.get("price") or 0) > 0]
    sells = [s for s in (depth.get("sell") or []) if float(s.get("price") or 0) > 0]
    bid = float(buys[0]["price"]) if buys else None
    ask = float(sells[0]["price"]) if sells else None
    return bid, ask


def recommended_limit_price(transaction_type, bid, ask, ltp=None):
    """Marketable LIMIT price: BUY uses best Ask; SELL uses best Bid."""
    if transaction_type == "BUY":
        return ask if ask is not None else ltp
    return bid if bid is not None else ltp


def refresh_execution_quotes(position):
    """Fetch fresh LTP/Bid/Ask and recommended LIMIT prices for all entry legs."""
    quantity = position.get("quantity", position["lot_size"])
    leg_keys = leg_keys_for(position)
    inst_keys = [f"NFO:{position['legs'][k]['tradingsymbol']}" for k in leg_keys]
    quotes = kite_quote_bulk(inst_keys, force_refresh=True)

    orders = []
    for k in leg_keys:
        leg = position["legs"][k]
        txn = "SELL" if k.startswith("sell") else "BUY"
        q = quotes.get(f"NFO:{leg['tradingsymbol']}") or {}
        ltp = q.get("last_price")
        bid, ask = extract_bid_ask(q)
        auto_price = recommended_limit_price(txn, bid, ask, ltp)
        orders.append({
            "leg": k,
            "tradingsymbol": leg["tradingsymbol"],
            "transaction_type": txn,
            "quantity": quantity,
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "recommended_limit_price": auto_price,
            "reference_price": auto_price,
            "price_source": (
                "BID" if txn == "SELL" and bid is not None else
                "ASK" if txn == "BUY" and ask is not None else
                "LTP_FALLBACK"
            ),
        })
    return orders

def compute_margin(legs_for_margin, quantity, product="NRML"):
    """legs_for_margin: list of {'tradingsymbol': ..., 'transaction_type': 'BUY'/'SELL'}.
    Returns (margin_amount_or_None, error_message_or_None). Uses Kite's basket margin API
    where available (accounts for the margin benefit of hedged combos like an iron condor);
    falls back to summing individual order margins if the basket endpoint isn't available."""
    order_params = []
    for lg in legs_for_margin:
        order_params.append({
            "exchange": "NFO", "tradingsymbol": lg["tradingsymbol"],
            "transaction_type": lg["transaction_type"], "variety": "regular",
            "product": product, "order_type": "MARKET", "quantity": quantity,
        })
    try:
        if hasattr(kite, "basket_order_margins"):
            resp = kite.basket_order_margins(order_params, consider_positions=False)
            total = None
            if isinstance(resp, dict):
                section = resp.get("final") or resp.get("initial") or resp
                if isinstance(section, dict):
                    total = section.get("total")
            if total is None:
                return None, "Unexpected response shape from basket margin API"
            return round(total, 2), None
        else:
            resp = kite.order_margins(order_params)
            total = sum(r.get("total", 0) for r in resp)
            return round(total, 2), None
    except Exception as e:
        return None, str(e)


def estimate_charges(orders):
    """orders: list of {'price': float, 'quantity': int, 'transaction_type': 'BUY'/'SELL'}.
    Returns an approximate total charges figure (brokerage + STT + exchange fee + SEBI fee +
    GST + stamp duty) for placing this exact basket as ONE side of a trade (i.e. call this once
    for entry orders, and again separately for exit orders, to get a full round-trip estimate).
    This is a planning estimate only — always verify against your actual Kite contract note."""
    total_brokerage = total_stt = total_exchange = total_sebi = total_stamp = 0.0
    for o in orders:
        turnover = float(o["price"]) * int(o["quantity"])
        if turnover <= 0:
            continue
        brokerage = min(CHARGES["brokerage_flat"], CHARGES["brokerage_pct"] * turnover)
        exchange_txn = CHARGES["exchange_txn_pct"] * turnover
        sebi = CHARGES["sebi_pct"] * turnover
        total_brokerage += brokerage
        total_exchange += exchange_txn
        total_sebi += sebi
        if o["transaction_type"] == "SELL":
            total_stt += CHARGES["stt_sell_pct"] * turnover
        else:
            total_stamp += CHARGES["stamp_duty_buy_pct"] * turnover

    gst = CHARGES["gst_pct"] * (total_brokerage + total_exchange + total_sebi)
    total = total_brokerage + total_stt + total_exchange + total_sebi + gst + total_stamp

    return {
        "brokerage": round(total_brokerage, 2), "stt": round(total_stt, 2),
        "exchange_txn_charges": round(total_exchange, 2), "sebi_fee": round(total_sebi, 2),
        "gst": round(gst, 2), "stamp_duty": round(total_stamp, 2), "total": round(total, 2),
    }


# ---------------------------------------------------------------------------
# Kite login flow
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Historical breakout diagnostics
# ---------------------------------------------------------------------------


@app.route("/api/login-url")
def login_url():
    return jsonify({"url": kite.login_url()})


@app.route("/api/callback")
def callback():
    request_token = request.args.get("request_token")
    if not request_token:
        return "Login failed: no request_token received.", 400
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    SESSION["access_token"] = data["access_token"]
    SESSION["logged_in_at"] = now_ist().isoformat()
    kite.set_access_token(SESSION["access_token"])
    # Do not wait for the next 30-second scanner tick: wake Stock AI immediately so
    # full-F&O discovery and resumable deep-history collection begin after login.
    try:
        if 'STOCK_DESK' in globals():threading.Thread(target=STOCK_DESK.cycle,daemon=True,name='stock-ai-login-wake').start()
    except Exception:pass
    return redirect("/")


@app.route("/api/session-status")
def session_status():
    """Actively validates the token (not just checks it's present) so a stale/expired token
    can't keep showing 'Connected' after it's no longer valid — this is the fix for the tool
    showing 'connected' even when Zerodha has actually invalidated the session."""
    if not SESSION["access_token"]:
        return jsonify({"logged_in": False, "logged_in_at": None})
    try:
        kite.set_access_token(SESSION["access_token"])
        kite.profile()  # cheap call just to confirm the token still actually works
        return jsonify({"logged_in": True, "logged_in_at": SESSION["logged_in_at"]})
    except TokenException:
        SESSION["access_token"] = None
        SESSION["logged_in_at"] = None
        return jsonify({"logged_in": False, "logged_in_at": None, "session_expired": True})
    except Exception:
        # network hiccup or similar — don't log the user out for a transient error,
        # just report what we last knew
        return jsonify({"logged_in": True, "logged_in_at": SESSION["logged_in_at"],
                         "warning": "Could not verify token freshness right now (network issue?)."})


def require_session():
    if not SESSION["access_token"]:
        return False
    kite.set_access_token(SESSION["access_token"])
    return True


@app.route("/api/logout", methods=["POST"])
def logout():
    """Clears the stored session so the dashboard stops using this token — does NOT invalidate
    the token on Zerodha's side (Kite has no logout API), it just makes this app forget it."""
    SESSION["access_token"] = None
    SESSION["logged_in_at"] = None
    logger.info("User logged out — session cleared.")
    return jsonify({"ok": True})


@app.errorhandler(TokenException)
def handle_token_exception(e):
    """Catches an expired/invalid token from ANY route (whichever endpoint happened to hit
    Zerodha with a stale token), clears the stored session, and tells the frontend to show the
    login button again — instead of a generic 500 error or a UI that silently keeps showing
    'Connected' while every data call quietly fails."""
    SESSION["access_token"] = None
    SESSION["logged_in_at"] = None
    logger.warning("TokenException caught — clearing session and asking frontend to reconnect.")
    return jsonify({"error": "session_expired", "session_expired": True,
                     "message": "Your Zerodha session has expired. Please reconnect."}), 401


@app.errorhandler(Exception)
def handle_any_exception(e):
    """Safety net: ANY unhandled exception anywhere in the app returns valid JSON instead of an
    HTML error page. Without this, a bug in one route (e.g. a new feature touching old saved
    data) crashes with a raw 500 HTML page, which breaks every frontend '.json()' call with a
    confusing 'SyntaxError: string did not match expected pattern' instead of a clear message."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e  # let normal HTTP errors (404, 405, etc.) behave as Flask normally would
    logger.exception("Unhandled exception on %s %s", request.method, request.path)
    return jsonify({"error": "internal_error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Instrument cache
# ---------------------------------------------------------------------------
def get_bfo_instruments(force=False):
    now = now_ist()
    if force or INSTRUMENT_CACHE.get("bfo") is None or INSTRUMENT_CACHE.get("fetched_at") is None or now - INSTRUMENT_CACHE["fetched_at"] > timedelta(hours=6):
        INSTRUMENT_CACHE["bfo"] = kite.instruments("BFO")
    return INSTRUMENT_CACHE["bfo"]


def get_bse_instruments(force=False):
    now = now_ist()
    if force or INSTRUMENT_CACHE.get("bse") is None or INSTRUMENT_CACHE.get("fetched_at") is None or now - INSTRUMENT_CACHE["fetched_at"] > timedelta(hours=6):
        INSTRUMENT_CACHE["bse"] = kite.instruments("BSE")
    return INSTRUMENT_CACHE["bse"]


def option_exchange_for_symbol(symbol):
    return INDEX_OPTION_EXCHANGE.get(symbol.upper(), "NFO")


def option_segment_for_symbol(symbol):
    return OPTION_EXCHANGE_SEGMENT.get(option_exchange_for_symbol(symbol), "NFO-OPT")


def get_option_instruments_for_symbol(symbol):
    exchange = option_exchange_for_symbol(symbol)
    if exchange == "BFO":
        instruments = get_bfo_instruments()
    else:
        instruments, _ = get_instruments()
    return exchange, [i for i in instruments if i.get("name") == symbol.upper() and i.get("segment") == option_segment_for_symbol(symbol)]


def get_instruments(force=False):
    now = now_ist()
    if (force or INSTRUMENT_CACHE["fetched_at"] is None or
            now - INSTRUMENT_CACHE["fetched_at"] > timedelta(hours=6)):
        INSTRUMENT_CACHE["nfo"] = kite.instruments("NFO")
        INSTRUMENT_CACHE["nse"] = kite.instruments("NSE")
        INSTRUMENT_CACHE["fetched_at"] = now
    return INSTRUMENT_CACHE["nfo"], INSTRUMENT_CACHE["nse"]


def fo_stock_universe(force=False):
    """Return only genuine NSE cash equities that currently have NFO options.

    Do not infer "stock" merely because an NFO option name is not one of our
    configured headline indices. NSE also lists derivatives on other indices
    (for example NIFTYFPI / NIFTYNXT50), which do not resolve as NSE cash
    equities and can otherwise leave the history backfill permanently partial.
    The cash-market EQ intersection is the authoritative stock filter.
    """
    nfo, nse = get_instruments(force=force)
    cash_equities = {
        str(i.get("tradingsymbol") or "").upper()
        for i in nse
        if i.get("exchange") == "NSE"
        and i.get("segment") == "NSE"
        and i.get("instrument_type") == "EQ"
        and i.get("tradingsymbol")
    }
    names = {
        str(ins.get("name") or "").upper()
        for ins in nfo
        if ins.get("segment") == "NFO-OPT"
        and str(ins.get("name") or "").upper() in cash_equities
    }
    return sorted(names)


@app.route("/api/refresh-data", methods=["POST"])
def refresh_data():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    get_instruments(force=True)
    SCREENER_CACHE["results"] = None
    SCREENER_CACHE["fetched_at"] = None
    IC_SCREENER_CACHE["results"] = None
    IC_SCREENER_CACHE["fetched_at"] = None
    return jsonify({"ok": True, "message": "Instrument cache cleared. Re-run the screener to refresh rankings."})


# ---------------------------------------------------------------------------
# Event calendar — avoid-new-entry warnings and existing-position event flags
# ---------------------------------------------------------------------------
@app.route("/api/event-calendar")
def event_calendar_route():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    days_ahead = int(request.args.get("days", 45))
    events = get_upcoming_events(days_ahead)
    return jsonify({"events": events,
                     "note": "Hand-maintained calendar (RBI/Fed dates from published sources — re-verify at "
                             "rbi.org.in and federalreserve.gov). Election result days and geopolitical events "
                             "are not predictable in advance and are not auto-tracked — add them yourself below "
                             "as they become known."})


@app.route("/api/event-calendar/add", methods=["POST"])
def event_calendar_add():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    date_str = body.get("date")
    if not date_str:
        return jsonify({"error": "date is required (YYYY-MM-DD)"}), 400
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format, expected YYYY-MM-DD"}), 400
    events = load_event_calendar()
    events.append({"date": date_str, "label": body.get("label", "Custom event"),
                    "type": body.get("type", "custom"), "source": "user-added"})
    save_event_calendar(events)
    return jsonify({"ok": True})


@app.route("/api/event-calendar/<int:index>", methods=["DELETE"])
def event_calendar_delete(index):
    events = load_event_calendar()
    if 0 <= index < len(events):
        events.pop(index)
        save_event_calendar(events)
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid event index"}), 400


# ---------------------------------------------------------------------------
# Volatility screener
# ---------------------------------------------------------------------------
def historical_vol_and_atr(nse_token, days=60):
    to_date = now_ist()
    from_date = to_date - timedelta(days=days + 20)
    candles = kite.historical_data(nse_token, from_date, to_date, "day")
    if len(candles) < 10:
        return None, None, None
    closes = np.array([c["close"] for c in candles[-days:]])
    highs = np.array([c["high"] for c in candles[-days:]])
    lows = np.array([c["low"] for c in candles[-days:]])
    returns = np.diff(np.log(closes))
    hv_annualized = float(np.std(returns) * math.sqrt(252) * 100)
    tr = np.maximum(highs[1:] - lows[1:],
                     np.maximum(abs(highs[1:] - closes[:-1]), abs(lows[1:] - closes[:-1])))
    atr = float(np.mean(tr[-14:]))
    last_close = float(closes[-1])
    atr_pct = atr / last_close * 100
    return hv_annualized, atr_pct, last_close


@app.route("/api/fo-universe")
def fo_universe_route():
    """Indices + the real, current list of individual F&O stocks -- used by the Auto Trade tab's
    universe picker so you're selecting symbols that actually have tradable options, instead of
    typing a symbol blind and having it silently fail to scan."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    try:
        stocks = fo_stock_universe()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"indices": list(INDEX_SYMBOLS.keys()), "stocks": stocks})


@app.route("/api/screener")
def screener():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    limit = int(request.args.get("limit", 25))
    force = request.args.get("force", "false").lower() == "true"
    include_news = request.args.get("news", "true").lower() == "true"
    today = now_ist().date()
    today_str = str(today)

    universe = fo_stock_universe(force=force)
    nfo, nse = get_instruments(force=force)
    symbol_to_token = {i["tradingsymbol"]: i["instrument_token"] for i in nse if i["exchange"] == "NSE"}

    # --- Pass 1: calmness (HV/ATR from daily closes) — same as before ---
    results = []
    for name in universe:
        token = symbol_to_token.get(name)
        if not token:
            continue
        try:
            hv, atr_pct, ltp = historical_vol_and_atr(token)
        except Exception:
            continue
        if hv is None:
            continue
        results.append({"symbol": name, "ltp": round(ltp, 2),
                         "hv_annualized_pct": round(hv, 2), "atr_pct_of_price": round(atr_pct, 2)})
        if len(results) >= 300:
            break

    # Seed shared spot cache from the already-computed historical close. This prevents
    # the later option-chain scan from making one NSE Quote request per stock.
    seed_spot_cache_from_prices(results)
    # --- Pass 2: ATM IV + liquidity, batched across all candidates at once ---
    candidates = [{"symbol": r["symbol"], "last_close": r["ltp"]} for r in results]
    try:
        iv_liquidity = get_atm_iv_and_liquidity_bulk(candidates, nfo)
    except Exception as e:
        iv_liquidity = {}
        logger.warning(f"ATM IV/liquidity batch fetch failed: {e}")

    iv_history = load_iv_history()
    for r in results:
        info = iv_liquidity.get(r["symbol"])
        if not info:
            r["atm_iv_pct"] = None
            r["atm_oi_total"] = None
            r["atm_spread_pct"] = None
            r["iv_rank_pct"] = None
            r["iv_rank_history_days"] = 0
            r["liquidity_ok"] = False
            continue
        r.update(info)
        rank_pct, hist_days = update_iv_history_and_get_rank(r["symbol"], info["atm_iv_pct"], iv_history, today_str)
        r["iv_rank_pct"] = rank_pct
        r["iv_rank_history_days"] = hist_days
        r["liquidity_ok"] = (info["atm_oi_total"] >= MIN_ATM_TOTAL_OI and
                              info["atm_spread_pct"] is not None and info["atm_spread_pct"] <= MAX_ATM_SPREAD_PCT)
        r["iv_hv"] = classify_iv_hv(r.get("atm_iv_pct"), r.get("hv_annualized_pct"))
    save_iv_history(iv_history)

    # --- Pass 3: best-effort F&O ban list, excludes banned symbols from top picks ---
    ban_symbols, ban_error = get_fo_ban_list()
    for r in results:
        r["fo_banned_today"] = (ban_symbols is not None and r["symbol"] in ban_symbols)

    # --- Composite score: rich IV (same-day cross-sectional percentile, since most stocks
    # won't have 20+ days of stored history yet) + calm underlying + tradeable liquidity ---
    all_iv = [r["atm_iv_pct"] for r in results]
    all_hv = [r["hv_annualized_pct"] for r in results]
    all_atr = [r["atr_pct_of_price"] for r in results]
    all_oi = [r["atm_oi_total"] for r in results if r["atm_oi_total"]]
    all_spread = [r["atm_spread_pct"] for r in results if r["atm_spread_pct"] is not None]

    eligible = []
    for r in results:
        iv_richness_pct = (r["iv_rank_pct"] if r["iv_rank_pct"] is not None
                            else _percentile_rank(r["atm_iv_pct"], all_iv))
        calm_hv_pct = 100 - _percentile_rank(r["hv_annualized_pct"], all_hv)
        calm_atr_pct = 100 - _percentile_rank(r["atr_pct_of_price"], all_atr)
        calmness_pct = (calm_hv_pct + calm_atr_pct) / 2
        oi_pct = _percentile_rank(r["atm_oi_total"], all_oi)
        spread_pct_rank = 100 - _percentile_rank(r["atm_spread_pct"], all_spread)
        liquidity_pct = (oi_pct + spread_pct_rank) / 2

        composite = (SCORE_WEIGHTS["iv_richness"] * iv_richness_pct +
                     SCORE_WEIGHTS["calmness"] * calmness_pct +
                     SCORE_WEIGHTS["liquidity"] * liquidity_pct)
        r["iv_richness_score"] = round(iv_richness_pct, 1)
        r["calmness_score"] = round(calmness_pct, 1)
        r["liquidity_score"] = round(liquidity_pct, 1)
        r["composite_score"] = round(composite, 1)
        if r["liquidity_ok"] and not r["fo_banned_today"] and r["atm_iv_pct"] is not None:
            eligible.append(r)

    eligible.sort(key=lambda r: r["composite_score"], reverse=True)
    for i, r in enumerate(eligible):
        r["rank"] = i + 1
        r["total"] = len(eligible)

    # Everything else (illiquid, banned, or IV/liquidity data unavailable) still gets returned
    # further down the list so nothing silently disappears, just clearly marked as excluded.
    excluded = [r for r in results if r not in eligible]
    for r in excluded:
        r["rank"] = None
        r["total"] = len(eligible)

    top = eligible[:limit]

    # --- Pass 4: headlines, ONLY for the final top-N being shown, to keep this fast ---
    if include_news:
        for r in top[:NEWS_FOR_TOP_N]:
            r["headlines"], r["headlines_error"] = _get_headlines_best_effort(r["symbol"])
    for r in top[:NEWS_FOR_TOP_N]:
        r["iv_trend"] = get_iv_trend_from_history(r["symbol"], iv_history)

    SCREENER_CACHE["results"] = eligible + excluded
    SCREENER_CACHE["fetched_at"] = now_ist()

    return jsonify({
        "count": len(results), "eligible_count": len(eligible), "stocks": top,
        "excluded_sample": excluded[:10],
        "ban_list_note": ban_error if ban_error else "F&O ban list fetched OK — banned symbols excluded above.",
        "note": ("Ranked by a composite score for OPTION-SELLING: IV richness (real IV Rank once "
                 "20+ days of history accumulate in iv_history.json, cross-sectional IV percentile "
                 "until then) 40%, calmness (inverse HV+ATR) 35%, ATM liquidity (OI + spread) 25%. "
                 f"Stocks are excluded from ranking if ATM combined OI < {MIN_ATM_TOTAL_OI} lots, "
                 f"ATM spread > {MAX_ATM_SPREAD_PCT}%, on today's F&O ban list, or IV couldn't be "
                 "computed. Headlines are a best-effort keyword scan, NOT sentiment analysis or "
                 "verified news — read the actual articles, and still check earnings/corporate "
                 "action dates yourself before trading."),
    })


def get_stock_rank(symbol):
    if not SCREENER_CACHE["results"]:
        return None
    for r in SCREENER_CACHE["results"]:
        if r["symbol"] == symbol:
            return {"rank": r["rank"], "total": r["total"],
                     "hv_annualized_pct": r["hv_annualized_pct"], "atr_pct_of_price": r["atr_pct_of_price"],
                     "atm_iv_pct": r.get("atm_iv_pct"), "iv_rank_pct": r.get("iv_rank_pct"),
                     "composite_score": r.get("composite_score"), "liquidity_ok": r.get("liquidity_ok"),
                     "fo_banned_today": r.get("fo_banned_today"),
                     "screener_age_minutes": round((now_ist() - SCREENER_CACHE["fetched_at"]).total_seconds() / 60, 1)}
    return None

@app.route("/api/ic-screener")
def ic_screener():
    """Fast, production-safe IC screener.

    Screen 1 remains the full F&O stock ranking. Screen 2 deliberately uses Screen 1's ranked
    universe as its shortlist and then batches ALL option instruments needed for that shortlist
    into Kite's bulk /quote endpoint. This preserves actual four-leg Zerodha tradability without
    turning one browser click into hundreds of sequential REST requests and a 504 timeout.
    """
    if not require_session():
        return jsonify({"error":"not_logged_in"}), 401

    limit=max(1,min(int(request.args.get("limit",25)),50))
    force=request.args.get("force","false").lower()=="true"
    include_news=request.args.get("news","false").lower()=="true"
    target_delta=float(request.args.get("target_delta",DEFAULT_TARGET_DELTA))
    min_dte=int(request.args.get("min_dte",IC_MIN_DTE))
    max_dte=int(request.args.get("max_dte",IC_MAX_DTE))
    max_symbols=max(10,min(int(request.args.get("max_symbols",30)),50))
    max_expiries=max(1,min(int(request.args.get("max_expiries",2)),3))

    if min_dte < 1 or max_dte < min_dte:
        return jsonify({"error":"Invalid DTE range"}),400

    nfo,nse=get_instruments(force=force)
    today=now_ist().date()
    opts_by_sym={}
    for o in nfo:
        if o.get("segment")=="NFO-OPT":
            opts_by_sym.setdefault(o.get("name"),[]).append(o)

    # Prefer Screen 1's already-computed ranking. This is both faster and economically cleaner:
    # Screen 1 answers which stocks deserve attention; Screen 2 answers which actual IC structure
    # among those stocks is best.
    cached=SCREENER_CACHE.get("results") or []
    ranked=[r for r in cached if r.get("rank") is not None and not r.get("fo_banned_today")]
    ranked.sort(key=lambda x:x.get("composite_score",0), reverse=True)

    if ranked:
        shortlist=ranked[:max_symbols]
        shortlist_source="Screen 1 ranked universe"
    else:
        # Safe fallback if the user has not run Screen 1 yet: use a small deterministic F&O
        # shortlist rather than starting another 199-stock historical/IV scan inside this request.
        universe=fo_stock_universe(force=force)
        token_map={i["tradingsymbol"]:i["instrument_token"] for i in nse if i.get("exchange")=="NSE"}
        shortlist=[]
        for sym in universe:
            if sym in token_map and opts_by_sym.get(sym):
                shortlist.append({"symbol":sym,"ltp":None,"hv_annualized_pct":None,"atr_pct_of_price":None,
                                  "atm_iv_pct":None,"iv_rank_pct":None,"composite_score":0,"rank":None,
                                  "fo_banned_today":False})
            if len(shortlist)>=max_symbols:
                break
        shortlist_source="fallback F&O shortlist — run Screen 1 first for ranked candidates"

    seed=[]
    for r in shortlist:
        if r.get("ltp"):
            seed.append({"symbol":r["symbol"],"ltp":r["ltp"]})
    seed_spot_cache_from_prices(seed)

    # Choose up to max_expiries per stock, prioritising the user's preferred 21–35 DTE window
    # and then the closest remaining expiries to the midpoint. This gives meaningful expiry
    # diversity while keeping the REST quote workload bounded.
    selected=[]
    option_keys=[]
    selection_meta={}
    for r in shortlist:
        sym=r["symbol"]
        spot_ref=float(r.get("ltp") or 0)
        opts=opts_by_sym.get(sym,[])
        exps=sorted({o["expiry"] for o in opts if min_dte <= (o["expiry"]-today).days <= max_dte})
        if not exps:
            continue
        preferred=[e for e in exps if IC_PREFERRED_DTE_LOW <= (e-today).days <= IC_PREFERRED_DTE_HIGH]
        preferred.sort(key=lambda e: abs((e-today).days-28))
        remaining=[e for e in exps if e not in preferred]
        remaining.sort(key=lambda e: abs((e-today).days-28))
        chosen=(preferred+remaining)[:max_expiries]
        for exp in chosen:
            subset=[o for o in opts if o["expiry"]==exp and o.get("strike",0)>0 and
                    (not spot_ref or abs(float(o["strike"])-spot_ref)/spot_ref <= IC_CHAIN_STRIKE_RANGE_PCT)]
            if not subset:
                continue
            key=(sym,exp)
            selected.append((r,exp,subset))
            selection_meta[key]=r
            option_keys.extend(f"NFO:{o['tradingsymbol']}" for o in subset)

    # One/bounded set of bulk requests for the whole scan, instead of one request per expiry.
    # Kite supports up to 500 instruments per /quote request; kite_quote_bulk enforces the
    # account-wide 1 req/sec limit and caches results.
    all_quotes=kite_quote_bulk(option_keys,chunk_size=500,retries=1)

    evaluated=[]
    evaluation_errors=[]
    best_by_symbol={}
    for r,exp,subset in selected:
        sym=r["symbol"]
        try:
            spot=float(r.get("ltp") or 0)
            data,err=fetch_chain_quotes_for_expiry(sym,exp,subset,spot_override=spot,quotes_override=all_quotes)
            if err:
                evaluation_errors.append({"symbol":sym,"expiry":str(exp),"error":err})
                continue
            if not data or len(data.get("chain",[]))<4:
                evaluation_errors.append({"symbol":sym,"expiry":str(exp),"error":"Insufficient quoted option legs"})
                continue
            ic=build_ic_candidate_from_chain(sym,data["spot"],exp,data["chain"],target_delta=target_delta,lots=1)
            if not ic:
                continue
            dte=(exp-today).days
            candidate={**ic,"expiry":str(exp),"dte":dte,"lot_size":data["lot_size"]}
            prev=best_by_symbol.get(sym)
            if prev is None or candidate["selection_score"]>prev["selection_score"]:
                best_by_symbol[sym]=(r,candidate)
        except Exception as e:
            evaluation_errors.append({"symbol":sym,"expiry":str(exp),"error":str(e)})
            logger.exception("IC screener failed for %s %s",sym,exp)

    for sym,(r,best) in best_by_symbol.items():
        try:
            sc,lc,sp,lp=[best[k] for k in ("sell_call","buy_call","sell_put","buy_put")]
            short_legs=[sc,sp]; hedge_legs=[lc,lp]; all_legs=[sc,lc,sp,lp]
            spreads=[x.get("spread_pct") for x in all_legs if x.get("spread_pct") is not None]
            short_spreads=[x.get("spread_pct") for x in short_legs if x.get("spread_pct") is not None]
            hedge_spreads=[x.get("spread_pct") for x in hedge_legs if x.get("spread_pct") is not None]
            short_oi=[x.get("oi",0) for x in short_legs]
            hedge_oi=[x.get("oi",0) for x in hedge_legs]
            short_vol=[x.get("volume",0) for x in short_legs]
            total_vol=sum(x.get("volume",0) for x in all_legs)
            short_liq_ok=(min(short_oi)>=IC_MIN_SHORT_OI and
                          (not short_spreads or max(short_spreads)<=IC_MAX_SHORT_SPREAD_PCT) and
                          min(short_vol)>=IC_MIN_SHORT_VOLUME)
            hedge_liq_ok=(min(hedge_oi)>=IC_MIN_LEG_OI and
                          (not hedge_spreads or max(hedge_spreads)<=IC_MAX_LEG_SPREAD_PCT))
            leg_liq_ok=short_liq_ok and hedge_liq_ok and total_vol>=IC_MIN_TOTAL_VOLUME

            trend=get_trend_regime(sym)
            trend_ok=not trend.get("error")
            if trend_ok:
                reg=trend.get("regime","")
                range_score=95 if reg=="Range Bound" else 75 if reg in ("Transitioning","Volatile / Mixed") else 30
                if trend.get("avoid_premium_selling"): range_score=15
            else: range_score=50

            iv_score=(r.get("iv_rank_pct") if r.get("iv_rank_pct") is not None else r.get("composite_score",50))
            ivhv=(r.get("iv_hv") or {}).get("ratio")
            if ivhv is not None:
                iv_score=0.55*iv_score+0.45*min(100,max(0,(ivhv-0.8)/0.8*100))
            cushion=min(best["ce_cushion"],best["pe_cushion"])
            cushion_score=score_band(cushion,[(0.75,20),(0.90,35),(1.00,50),(1.15,70),(1.30,85),(1.50,95),(999,100)])
            # Delta symmetry is a first-class IC criterion.  This is what prevents a
            # richer 0.25-delta call from being presented as an ordinary 0.18-delta IC.
            short_delta_gap=abs(abs(sc.get("delta",0))-abs(sp.get("delta",0)))
            delta_score=float(best.get("delta_symmetry_score", max(0,100-(short_delta_gap/0.04)*100)))
            credit_ratio=best["credit"]/best["max_loss"] if best["max_loss"] else 0
            econ_score=score_band(credit_ratio,[(0.08,20),(0.10,30),(0.15,50),(0.20,65),(0.25,78),(0.35,92),(0.50,100),(999,100)])
            def spread_score(vals,scale):
                return sum(min(100,max(0,100-(v/scale)*100)) for v in vals)/len(vals) if vals else 60
            short_spread_score=spread_score(short_spreads,IC_MAX_SHORT_SPREAD_PCT)
            hedge_spread_score=spread_score(hedge_spreads,IC_MAX_LEG_SPREAD_PCT)
            short_oi_score=sum(min(100,math.log10(max(x,1))/4*100) for x in short_oi)/len(short_oi)
            hedge_oi_score=sum(min(100,math.log10(max(x,1))/4*100) for x in hedge_oi)/len(hedge_oi)
            liquidity_score=0.55*(0.65*short_spread_score+0.35*short_oi_score)+0.45*(0.65*hedge_spread_score+0.35*hedge_oi_score)
            if not short_liq_ok: liquidity_score=min(liquidity_score,45)
            elif not hedge_liq_ok: liquidity_score=min(liquidity_score,65)
            event_score,flags=ic_event_risk(sym,best["expiry"])
            final=(IC_SCORE_WEIGHTS["iv"]*iv_score+IC_SCORE_WEIGHTS["range"]*range_score+
                   IC_SCORE_WEIGHTS["cushion"]*cushion_score+IC_SCORE_WEIGHTS["liquidity"]*liquidity_score+
                   IC_SCORE_WEIGHTS["economics"]*econ_score+IC_SCORE_WEIGHTS["delta"]*delta_score+
                   IC_SCORE_WEIGHTS["event"]*event_score)
            hard_reasons=[]
            if not short_liq_ok: hard_reasons.append("short-leg liquidity failed")
            if not hedge_liq_ok: hard_reasons.append("hedge-leg liquidity weak")
            if total_vol<IC_MIN_TOTAL_VOLUME: hard_reasons.append("very low four-leg volume")
            if credit_ratio<0.08: hard_reasons.append("very low credit/max-loss")
            if cushion<0.75: hard_reasons.append("short strike inside 0.75x expected move")
            if short_delta_gap>0.04: hard_reasons.append("short-call/put delta asymmetry exceeds 0.04")
            if best.get("delta_target_error",0)>0.03: hard_reasons.append("short legs are too far from requested target delta")
            if best["credit"]<=0 or best["max_loss"]<=0: hard_reasons.append("invalid risk/reward")
            out=dict(r)
            out.update({"ic_score":round(final,1),"ic_label":"Excellent" if final>=80 else "Good" if final>=65 else "Average" if final>=50 else "Watch",
                        "ic_expiry":best["expiry"],"ic_dte":best["dte"],"ic_credit_per_share":round(best["credit"],2),
                        "ic_max_loss_per_share":round(best["max_loss"],2),"ic_credit_max_loss_pct":round(credit_ratio*100,1),
                        "ic_pop_pct":best["probability_of_profit"],"ic_ce_cushion_em":round(best["ce_cushion"],2),
                        "ic_pe_cushion_em":round(best["pe_cushion"],2),
                        "ic_sell_call_delta":round(float(sc.get("delta",0)),3),"ic_sell_put_delta":round(float(sp.get("delta",0)),3),
                        "ic_buy_call_delta":round(float(lc.get("delta",0)),3),"ic_buy_put_delta":round(float(lp.get("delta",0)),3),
                        "ic_delta_gap":round(short_delta_gap,3),"ic_delta_score":round(delta_score,1),
                        "ic_target_delta":round(target_delta,3),
                        "ic_risk_adjusted_economics_score":round(best.get("risk_adjusted_economics_score",econ_score),1),
                        "ic_liquidity_score":round(liquidity_score,1),
                        "ic_min_leg_oi":min(x.get("oi",0) for x in all_legs),"ic_min_short_oi":min(short_oi),
                        "ic_min_hedge_oi":min(hedge_oi),"ic_total_leg_volume":total_vol,
                        "ic_max_leg_spread_pct":round(max(spreads),2) if spreads else None,
                        "ic_max_short_spread_pct":round(max(short_spreads),2) if short_spreads else None,
                        "ic_max_hedge_spread_pct":round(max(hedge_spreads),2) if hedge_spreads else None,
                        "ic_short_liquidity_ok":short_liq_ok,"ic_hedge_liquidity_ok":hedge_liq_ok,
                        "trend_regime":trend.get("regime") if trend_ok else None,"trend_adx":trend.get("adx14") if trend_ok else None,
                        "event_score":event_score,"event_flags":flags,"hard_reasons":hard_reasons,
                        "ic_legs":{"sell_call":best["sell_call"]["strike"],"buy_call":best["buy_call"]["strike"],
                                   "sell_put":best["sell_put"]["strike"],"buy_put":best["buy_put"]["strike"]}})
            evaluated.append(out)
        except Exception as e:
            evaluation_errors.append({"symbol":sym,"error":str(e)})
            logger.exception("IC scoring failed for %s",sym)

    eligible=[r for r in evaluated if not r["hard_reasons"]]
    eligible.sort(key=lambda x:x["ic_score"],reverse=True)
    for i,r in enumerate(eligible,1):
        r["rank"]=i; r["total"]=len(eligible)
    excluded=[r for r in evaluated if r not in eligible]
    for r in excluded:
        r["rank"]=None; r["total"]=len(eligible)
    top=eligible[:limit]
    if include_news:
        for r in top[:NEWS_FOR_TOP_N]:
            r["headlines"],r["headlines_error"]=_get_headlines_best_effort(r["symbol"])
            r["event_score"],r["event_flags"]=ic_event_risk(r["symbol"],r["ic_expiry"],r.get("headlines"))
    for r in top:
        r["iv_trend"]=get_iv_trend_from_history(r["symbol"],load_iv_history())

    IC_SCREENER_CACHE["results"]=eligible+excluded
    IC_SCREENER_CACHE["fetched_at"]=now_ist()
    IC_SCREENER_CACHE["errors"]=evaluation_errors
    return jsonify({"count":len(shortlist),"eligible_count":len(eligible),"stocks":top,
                    "excluded_sample":excluded[:10],"deep_evaluated":len(shortlist),
                    "evaluation_error_count":len(evaluation_errors),"evaluation_errors_sample":evaluation_errors[:10],
                    "shortlist_source":shortlist_source,"option_quote_instruments":len(set(option_keys)),
                    "config":{"target_delta":target_delta,"min_dte":min_dte,"max_dte":max_dte,
                              "preferred_dte":[IC_PREFERRED_DTE_LOW,IC_PREFERRED_DTE_HIGH],"max_symbols":max_symbols,
                              "max_expiries_per_symbol":max_expiries,"weights":IC_SCORE_WEIGHTS,
                              "delta_tolerance":0.03,"max_short_delta_gap":0.04,
                              "short_leg_min_oi":IC_MIN_SHORT_OI,"short_leg_max_spread_pct":IC_MAX_SHORT_SPREAD_PCT,
                              "hedge_min_oi":IC_MIN_LEG_OI,"hedge_max_spread_pct":IC_MAX_LEG_SPREAD_PCT},
                    "note":"Screen 1 ranks the F&O universe; this screen uses the top ranked stocks, then batches the required option-chain instruments into Kite quote requests. It evaluates up to two preferred expiries per stock by default, prioritising 21–35 DTE, to remain within Zerodha REST limits and avoid browser 504 timeouts. The result is an actual four-leg Zerodha-tradable IC heuristic. Normal mode keeps both short legs within +/-0.03 of the requested delta and rejects >0.04 CE/PE delta asymmetry; expected-move cushion risk-adjusts the premium/economics score. It is not a backtest or guarantee."})


@app.route("/api/screener-health")
def screener_health():
    if not require_session():
        return jsonify({"error":"not_logged_in"}), 401
    now = time.monotonic()
    return jsonify({"stock_screener_cache":bool(SCREENER_CACHE.get("results")),
                    "ic_screener_cache":bool(IC_SCREENER_CACHE.get("results")),
                    "ic_error_count":len(IC_SCREENER_CACHE.get("errors",[])),
                    "quote_cache_size":len(_QUOTE_CACHE),
                    "quote_cooldown_seconds":round(max(0.0, _QUOTE_COOLDOWN_UNTIL-now),2),
                    "quote_min_interval":_QUOTE_MIN_INTERVAL,
                    "quote_cache_ttl":_QUOTE_CACHE_TTL})


# ---------------------------------------------------------------------------
# Expiry list + option chain (supports stocks AND indices, any expiry you pick)
# ---------------------------------------------------------------------------
def get_spot_price(symbol):
    """Returns (spot_price, error_dict_or_None). Uses the shared quote cache first and
    only falls back to a rate-limited single Quote call when no cached value exists."""
    symbol = symbol.upper()
    key = INDEX_SYMBOLS.get(symbol) if symbol in INDEX_SYMBOLS else f"NSE:{symbol}"
    cached = _quote_cache_get([key]).get(key)
    if cached and cached.get("last_price") is not None:
        return cached["last_price"], None
    try:
        quote = kite_quote_bulk([key], chunk_size=500, retries=1).get(key)
        if quote and quote.get("last_price") is not None:
            return quote["last_price"], None
        return None, {"error": f"No live quote returned for {symbol}"}
    except Exception as e:
        return None, {"error": f"Could not fetch {symbol} quote: {e}"}


@app.route("/api/expiries/<symbol>")
def expiries(symbol):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    symbol = symbol.upper()
    exchange, opts = get_option_instruments_for_symbol(symbol)
    if not opts:
        return jsonify({"error": f"No options found for {symbol}"}), 404
    today = now_ist().date()
    exp_list = sorted({o["expiry"] for o in opts if (o["expiry"] - today).days >= 1})
    return jsonify({"symbol": symbol, "expiries": [str(e) for e in exp_list]})


def get_chain_for_symbol(symbol, expiry_str=None):
    """Returns (data_dict, None) or (None, error_dict). Supports NFO indices and BFO SENSEX."""
    symbol = symbol.upper()
    spot, err = get_spot_price(symbol)
    if err:
        return None, err

    exchange, opts = get_option_instruments_for_symbol(symbol)
    if not opts:
        return None, {"error": f"No options found for {symbol}"}

    today = now_ist().date()
    all_expiries = sorted({o["expiry"] for o in opts})

    if expiry_str:
        try:
            target_expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        except ValueError:
            return None, {"error": f"Invalid expiry format '{expiry_str}', expected YYYY-MM-DD"}
        if target_expiry not in all_expiries:
            return None, {"error": f"{expiry_str} is not a valid expiry for {symbol}. "
                                    f"Available: {', '.join(str(e) for e in all_expiries[:6])}"}
        expiry = target_expiry
    else:
        valid = [e for e in all_expiries if (e - today).days >= MIN_DAYS_TO_EXPIRY]
        if not valid:
            return None, {"error": "No expiry beyond minimum days-to-expiry filter"}
        expiry = valid[0]

    T = max((expiry - today).days, 0) / 365.0
    chain = [o for o in opts if o["expiry"] == expiry]
    lot_size = chain[0]["lot_size"]
    inst_keys = [f"{exchange}:{o['tradingsymbol']}" for o in chain]
    quotes = kite_quote_bulk(inst_keys)

    enriched = []
    for o in chain:
        key = f"{exchange}:{o['tradingsymbol']}"
        q = quotes.get(key)
        st = quote_stats(q)
        ltp = st["mid"] if st["mid"] is not None else extract_price(q)
        if ltp is None:
            continue
        iv = implied_vol(ltp, spot, o["strike"], T, o["instrument_type"])
        delta = bs_delta(spot, o["strike"], T, RISK_FREE_RATE, iv, o["instrument_type"])
        enriched.append({**o, "ltp": ltp, "bid": st["bid"], "ask": st["ask"], "mid": st["mid"],
                         "spread_pct": round(st["spread_pct"],2) if st["spread_pct"] is not None else None,
                         "volume": st["volume"], "oi": st["oi"], "iv": round(iv * 100, 1), "delta": round(delta, 3)})

    return {"symbol": symbol, "exchange": exchange, "spot": spot, "expiry": expiry, "T": T, "lot_size": lot_size, "chain": enriched,
            "all_expiries": [str(e) for e in all_expiries]}, None


def compute_pcr_and_max_pain(chain):
    """Put/Call Ratio (by OI) and Max Pain strike, computed across the FULL fetched chain
    (not just the strikes shown in the UI's +/-25% window) so both are based on complete OI."""
    calls = [o for o in chain if o["instrument_type"] == "CE"]
    puts = [o for o in chain if o["instrument_type"] == "PE"]
    total_call_oi = sum(o["oi"] for o in calls)
    total_put_oi = sum(o["oi"] for o in puts)
    pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi else None

    strikes = sorted({o["strike"] for o in chain})
    max_pain_strike, min_pain = None, None
    for k in strikes:
        pain = 0.0
        for o in calls:
            if k > o["strike"]:
                pain += (k - o["strike"]) * o["oi"]
        for o in puts:
            if k < o["strike"]:
                pain += (o["strike"] - k) * o["oi"]
        if min_pain is None or pain < min_pain:
            min_pain, max_pain_strike = pain, k

    return {"pcr": pcr, "total_call_oi": int(total_call_oi), "total_put_oi": int(total_put_oi),
            "max_pain_strike": max_pain_strike,
            "note": "PCR > 1 is traditionally read as bullish/support-building, < 1 as bearish — "
                    "a rough sentiment gauge, not a price target. Max Pain is the strike where option "
                    "writers' aggregate payout is lowest at expiry; a commonly-watched but unreliable-alone "
                    "expiry-pinning heuristic."}


@app.route("/api/optionchain/<symbol>")
def option_chain(symbol):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    expiry_str = request.args.get("expiry")
    data, err = get_chain_for_symbol(symbol, expiry_str)
    if err:
        return jsonify(err), 404

    oi_summary = compute_pcr_and_max_pain(data["chain"])
    spot = data["spot"]
    lo = spot * (1 - CHAIN_STRIKE_RANGE_PCT)
    hi = spot * (1 + CHAIN_STRIKE_RANGE_PCT)
    filtered = [o for o in data["chain"] if lo <= o["strike"] <= hi]
    calls = sorted([o for o in filtered if o["instrument_type"] == "CE"], key=lambda x: x["strike"])
    puts = sorted([o for o in filtered if o["instrument_type"] == "PE"], key=lambda x: x["strike"])

    def slim(o):
        return {"strike": o["strike"], "ltp": o["ltp"], "oi": o["oi"], "iv_pct": o["iv"], "delta": o["delta"]}

    return jsonify({
        "symbol": symbol.upper(), "spot": spot, "expiry": str(data["expiry"]), "lot_size": data["lot_size"],
        "all_expiries": data["all_expiries"], "oi_summary": oi_summary,
        "calls": [slim(o) for o in calls], "puts": [slim(o) for o in puts]
    })


# ---------------------------------------------------------------------------
# Strategy builder — Iron Condor or Naked Strangle, adjustable delta/wing/lots/expiry
# ---------------------------------------------------------------------------
def build_strategy(symbol, target_delta=DEFAULT_TARGET_DELTA, wing_width_pct=DEFAULT_WING_WIDTH_PCT,
                    strategy_type="iron_condor", expiry_str=None, lots=1, put_delta=None, call_delta=None,
                    wing_mode="auto"):
    symbol=symbol.upper(); data,err=get_chain_for_symbol(symbol,expiry_str)
    if err: return err
    spot,expiry,T,lot_size,chain=data["spot"],data["expiry"],data["T"],data["lot_size"],data["chain"]
    today=now_ist().date(); quantity=lot_size*max(1,int(lots))
    calls=sorted([e for e in chain if e["instrument_type"]=="CE"],key=lambda x:x["strike"])
    puts=sorted([e for e in chain if e["instrument_type"]=="PE"],key=lambda x:x["strike"])
    def closest(options,target,sign): return min(options,key=lambda o:abs(o["delta"]-sign*target)) if options else None
    cd=float(call_delta if call_delta is not None else target_delta); pd=float(put_delta if put_delta is not None else target_delta)
    short_call=closest(calls,cd,1); short_put=closest(puts,pd,-1)
    if not short_call or not short_put: return {"error":"Could not find suitable short strikes"}
    def leg(o): return {"strike":o["strike"],"ltp":o["ltp"],"mid":o.get("mid",o["ltp"]),"bid":o.get("bid"),"ask":o.get("ask"),"delta":o["delta"],"iv":o.get("iv"),"oi":o.get("oi"),"volume":o.get("volume"),"spread_pct":o.get("spread_pct"),"tradingsymbol":o["tradingsymbol"]}
    if strategy_type=="naked_strangle":
        net_credit=short_call["mid"]+short_put["mid"]
        result={"symbol":symbol,"spot":spot,"expiry":str(expiry),"days_to_expiry":(expiry-today).days,"lot_size":lot_size,"lots":lots,"quantity":quantity,
                "strategy_type":"naked_strangle","legs":{"sell_call":leg(short_call),"sell_put":leg(short_put)},"net_credit_per_share":round(net_credit,2),
                "max_profit":round(net_credit*quantity,2),"max_loss":None,"breakeven_upper":round(short_call["strike"]+net_credit,2),"breakeven_lower":round(short_put["strike"]-net_credit,2)}
    else:
        # Use the actual quoted chain and optimise wing width unless the user explicitly selected a fixed percentage.
        width=float(wing_width_pct) if wing_mode=="fixed" else None
        ic=build_ic_candidate_from_chain(symbol,spot,expiry,chain,target_delta=target_delta,wing_width_pct=width,lots=lots,call_delta=cd,put_delta=pd)
        if not ic: return {"error":"Could not construct a positive-credit, liquid Iron Condor from the current chain"}
        # Respect independently requested call/put deltas when supplied by selecting nearest valid strikes, then re-optimise wings around them.
        sc,lc,sp,lp=ic["sell_call"],ic["buy_call"],ic["sell_put"],ic["buy_put"]
        net_credit=ic["credit"]; max_loss=ic["max_loss"]
        result={"symbol":symbol,"spot":spot,"expiry":str(expiry),"days_to_expiry":(expiry-today).days,"lot_size":lot_size,"lots":lots,"quantity":quantity,
                "strategy_type":"iron_condor","legs":{"sell_call":leg(sc),"buy_call":leg(lc),"sell_put":leg(sp),"buy_put":leg(lp)},
                "net_credit_per_share":round(net_credit,2),"max_profit":round(net_credit*quantity,2),"max_loss":round(max_loss*quantity,2),
                "breakeven_upper":round(sc["strike"]+net_credit,2),"breakeven_lower":round(sp["strike"]-net_credit,2),
                "call_wing":round(ic["call_wing"],2),"put_wing":round(ic["put_wing"],2),"wing_width_pct_used":wing_width_pct if width else None,
                "ic_selection_score":round(ic["selection_score"],1)}
    result["target_delta_used"]=target_delta; result["call_delta_used"]=cd; result["put_delta_used"]=pd; result["wing_mode"]=wing_mode
    result["rank_info"]=get_stock_rank(symbol); result["all_expiries"]=data["all_expiries"]
    legs_for_margin=[{"tradingsymbol":lg["tradingsymbol"],"transaction_type":"SELL" if k.startswith("sell") else "BUY"} for k,lg in result["legs"].items()]
    result["margin_required"],result["margin_error"]=compute_margin(legs_for_margin,quantity)
    result["entry_event_warning"]=get_entry_warning(); result["event_before_expiry"]=get_event_before_expiry(result["expiry"])
    entry=[{"price":lg.get("mid",lg["ltp"]),"quantity":quantity,"transaction_type":"SELL" if k.startswith("sell") else "BUY"} for k,lg in result["legs"].items()]
    result["estimated_entry_charges"]=estimate_charges(entry); result["net_profit_after_entry_charges"]=round(result["max_profit"]-result["estimated_entry_charges"]["total"],2) if result.get("max_profit") is not None else None
    rank=result.get("rank_info") or {}; iv_hv=classify_iv_hv(rank.get("atm_iv_pct"),rank.get("hv_annualized_pct")); result["iv_hv"]=iv_hv
    em=expected_move(spot,rank.get("atm_iv_pct"),result["days_to_expiry"]); result["expected_move"]=em
    if em and strategy_type=="iron_condor":
        result["ce_cushion_em"]=round((result["legs"]["sell_call"]["strike"]-spot)/em["expected_move"],2)
        result["pe_cushion_em"]=round((spot-result["legs"]["sell_put"]["strike"])/em["expected_move"],2)
        result["probability_of_profit_pct"]=model_expiry_probability_between(spot,result["breakeven_lower"],result["breakeven_upper"],rank.get("atm_iv_pct"),result["days_to_expiry"])
    else:
        result["probability_of_profit_pct"]=round(max(0,(1-abs(cd)-abs(pd))*100),1)
    for k in ("sell_call","sell_put"):
        if k in result["legs"]: result["legs"][k]["probability_of_touch_pct"]=probability_of_touch(result["legs"][k]["delta"])
    trend=get_trend_regime(symbol); result["trend"]=None if trend.get("error") else trend
    vix,vix_err=get_india_vix(); result["volatility_regime"]=classify_volatility_regime(vix,rank.get("iv_rank_pct"))
    if strategy_type=="iron_condor":
        liq=min([result["legs"][k].get("oi",0) for k in result["legs"]]) if result.get("legs") else 0
        spreads=[result["legs"][k].get("spread_pct") for k in result["legs"] if result["legs"][k].get("spread_pct") is not None]
        result["ic_liquidity_ok"]=liq>=IC_MIN_LEG_OI and (not spreads or max(spreads)<=IC_MAX_LEG_SPREAD_PCT)
        result["ic_credit_max_loss_pct"]=round(result["net_credit_per_share"]/max(result["max_loss"]/quantity,1e-9)*100,1)
        result["ic_event_score"],result["ic_event_flags"]=ic_event_risk(symbol,result["expiry"])
        result["trade_quality_score"]=round((0.25*(rank.get("ic_score") or 50)+0.25*(iv_hv and min(100,max(0,(iv_hv["ratio"]-0.8)/0.8*100)) or 50)+
                                             0.25*(min(result.get("ce_cushion_em",0),result.get("pe_cushion_em",0))/1.5*100)+0.25*(80 if result["ic_liquidity_ok"] else 30)),1)
    else: result["trade_quality_score"]=rank.get("composite_score")
    result["trade_quality_label"]="Excellent" if result["trade_quality_score"]>=80 else "Good" if result["trade_quality_score"]>=60 else "Average" if result["trade_quality_score"]>=40 else "Avoid"
    result["suggested_strategy"]=suggest_strategy_family(rank.get("iv_rank_pct"),trend)
    result["trade_quality_note"]="IC score is a transparent heuristic combining volatility richness, range behaviour, expected-move cushion, four-leg liquidity and trade economics. It is not backtested and is not a probability guarantee."
    return result


@app.route("/api/strategy/<symbol>")
def strategy(symbol):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    target_delta = float(request.args.get("target_delta", DEFAULT_TARGET_DELTA))
    wing_width_pct = float(request.args.get("wing_width_pct", DEFAULT_WING_WIDTH_PCT))
    strategy_type = request.args.get("strategy_type", "iron_condor")
    expiry_str = request.args.get("expiry")
    lots = int(request.args.get("lots", 1))
    put_delta = request.args.get("put_delta")
    call_delta = request.args.get("call_delta")
    wing_mode = request.args.get("wing_mode", "auto")
    result = build_strategy(symbol, target_delta, wing_width_pct, strategy_type, expiry_str, lots,
                            put_delta=float(put_delta) if put_delta not in (None, "") else None,
                            call_delta=float(call_delta) if call_delta not in (None, "") else None,
                            wing_mode=wing_mode)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


# ---------------------------------------------------------------------------
# Strategy builder — Double Calendar Spread (sell near-term call+put, buy far-term
# call+put at the same two strikes). Net debit, defined risk, long vega / positive theta.
# ---------------------------------------------------------------------------
def pick_calendar_expiries(all_expiries_str, near_expiry_str_override=None, far_expiry_str_override=None):
    """Auto-picks a sensible near/far expiry pair from the full expiry list (strings 'YYYY-MM-DD').
    Near = nearest expiry beyond MIN_DAYS_TO_EXPIRY (same rule as the other strategy builders).
    Far = the available expiry whose gap from Near is closest to CALENDAR_TARGET_GAP_DAYS (and
    strictly after Near) — this is what "automatically pick which expiries" means in practice:
    typically the current/next-week expiry paired with the next monthly one or two out.
    Returns (near_str, far_str) or (None, None, error_dict)."""
    today = now_ist().date()
    all_expiries = sorted(datetime.strptime(e, "%Y-%m-%d").date() for e in all_expiries_str)

    if near_expiry_str_override:
        try:
            near = datetime.strptime(near_expiry_str_override, "%Y-%m-%d").date()
        except ValueError:
            return None, None, {"error": f"Invalid near_expiry format '{near_expiry_str_override}'"}
        if near not in all_expiries:
            return None, None, {"error": f"{near_expiry_str_override} is not a valid expiry for this symbol"}
    else:
        valid = [e for e in all_expiries if (e - today).days >= MIN_DAYS_TO_EXPIRY]
        if not valid:
            return None, None, {"error": "No near expiry beyond minimum days-to-expiry filter"}
        near = valid[0]

    later = [e for e in all_expiries if e > near]
    if not later:
        return None, None, {"error": f"No later expiry available beyond near expiry {near} to use as the far leg"}

    if far_expiry_str_override:
        try:
            far = datetime.strptime(far_expiry_str_override, "%Y-%m-%d").date()
        except ValueError:
            return None, None, {"error": f"Invalid far_expiry format '{far_expiry_str_override}'"}
        if far not in later:
            return None, None, {"error": f"{far_expiry_str_override} must be a valid expiry strictly after {near}"}
    else:
        far = min(later, key=lambda e: abs((e - near).days - CALENDAR_TARGET_GAP_DAYS))

    return str(near), str(far), None


def build_double_calendar_strategy(symbol, strike_mode="otm_pct", otm_pct=DEFAULT_CALENDAR_OTM_PCT,
                                    target_delta=DEFAULT_CALENDAR_TARGET_DELTA,
                                    near_expiry_str=None, far_expiry_str=None, lots=1):
    symbol = symbol.upper()
    today = now_ist().date()

    # Resolve which two expiries to use (auto-picked unless the user overrode one/both).
    nfo, _ = get_instruments()
    opts = [i for i in nfo if i["name"] == symbol and i["segment"] == "NFO-OPT"]
    if not opts:
        return {"error": f"No options found for {symbol}"}
    all_expiries_str = [str(e) for e in sorted({o["expiry"] for o in opts})]
    near_expiry_str, far_expiry_str, err = pick_calendar_expiries(all_expiries_str, near_expiry_str, far_expiry_str)
    if err:
        return err

    near_data, err = get_chain_for_symbol(symbol, near_expiry_str)
    if err:
        return err
    far_data, err = get_chain_for_symbol(symbol, far_expiry_str)
    if err:
        return err

    spot = near_data["spot"]
    lot_size = near_data["lot_size"]
    quantity = lot_size * max(1, int(lots))
    near_expiry = near_data["expiry"]
    far_expiry = far_data["expiry"]
    days_to_near = (near_expiry - today).days
    days_between = (far_expiry - near_expiry).days
    if days_between <= 0:
        return {"error": "Far expiry must be strictly after near expiry"}

    near_calls = sorted([o for o in near_data["chain"] if o["instrument_type"] == "CE"], key=lambda x: x["strike"])
    near_puts = sorted([o for o in near_data["chain"] if o["instrument_type"] == "PE"], key=lambda x: x["strike"])
    far_calls = sorted([o for o in far_data["chain"] if o["instrument_type"] == "CE"], key=lambda x: x["strike"])
    far_puts = sorted([o for o in far_data["chain"] if o["instrument_type"] == "PE"], key=lambda x: x["strike"])
    if not near_calls or not near_puts or not far_calls or not far_puts:
        return {"error": "Could not load a complete call/put chain for both expiries"}

    def closest_strike(options, target):
        return min(options, key=lambda o: abs(o["strike"] - target))

    def closest_by_delta(options, target, sign):
        return min(options, key=lambda o: abs(o["delta"] - sign * target))

    if strike_mode == "atm":
        call_strike_target = put_strike_target = spot
        near_call = closest_strike(near_calls, call_strike_target)
        near_put = closest_strike(near_puts, put_strike_target)
    elif strike_mode == "delta":
        near_call = closest_by_delta(near_calls, target_delta, +1)
        near_put = closest_by_delta(near_puts, target_delta, -1)
    else:  # "otm_pct" (default)
        near_call = closest_strike(near_calls, spot * (1 + otm_pct))
        near_put = closest_strike(near_puts, spot * (1 - otm_pct))

    # Match the SAME strikes on the far expiry (nearest available if strikes differ slightly).
    far_call = closest_strike(far_calls, near_call["strike"])
    far_put = closest_strike(far_puts, near_put["strike"])

    def leg(o):
        return {"strike": o["strike"], "ltp": o["ltp"], "delta": o["delta"], "iv_pct": o["iv"],
                "tradingsymbol": o["tradingsymbol"]}

    legs = {"sell_call_near": leg(near_call), "sell_put_near": leg(near_put),
            "buy_call_far": leg(far_call), "buy_put_far": leg(far_put)}

    net_debit_per_share = ((far_call["ltp"] + far_put["ltp"]) - (near_call["ltp"] + near_put["ltp"]))
    max_loss_per_share = max(net_debit_per_share, 0.0)  # defined risk: worst case both near legs expire
    # worthless and you simply own the far legs, having overpaid the debit — you lose at most the debit.

    # --- Model-based P&L curve at NEAR expiry, across a range of assumed spot outcomes ---
    # At near expiry: the short near-leg is worth its intrinsic value (you owe that to close it);
    # the long far-leg still has (far_expiry - near_expiry) days left, valued via Black-Scholes at
    # today's implied vol for that leg (assumes IV holds roughly steady — the standard simplifying
    # assumption for calendar-spread payoff diagrams; real IV can/does change).
    T_far_remaining = days_between / 365.0
    call_iv = far_call["iv"] / 100.0
    put_iv = far_put["iv"] / 100.0
    call_strike, put_strike = near_call["strike"], near_put["strike"]

    def pnl_at_spot(s_t):
        near_call_intrinsic = max(s_t - call_strike, 0.0)
        near_put_intrinsic = max(put_strike - s_t, 0.0)
        far_call_value = bs_price(s_t, call_strike, T_far_remaining, RISK_FREE_RATE, call_iv, "CE")
        far_put_value = bs_price(s_t, put_strike, T_far_remaining, RISK_FREE_RATE, put_iv, "PE")
        position_value = (far_call_value - near_call_intrinsic) + (far_put_value - near_put_intrinsic)
        return position_value - net_debit_per_share

    lo = spot * (1 - CALENDAR_CURVE_RANGE_PCT)
    hi = spot * (1 + CALENDAR_CURVE_RANGE_PCT)
    step = (hi - lo) / (CALENDAR_CURVE_POINTS - 1)
    curve = []
    for i in range(CALENDAR_CURVE_POINTS):
        s_t = lo + i * step
        pnl_per_share = pnl_at_spot(s_t)
        curve.append({"spot": round(s_t, 2), "pnl": round(pnl_per_share * quantity, 2)})

    max_profit_point = max(curve, key=lambda pt: pt["pnl"])
    max_profit_estimated = max_profit_point["pnl"]

    # Breakevens: spot values where the curve crosses zero (linear interpolation between samples).
    breakevens = []
    for i in range(len(curve) - 1):
        p1, p2 = curve[i], curve[i + 1]
        if (p1["pnl"] <= 0 <= p2["pnl"]) or (p1["pnl"] >= 0 >= p2["pnl"]):
            if p2["pnl"] != p1["pnl"]:
                frac = -p1["pnl"] / (p2["pnl"] - p1["pnl"])
                be_spot = p1["spot"] + frac * (p2["spot"] - p1["spot"])
                breakevens.append(round(be_spot, 2))
    # de-dupe near-identical crossings
    dedup_breakevens = []
    for b in breakevens:
        if not any(abs(b - x) < 0.5 for x in dedup_breakevens):
            dedup_breakevens.append(b)

    result = {
        "symbol": symbol, "spot": spot, "lot_size": lot_size, "lots": lots, "quantity": quantity,
        "strategy_type": "double_calendar", "strike_mode": strike_mode,
        "near_expiry": str(near_expiry), "far_expiry": str(far_expiry),
        "days_to_near_expiry": days_to_near, "days_between_expiries": days_between,
        "all_expiries": all_expiries_str,
        "legs": legs,
        "net_debit_per_share": round(net_debit_per_share, 2),
        "max_loss": round(max_loss_per_share * quantity, 2),
        "max_profit_estimated": round(max_profit_estimated, 2),
        "breakevens": dedup_breakevens,
        "sweet_spot_range": [near_put["strike"], near_call["strike"]],
        "curve": curve,
        "note": ("DOUBLE CALENDAR SPREAD: net-debit, defined-risk trade. Max loss is capped at the debit "
                 "paid; max profit is a MODEL ESTIMATE (Black-Scholes value of the far leg at near expiry, "
                 "assuming today's IV holds) — not guaranteed, since realized IV and the exact time of exit "
                 "both move the actual P&L. Profit is maximized if spot sits between the two short strikes "
                 "at near expiry; sharp moves in either direction erode it. Educational calculation only — "
                 "not a trade recommendation. Verify prices, margin, and lot size on your broker terminal.")
    }

    legs_for_margin = [
        {"tradingsymbol": legs["sell_call_near"]["tradingsymbol"], "transaction_type": "SELL"},
        {"tradingsymbol": legs["sell_put_near"]["tradingsymbol"], "transaction_type": "SELL"},
        {"tradingsymbol": legs["buy_call_far"]["tradingsymbol"], "transaction_type": "BUY"},
        {"tradingsymbol": legs["buy_put_far"]["tradingsymbol"], "transaction_type": "BUY"},
    ]
    margin_required, margin_error = compute_margin(legs_for_margin, quantity)
    result["margin_required"] = margin_required
    result["margin_error"] = margin_error
    result["entry_event_warning"] = get_entry_warning()
    result["event_before_expiry"] = get_event_before_expiry(result["near_expiry"])

    entry_orders_for_charges = [
        {"price": legs["sell_call_near"]["ltp"], "quantity": quantity, "transaction_type": "SELL"},
        {"price": legs["sell_put_near"]["ltp"], "quantity": quantity, "transaction_type": "SELL"},
        {"price": legs["buy_call_far"]["ltp"], "quantity": quantity, "transaction_type": "BUY"},
        {"price": legs["buy_put_far"]["ltp"], "quantity": quantity, "transaction_type": "BUY"},
    ]
    entry_charges = estimate_charges(entry_orders_for_charges)
    result["estimated_entry_charges"] = entry_charges
    result["charges_note"] = ("Entry-side charges only. If you square off before near expiry, exit-side "
                               "charges apply too — see the Track Positions section for the running "
                               "round-trip estimate once tracked.")

    # --- Reuse the same trading-logic layer as the Iron Condor/Strangle builder ---
    rank_info = get_stock_rank(symbol)
    result["rank_info"] = rank_info
    iv_hv = classify_iv_hv(rank_info.get("atm_iv_pct") if rank_info else None,
                            rank_info.get("hv_annualized_pct") if rank_info else None)
    result["iv_hv"] = iv_hv
    if iv_hv is None:
        result["iv_hv_note"] = "Run the Screener (section 1) first so IV/HV data is cached for this symbol."

    em = expected_move(spot, rank_info.get("atm_iv_pct") if rank_info else None, days_to_near)
    result["expected_move"] = em
    if em:
        outside_sweet_spot = em["upper"] > near_call["strike"] or em["lower"] < near_put["strike"]
        result["expected_move_vs_sweet_spot"] = (
            f"{days_to_near}-day expected move (±₹{em['expected_move']}, range {em['lower']}–{em['upper']}) "
            + ("extends BEYOND the short strikes (" + f"{near_put['strike']}–{near_call['strike']}"
               + ") — a normal move could already erode profit before near expiry."
               if outside_sweet_spot else
               "comfortably stays WITHIN the short strikes (" + f"{near_put['strike']}–{near_call['strike']}"
               + ") — favorable for this trade."))

    trend = get_trend_regime(symbol)
    result["trend"] = None if trend.get("error") else trend
    if trend.get("error"):
        result["trend_note"] = trend["error"]
    if trend and not trend.get("error") and trend.get("avoid_premium_selling"):
        result["trend_warning"] = (f"{trend['regime']} detected — calendars do best in range-bound/low-trend "
                                    f"conditions; a strong trend risks pushing spot outside the sweet spot.")

    vix, vix_err = get_india_vix()
    iv_rank_for_regime = rank_info.get("iv_rank_pct") if rank_info else None
    result["volatility_regime"] = classify_volatility_regime(vix, iv_rank_for_regime)
    if vix_err:
        result["volatility_regime"]["note"] = f"India VIX fetch failed ({vix_err}); classification unavailable."
    # Calendars are LONG vega (unlike condors/strangles which are short vega) — a rising-IV regime
    # after entry helps this trade, so flip the usual "avoid high vol" framing into a note here.
    result["vega_note"] = ("This trade is net LONG vega (benefits if IV rises after entry) and net SHORT "
                            "gamma near-term — opposite of the Iron Condor/Strangle builder's exposure. "
                            "A low-IV entry (cheap far-month vega) with room for IV to expand is typically "
                            "more favorable than entering when IV is already elevated.")

    score_components = []
    if iv_hv:
        # For a long-vega trade, a LOW iv/hv ratio (calm now, room to expand) scores better — inverse
        # of the condor/strangle scoring, which wants rich IV to sell.
        inv_label = {"avoid": 90, "fair": 70, "good": 55, "excellent": 35}.get(iv_hv["label"].lower(), 50)
        score_components.append(inv_label)
    if trend and not trend.get("error"):
        score_components.append(25 if trend.get("avoid_premium_selling") else 80)
    if result["volatility_regime"]["label"] != "Unknown":
        vr_score = {"Low Volatility": 80, "Normal": 65, "High Volatility": 35, "Extreme": 15}.get(
            result["volatility_regime"]["label"], 50)
        score_components.append(vr_score)
    if rank_info and rank_info.get("fo_banned_today"):
        score_components.append(0)
    trade_quality_score = round(sum(score_components) / len(score_components), 1) if score_components else None
    result["trade_quality_score"] = trade_quality_score
    if trade_quality_score is not None:
        result["trade_quality_label"] = ("Excellent" if trade_quality_score >= 80 else
                                          "Good" if trade_quality_score >= 60 else
                                          "Average" if trade_quality_score >= 40 else "Avoid")
    result["trade_quality_note"] = ("Heuristic score for a LONG-VEGA/theta trade: rewards calm current IV "
                                     "with room to rise, range-bound trend, and low-to-normal volatility "
                                     "regime — the inverse of the premium-selling score elsewhere in this "
                                     "dashboard. Not a probability, not backtested — a rough triage aid only.")

    return result


@app.route("/api/calendar-strategy/<symbol>")
def calendar_strategy(symbol):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    strike_mode = request.args.get("strike_mode", "otm_pct")
    otm_pct = float(request.args.get("otm_pct", DEFAULT_CALENDAR_OTM_PCT))
    target_delta = float(request.args.get("target_delta", DEFAULT_CALENDAR_TARGET_DELTA))
    near_expiry = request.args.get("near_expiry")
    far_expiry = request.args.get("far_expiry")
    lots = int(request.args.get("lots", 1))
    result = build_double_calendar_strategy(symbol, strike_mode, otm_pct, target_delta,
                                             near_expiry, far_expiry, lots)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/trend/<symbol>")
def trend(symbol):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    result = get_trend_regime(symbol)
    if result.get("error"):
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/position-sizing")
def position_sizing():
    """Dynamic position sizing (fixed-fractional): given total capital, risk-per-trade %, and
    the max loss of ONE lot of the trade you're considering, returns how many lots keep you
    within that risk budget."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    try:
        capital = float(request.args.get("capital"))
        risk_pct = float(request.args.get("risk_pct"))
        max_loss_per_lot = float(request.args.get("max_loss_per_lot"))
    except (TypeError, ValueError):
        return jsonify({"error": "capital, risk_pct, and max_loss_per_lot are all required numeric params"}), 400
    result = recommended_position_size(capital, risk_pct, max_loss_per_lot)
    if result is None:
        return jsonify({"error": "Invalid inputs — all values must be positive numbers"}), 400
    result["note"] = ("recommended_lots = floor((capital x risk_pct%) / max_loss_per_lot). This caps your RISK "
                       "budget only — it does not check margin availability. Always confirm actual margin "
                       "required (shown in the Strategy Builder) is within your free cash too.")
    return jsonify(result)


def position_greeks(position):
    """Per-position net Greeks via Black-Scholes at current quotes (Kite doesn't publish Greeks
    itself). Gamma/Vega/Theta are estimated by bump-and-reprice off the same bs_price/bs_delta
    helpers used everywhere else in this file. Handles double_calendar's two different expiries
    (near legs use position['expiry'], far legs use position['far_expiry'])."""
    strategy_type = position.get("strategy_type", "iron_condor")
    leg_keys = leg_keys_for(position)
    quantity = position.get("quantity", position["lot_size"])
    spot, err = get_spot_price(position["symbol"])
    if err:
        return {"error": err["error"]}

    today = now_ist().date()
    near_expiry_date = datetime.strptime(position["expiry"], "%Y-%m-%d").date()
    days_left_near = max((near_expiry_date - today).days, 0)
    far_expiry_date = None
    days_left_far = None
    if strategy_type == "double_calendar":
        far_expiry_date = datetime.strptime(position["far_expiry"], "%Y-%m-%d").date()
        days_left_far = max((far_expiry_date - today).days, 0)
        if days_left_near <= 0 and days_left_far <= 0:
            return {"error": "Position has expired"}
    else:
        if days_left_near <= 0:
            return {"error": "Position has expired"}

    inst_keys = [f"NFO:{position['legs'][k]['tradingsymbol']}" for k in leg_keys]
    try:
        quotes = kite_quote_bulk(inst_keys)
    except Exception as e:
        return {"error": str(e)}

    net_delta = net_theta = net_vega = net_gamma = 0.0
    for k in leg_keys:
        strike = position["legs"][k]["strike"]
        opt_type = "CE" if "call" in k else "PE"
        # calendars: near legs (sell_*_near) decay against the near expiry; far legs (buy_*_far)
        # against the far expiry. Everything else (iron_condor/strangle) has a single shared expiry.
        T = (days_left_far if (strategy_type == "double_calendar" and k.endswith("_far"))
             else days_left_near) / 365.0
        if T <= 0:
            continue
        ltp = extract_price(quotes.get(f"NFO:{position['legs'][k]['tradingsymbol']}"))
        if ltp is None:
            return {"error": f"No usable price for {k}"}
        iv = implied_vol(ltp, spot, strike, T, opt_type)
        delta = bs_delta(spot, strike, T, RISK_FREE_RATE, iv, opt_type)
        bump_s = spot * 0.01
        delta_up = bs_delta(spot + bump_s, strike, T, RISK_FREE_RATE, iv, opt_type)
        gamma = (delta_up - delta) / bump_s if bump_s else 0.0
        vega = (bs_price(spot, strike, T, RISK_FREE_RATE, iv + 0.01, opt_type)
                - bs_price(spot, strike, T, RISK_FREE_RATE, iv, opt_type))
        theta = -(bs_price(spot, strike, max(T - 1 / 365, 0), RISK_FREE_RATE, iv, opt_type)
                  - bs_price(spot, strike, T, RISK_FREE_RATE, iv, opt_type))
        sign = -1 if k.startswith("sell") else 1
        net_delta += sign * delta * quantity
        net_gamma += sign * gamma * quantity
        net_vega += sign * vega * quantity
        net_theta += sign * theta * quantity

    return {"net_delta": round(net_delta, 2), "net_gamma": round(net_gamma, 4),
            "net_vega": round(net_vega, 2), "net_theta": round(net_theta, 2)}



@app.route("/api/portfolio-greeks")
def portfolio_greeks():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    positions = load_positions()
    total_delta = total_theta = total_vega = total_gamma = 0.0
    details, errors = [], []
    for p in positions:
        g = position_greeks(p)
        if g.get("error"):
            errors.append({"id": p["id"], "symbol": p["symbol"], "error": g["error"]})
            continue
        total_delta += g["net_delta"]; total_gamma += g["net_gamma"]
        total_vega += g["net_vega"]; total_theta += g["net_theta"]
        details.append({"id": p["id"], "symbol": p["symbol"], **g})
    return jsonify({
        "net_delta": round(total_delta, 2), "net_gamma": round(total_gamma, 4),
        "net_vega": round(total_vega, 2), "net_theta": round(total_theta, 2),
        "positions": details, "errors": errors,
        "note": "Estimated via Black-Scholes at current quotes/implied vol — an approximation, not "
                "Kite's own Greeks (Kite doesn't publish them). Theta is per-day time decay; Vega is "
                "per 1-point (1%) change in IV.",
    })


@app.route("/api/best-trade")
def best_trade():
    """Rule-based 'Today's Best Trade' — combines the current Screener ranking with the Strategy
    Builder's enhanced output (IV/HV, expected move, trend, volatility regime) into one summary.
    This is NOT a machine-learning prediction and is NOT validated by backtesting — it's a
    transparent aggregation of the same signals shown elsewhere in this dashboard."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    if not SCREENER_CACHE["results"]:
        return jsonify({"error": "Run the Screener (section 1) first."}), 400
    eligible = [r for r in SCREENER_CACHE["results"] if r.get("rank")]
    if not eligible:
        return jsonify({"error": "No eligible stocks in the last screener run."}), 400
    eligible.sort(key=lambda r: r["rank"])
    top = eligible[0]
    strategy_type = request.args.get("strategy_type", "iron_condor")

    built = build_strategy(top["symbol"], strategy_type=strategy_type)
    if "error" in built:
        return jsonify({"error": f"Could not build a strategy for top pick {top['symbol']}: {built['error']}"}), 400

    reasons = []
    if built.get("iv_hv"):
        reasons.append(f"IV/HV ratio {built['iv_hv']['ratio']} ({built['iv_hv']['label']}).")
    if built.get("trend"):
        reasons.append(f"Trend regime: {built['trend']['regime']}.")
    if built.get("volatility_regime"):
        reasons.append(f"Volatility regime: {built['volatility_regime']['label']} "
                        f"(recommendation: {built['volatility_regime']['recommendation']}).")
    if built.get("suggested_strategy", {}).get("reason"):
        reasons.append(built["suggested_strategy"]["reason"])
    if built.get("expected_move_warning"):
        reasons.append(built["expected_move_warning"])

    risks = []
    if built.get("entry_event_warning"):
        risks.append(built["entry_event_warning"])
    if built.get("event_before_expiry"):
        risks.append(f"{built['event_before_expiry']['label']} on {built['event_before_expiry']['date']} "
                      f"falls before this expiry.")
    if built["strategy_type"] == "naked_strangle":
        risks.append("Naked strangle: unlimited risk on the call side.")

    max_profit = built.get("max_profit")
    return jsonify({
        "symbol": built["symbol"], "screener_rank": top["rank"], "strategy": built,
        "why_this_trade": reasons, "risks": risks,
        "expected_return": built.get("net_profit_after_entry_charges"),
        "probability_of_profit_pct": built.get("probability_of_profit_pct"),
        "max_risk": built.get("max_loss"),
        "suggested_exit_plan": [
            f"Profit target: exit at 50% of max profit"
            + (f" (₹{round(max_profit * 0.5, 2)})." if max_profit else "."),
            f"Time exit: close 3 days before expiry ({built['expiry']}) if still open.",
            f"Delta exit: exit a short leg if its delta rises to ≥{STOP_LOSS_DELTA_THRESHOLD}.",
        ],
        "note": "Rule-based triage using the current Screener + Strategy Builder output — NOT a "
                "machine-learning prediction, NOT investment advice, and NOT validated by backtesting. "
                "Verify everything before trading real money.",
    })


# ---------------------------------------------------------------------------
# Watchlist / Trade Section
# ---------------------------------------------------------------------------
@app.route("/api/watchlist/add", methods=["POST"])
def watchlist_add():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    symbol = body.get("symbol", "").upper()
    target_delta = float(body.get("target_delta", DEFAULT_TARGET_DELTA))
    wing_width_pct = float(body.get("wing_width_pct", DEFAULT_WING_WIDTH_PCT))
    strategy_type = body.get("strategy_type", "iron_condor")
    expiry_str = body.get("expiry")
    lots = int(body.get("lots", 1))

    built = build_strategy(symbol, target_delta, wing_width_pct, strategy_type, expiry_str, lots)
    if "error" in built:
        return jsonify(built), 404

    today_str = now_ist().date().isoformat()
    position = {
        "id": f"{symbol}_{int(time.time())}",
        "symbol": symbol,
        "added_on": today_str,
        "entry_spot": built["spot"],
        "expiry": built["expiry"],
        "lot_size": built["lot_size"],
        "lots": built["lots"],
        "quantity": built["quantity"],
        "strategy_type": built["strategy_type"],
        "legs": built["legs"],
        "entry_net_credit_per_share": built["net_credit_per_share"],
        "entry_max_profit": built["max_profit"],
        "entry_max_loss": built["max_loss"],
        "entry_margin_required": built.get("margin_required"),
        "entry_margin_error": built.get("margin_error"),
        "entry_estimated_charges": built.get("estimated_entry_charges", {}).get("total"),
        "breakeven_upper": built["breakeven_upper"],
        "breakeven_lower": built["breakeven_lower"],
        "broker_orders": [],
        "history": [{"date": today_str, "spot": built["spot"],
                     "pnl": 0.0, "current_debit_per_share": built["net_credit_per_share"]}],
    }
    positions = load_positions()
    positions.append(position)
    save_positions(positions)
    return jsonify({"ok": True, "position": position})


@app.route("/api/calendar-watchlist/add", methods=["POST"])
def calendar_watchlist_add():
    """Track-a-position counterpart of /api/watchlist/add, for Double Calendar Spreads. Kept as its
    own endpoint (rather than overloading /api/watchlist/add) since the position shape is different
    enough (two expiries, four legs with different leg-key names, debit instead of credit) to be
    clearer as a separate, explicit flow — mirrors how this dashboard keeps the Calendar tab separate."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    symbol = body.get("symbol", "").upper()
    strike_mode = body.get("strike_mode", "otm_pct")
    otm_pct = float(body.get("otm_pct", DEFAULT_CALENDAR_OTM_PCT))
    target_delta = float(body.get("target_delta", DEFAULT_CALENDAR_TARGET_DELTA))
    near_expiry = body.get("near_expiry")
    far_expiry = body.get("far_expiry")
    lots = int(body.get("lots", 1))

    built = build_double_calendar_strategy(symbol, strike_mode, otm_pct, target_delta,
                                            near_expiry, far_expiry, lots)
    if "error" in built:
        return jsonify(built), 404

    today_str = now_ist().date().isoformat()
    position = {
        "id": f"{symbol}_CAL_{int(time.time())}",
        "symbol": symbol,
        "added_on": today_str,
        "entry_spot": built["spot"],
        "strategy_type": "double_calendar",
        "strike_mode": built["strike_mode"],
        "expiry": built["near_expiry"],          # "expiry" = the near/critical management date
        "far_expiry": built["far_expiry"],
        "lot_size": built["lot_size"],
        "lots": built["lots"],
        "quantity": built["quantity"],
        "legs": built["legs"],
        "entry_net_debit_per_share": built["net_debit_per_share"],
        "entry_max_loss": built["max_loss"],
        "entry_max_profit_estimated": built["max_profit_estimated"],
        "entry_margin_required": built.get("margin_required"),
        "entry_margin_error": built.get("margin_error"),
        "entry_estimated_charges": built.get("estimated_entry_charges", {}).get("total"),
        "breakevens": built["breakevens"],
        "sweet_spot_range": built["sweet_spot_range"],
        "broker_orders": [],
        "history": [{"date": today_str, "spot": built["spot"],
                     "pnl": 0.0, "current_debit_per_share": built["net_debit_per_share"]}],
    }
    positions = load_positions()
    positions.append(position)
    save_positions(positions)
    return jsonify({"ok": True, "position": position})


@app.route("/api/calendar-watchlist/<pos_id>/curve")
def calendar_watchlist_curve(pos_id):
    """Regenerates a LIVE payoff curve for an already-tracked calendar position, using current spot
    and current far-leg IV (rather than the IV at entry time) — lets the Track Positions tab show how
    the expected max-profit/max-loss shape has shifted since entry, not just the frozen entry-day curve."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    position = find_position(pos_id)
    if not position or position.get("strategy_type") != "double_calendar":
        return jsonify({"error": "Calendar position not found"}), 404

    spot, err = get_spot_price(position["symbol"])
    if err:
        return jsonify(err), 404

    today = now_ist().date()
    near_expiry = datetime.strptime(position["expiry"], "%Y-%m-%d").date()
    far_expiry = datetime.strptime(position["far_expiry"], "%Y-%m-%d").date()
    days_to_near = max((near_expiry - today).days, 0)
    days_between = max((far_expiry - near_expiry).days, 1)

    call_strike = position["legs"]["sell_call_near"]["strike"]
    put_strike = position["legs"]["sell_put_near"]["strike"]
    quantity = position.get("quantity", position["lot_size"])

    inst_keys = [f"NFO:{position['legs']['buy_call_far']['tradingsymbol']}",
                 f"NFO:{position['legs']['buy_put_far']['tradingsymbol']}"]
    try:
        quotes = kite_quote_bulk(inst_keys)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    T_near_remaining = days_to_near / 365.0
    far_call_ltp = extract_price(quotes.get(inst_keys[0]))
    far_put_ltp = extract_price(quotes.get(inst_keys[1]))
    if far_call_ltp is None or far_put_ltp is None:
        return jsonify({"error": "No usable live price for one or both far legs"}), 400
    call_iv = implied_vol(far_call_ltp, spot, call_strike, T_near_remaining + days_between / 365.0, "CE")
    put_iv = implied_vol(far_put_ltp, spot, put_strike, T_near_remaining + days_between / 365.0, "PE")

    entry_debit = position["entry_net_debit_per_share"]
    T_far_remaining = days_between / 365.0

    def pnl_at_spot(s_t):
        near_call_intrinsic = max(s_t - call_strike, 0.0)
        near_put_intrinsic = max(put_strike - s_t, 0.0)
        far_call_value = bs_price(s_t, call_strike, T_far_remaining, RISK_FREE_RATE, call_iv, "CE")
        far_put_value = bs_price(s_t, put_strike, T_far_remaining, RISK_FREE_RATE, put_iv, "PE")
        position_value = (far_call_value - near_call_intrinsic) + (far_put_value - near_put_intrinsic)
        return position_value - entry_debit

    lo = spot * (1 - CALENDAR_CURVE_RANGE_PCT)
    hi = spot * (1 + CALENDAR_CURVE_RANGE_PCT)
    step = (hi - lo) / (CALENDAR_CURVE_POINTS - 1)
    curve = []
    for i in range(CALENDAR_CURVE_POINTS):
        s_t = lo + i * step
        curve.append({"spot": round(s_t, 2), "pnl": round(pnl_at_spot(s_t) * quantity, 2)})

    return jsonify({
        "position_id": pos_id, "spot": spot, "days_to_near_expiry": days_to_near,
        "curve": curve, "sweet_spot_range": [put_strike, call_strike],
        "note": "Live re-estimate using current spot and today's implied vol on the far legs — the "
                "curve shape will keep shifting daily as time passes and IV moves; treat it as a "
                "current best-guess snapshot, not a fixed prediction."
    })


@app.route("/api/watchlist/<pos_id>", methods=["DELETE"])
def watchlist_remove(pos_id):
    positions = load_positions()
    positions = [p for p in positions if p["id"] != pos_id]
    save_positions(positions)
    return jsonify({"ok": True})


def mark_to_market_calendar(position):
    """Double Calendar equivalent of mark_to_market() below — kept separate because the P&L math,
    zone logic, and exit rules are genuinely different for a debit calendar vs a credit condor/strangle
    (two expiries, model-based re-valuation of the far leg instead of a simple credit/debit diff)."""
    quantity = position.get("quantity", position["lot_size"])
    leg_keys = ["sell_call_near", "sell_put_near", "buy_call_far", "buy_put_far"]

    inst_keys = [f"NFO:{position['legs'][k]['tradingsymbol']}" for k in leg_keys]
    quotes = kite_quote_bulk(inst_keys)

    prices, missing_legs = {}, []
    for k in leg_keys:
        key = f"NFO:{position['legs'][k]['tradingsymbol']}"
        price = extract_price(quotes.get(key))
        prices[k] = price
        if price is None:
            missing_legs.append(f"{k} ({position['legs'][k]['tradingsymbol']})")
    if missing_legs:
        return {"__error__": "No usable price for: " + ", ".join(missing_legs) +
                              ". Contract may be expired/delisted, or market closed with no resting orders."}

    # Current cost to CLOSE this spread: sell the far longs at their ltp, buy back the near shorts
    # at their ltp. Position value rising above the entry debit is what "profit" means here.
    current_value_per_share = ((prices["buy_call_far"] - prices["sell_call_near"]) +
                                (prices["buy_put_far"] - prices["sell_put_near"]))
    entry_debit = position["entry_net_debit_per_share"]
    pnl_per_share = current_value_per_share - entry_debit
    pnl = round(pnl_per_share * quantity, 2)
    current_position_value = round(current_value_per_share * quantity, 2)

    spot, err = get_spot_price(position["symbol"])
    if err:
        return {"__error__": err["error"]}

    today = now_ist().date()
    near_expiry_date = datetime.strptime(position["expiry"], "%Y-%m-%d").date()
    far_expiry_date = datetime.strptime(position["far_expiry"], "%Y-%m-%d").date()
    days_left = (near_expiry_date - today).days
    T_near_remaining = max(days_left, 0) / 365.0

    call_strike = position["legs"]["sell_call_near"]["strike"]
    put_strike = position["legs"]["sell_put_near"]["strike"]
    sweet_spot_lo, sweet_spot_hi = put_strike, call_strike

    zone = "safe"
    if spot > sweet_spot_hi or spot < sweet_spot_lo:
        zone = "outside_sweet_spot"
    if days_left <= CALENDAR_NEAR_EXPIRY_DAYS_WARNING:
        zone = "near_expiry"

    delta_call = delta_put = None
    if days_left > 0:
        iv_call = implied_vol(prices["sell_call_near"], spot, call_strike, T_near_remaining, "CE")
        iv_put = implied_vol(prices["sell_put_near"], spot, put_strike, T_near_remaining, "PE")
        delta_call = bs_delta(spot, call_strike, T_near_remaining, RISK_FREE_RATE, iv_call, "CE")
        delta_put = bs_delta(spot, put_strike, T_near_remaining, RISK_FREE_RATE, iv_put, "PE")

    # Probability spot is still WITHIN the sweet spot (between the two short strikes) at near expiry —
    # lognormal approx using the same expected-move machinery used elsewhere in this file.
    probability_in_sweet_spot = None
    rank_info = get_stock_rank(position["symbol"])
    atm_iv_pct = rank_info.get("atm_iv_pct") if rank_info else None
    if atm_iv_pct and days_left > 0 and spot:
        sigma = atm_iv_pct / 100.0
        T = days_left / 365.0
        if sigma > 0 and T > 0:
            d_hi = (math.log(sweet_spot_hi / spot)) / (sigma * math.sqrt(T))
            d_lo = (math.log(sweet_spot_lo / spot)) / (sigma * math.sqrt(T))
            probability_in_sweet_spot = round((norm_cdf(d_hi) - norm_cdf(d_lo)) * 100, 1)
    elif days_left <= 0:
        probability_in_sweet_spot = 100.0 if zone == "safe" else 0.0

    # --- Exit suggestion (informational only) ---
    exit_suggested, exit_reasons = False, []
    entry_debit_total = abs(entry_debit * quantity)
    if entry_debit_total and pnl <= -CALENDAR_STOP_LOSS_DEBIT_MULTIPLE * entry_debit_total:
        exit_suggested = True
        exit_reasons.append(f"Loss (₹{abs(pnl)}) has reached {CALENDAR_STOP_LOSS_DEBIT_MULTIPLE}x the debit "
                             f"paid (₹{round(entry_debit_total,2)}).")
    if zone == "outside_sweet_spot":
        exit_suggested = True
        exit_reasons.append(f"Spot (₹{spot}) has moved outside the sweet spot range "
                             f"({sweet_spot_lo}–{sweet_spot_hi}) — the near leg is losing its edge.")
    if days_left <= CALENDAR_NEAR_EXPIRY_DAYS_WARNING and days_left >= 0:
        exit_suggested = True
        exit_reasons.append(f"Only {days_left} day(s) to near-leg expiry — consider closing or rolling "
                             f"the near leg to manage gamma/assignment risk.")

    event_flag = get_event_before_expiry(position["expiry"])

    exit_orders_for_charges = [
        {"price": prices["sell_call_near"], "quantity": quantity, "transaction_type": "BUY"},
        {"price": prices["sell_put_near"], "quantity": quantity, "transaction_type": "BUY"},
        {"price": prices["buy_call_far"], "quantity": quantity, "transaction_type": "SELL"},
        {"price": prices["buy_put_far"], "quantity": quantity, "transaction_type": "SELL"},
    ]
    exit_charges = estimate_charges(exit_orders_for_charges)
    entry_charges_total = position.get("entry_estimated_charges") or 0
    round_trip_charges = round(entry_charges_total + exit_charges["total"], 2)
    net_pnl_after_charges = round(pnl - round_trip_charges, 2)

    leg_details = {}
    for k in leg_keys:
        entry_price = position["legs"][k]["ltp"]
        current_price = prices[k]
        is_sell = k.startswith("sell")
        per_share = (entry_price - current_price) if is_sell else (current_price - entry_price)
        leg_details[k] = {
            "tradingsymbol": position["legs"][k]["tradingsymbol"],
            "strike": position["legs"][k]["strike"],
            "entry_price": entry_price, "current_price": round(current_price, 2),
            "pnl": round(per_share * quantity, 2),
        }
        if k == "sell_call_near" and delta_call is not None:
            leg_details[k]["current_delta"] = round(delta_call, 3)
        if k == "sell_put_near" and delta_put is not None:
            leg_details[k]["current_delta"] = round(delta_put, 3)

    entry_max_profit = position.get("entry_max_profit_estimated")
    return {
        "spot": spot, "pnl": pnl, "current_debit_per_share": round(current_value_per_share, 2),
        "current_position_value": current_position_value, "legs_current": leg_details,
        "days_left": days_left, "zone": zone,
        "probability_in_sweet_spot_pct": probability_in_sweet_spot,
        "pct_of_max_profit": round((pnl / entry_max_profit * 100), 1) if entry_max_profit else None,
        "exit_suggested": exit_suggested, "exit_reasons": exit_reasons,
        "event_before_expiry": event_flag,
        "entry_charges": entry_charges_total, "estimated_exit_charges": exit_charges["total"],
        "estimated_round_trip_charges": round_trip_charges, "net_pnl_after_charges": net_pnl_after_charges,
        "sweet_spot_range": [sweet_spot_lo, sweet_spot_hi],
    }


def mark_to_market(position):
    if position.get("strategy_type") == "double_calendar":
        return mark_to_market_calendar(position)

    strategy_type = position.get("strategy_type", "iron_condor")
    leg_keys = ["sell_call", "buy_call", "sell_put", "buy_put"] if strategy_type == "iron_condor" \
        else ["sell_call", "sell_put"]
    quantity = position.get("quantity", position["lot_size"])

    inst_keys = [f"NFO:{position['legs'][k]['tradingsymbol']}" for k in leg_keys]
    quotes = kite_quote_bulk(inst_keys)

    prices, missing_legs = {}, []
    for k in leg_keys:
        key = f"NFO:{position['legs'][k]['tradingsymbol']}"
        price = extract_price(quotes.get(key))
        prices[k] = price
        if price is None:
            missing_legs.append(f"{k} ({position['legs'][k]['tradingsymbol']})")

    if missing_legs:
        return {"__error__": "No usable price for: " + ", ".join(missing_legs) +
                              ". Contract may be expired/delisted, or market closed with no resting orders."}

    if strategy_type == "iron_condor":
        current_debit_per_share = (prices["sell_call"] + prices["sell_put"]) - (prices["buy_call"] + prices["buy_put"])
    else:
        current_debit_per_share = prices["sell_call"] + prices["sell_put"]

    pnl_per_share = position["entry_net_credit_per_share"] - current_debit_per_share
    pnl = round(pnl_per_share * quantity, 2)
    current_position_value = round(current_debit_per_share * quantity, 2)

    spot, err = get_spot_price(position["symbol"])
    if err:
        return {"__error__": err["error"]}

    today = now_ist().date()
    expiry_date = datetime.strptime(position["expiry"], "%Y-%m-%d").date()
    days_left = (expiry_date - today).days
    T_remaining = max(days_left, 0) / 365.0

    zone = "safe"
    if spot > position["breakeven_upper"] or spot < position["breakeven_lower"]:
        zone = "breached"
    elif days_left <= 2:
        zone = "near_expiry"

    probability_of_success = None
    delta_call = delta_put = None
    if days_left > 0:
        call_strike = position["legs"]["sell_call"]["strike"]
        put_strike = position["legs"]["sell_put"]["strike"]
        iv_call = implied_vol(prices["sell_call"], spot, call_strike, T_remaining, "CE")
        iv_put = implied_vol(prices["sell_put"], spot, put_strike, T_remaining, "PE")
        delta_call = bs_delta(spot, call_strike, T_remaining, RISK_FREE_RATE, iv_call, "CE")
        delta_put = bs_delta(spot, put_strike, T_remaining, RISK_FREE_RATE, iv_put, "PE")
        prob_call_itm = max(0.0, min(1.0, delta_call))
        prob_put_itm = max(0.0, min(1.0, abs(delta_put)))
        probability_of_success = round(max(0.0, 1 - prob_call_itm - prob_put_itm) * 100, 1)
    else:
        probability_of_success = 100.0 if zone == "safe" else 0.0

    # --- Stop-loss / exit suggestion (informational only — never auto-exits) ---
    # Trigger on whichever occurs first: total loss reaches N x premium received, or
    # either short leg's delta magnitude has risen to the threshold. Checking delta rather
    # than only waiting for the theoretical max loss catches a position going wrong earlier.
    exit_suggested, exit_reasons = False, []
    entry_premium_total = abs(position["entry_net_credit_per_share"] * quantity)
    if entry_premium_total and pnl <= -STOP_LOSS_PREMIUM_MULTIPLE * entry_premium_total:
        exit_suggested = True
        exit_reasons.append(f"Loss (₹{abs(pnl)}) has reached {STOP_LOSS_PREMIUM_MULTIPLE}x the premium "
                             f"received (₹{entry_premium_total}).")
    if delta_call is not None and abs(delta_call) >= STOP_LOSS_DELTA_THRESHOLD:
        exit_suggested = True
        exit_reasons.append(f"Short call delta has risen to {round(delta_call, 3)} "
                             f"(≥{STOP_LOSS_DELTA_THRESHOLD} threshold) — that side is losing its 'safety margin'.")
    if delta_put is not None and abs(delta_put) >= STOP_LOSS_DELTA_THRESHOLD:
        exit_suggested = True
        exit_reasons.append(f"Short put delta has risen to {round(delta_put, 3)} "
                             f"(≥{STOP_LOSS_DELTA_THRESHOLD} threshold) — that side is losing its 'safety margin'.")

    event_flag = get_event_before_expiry(position["expiry"])

    # --- Charges: entry (stored at tracking time) + a live exit-side estimate, giving a running
    # round-trip net P&L. This is what actually answers "what would I really pocket if I closed now."
    exit_orders_for_charges = []
    for k in leg_keys:
        original_txn = "SELL" if k.startswith("sell") else "BUY"
        close_txn = "BUY" if original_txn == "SELL" else "SELL"
        exit_orders_for_charges.append({"price": prices[k], "quantity": quantity, "transaction_type": close_txn})
    exit_charges = estimate_charges(exit_orders_for_charges)
    entry_charges_total = position.get("entry_estimated_charges") or 0
    round_trip_charges = round(entry_charges_total + exit_charges["total"], 2)
    net_pnl_after_charges = round(pnl - round_trip_charges, 2)

    leg_details = {}
    for k in leg_keys:
        entry_price = position["legs"][k]["ltp"]
        current_price = prices[k]
        is_sell = k.startswith("sell")
        # sold leg profits when price falls; bought leg profits when price rises
        per_share = (entry_price - current_price) if is_sell else (current_price - entry_price)
        leg_details[k] = {
            "tradingsymbol": position["legs"][k]["tradingsymbol"],
            "strike": position["legs"][k]["strike"],
            "entry_price": entry_price, "current_price": round(current_price, 2),
            "pnl": round(per_share * quantity, 2)
        }
        if k == "sell_call" and delta_call is not None:
            leg_details[k]["current_delta"] = round(delta_call, 3)
            leg_details[k]["probability_of_touch_pct"] = probability_of_touch(delta_call)
        if k == "sell_put" and delta_put is not None:
            leg_details[k]["current_delta"] = round(delta_put, 3)
            leg_details[k]["probability_of_touch_pct"] = probability_of_touch(delta_put)

    return {
        "spot": spot, "pnl": pnl, "current_debit_per_share": round(current_debit_per_share, 2),
        "current_position_value": current_position_value, "legs_current": leg_details,
        "days_left": days_left, "zone": zone, "probability_of_success_pct": probability_of_success,
        "pct_of_max_profit": round((pnl / position["entry_max_profit"] * 100), 1) if position["entry_max_profit"] else None,
        "exit_suggested": exit_suggested, "exit_reasons": exit_reasons,
        "event_before_expiry": event_flag,
        "entry_charges": entry_charges_total, "estimated_exit_charges": exit_charges["total"],
        "estimated_round_trip_charges": round_trip_charges, "net_pnl_after_charges": net_pnl_after_charges,
    }


@app.route("/api/watchlist")
def watchlist():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    positions = load_positions()
    today_str = now_ist().date().isoformat()
    out, changed = [], False
    for p in positions:
        try:
            mtm = mark_to_market(p)
        except Exception as e:
            logger.exception("mark_to_market failed for position %s (%s)", p.get("id"), p.get("symbol"))
            out.append({**p, "mtm_error": f"Internal error while pricing this position: {e}"})
            continue
        if mtm and "__error__" in mtm:
            out.append({**p, "mtm_error": mtm["__error__"]})
            continue
        if not p["history"] or p["history"][-1]["date"] != today_str:
            p["history"].append({"date": today_str, "spot": mtm["spot"], "pnl": mtm["pnl"],
                                  "current_debit_per_share": mtm["current_debit_per_share"]})
            changed = True
        out.append({**p, "current": mtm})
    if changed:
        save_positions(positions)
    return jsonify({"positions": out})


# ---------------------------------------------------------------------------
# Order execution — preview (no side effects) then confirm (places real orders)
# ---------------------------------------------------------------------------
def leg_keys_for(position):
    st = position.get("strategy_type")
    if st == "iron_condor":
        return ["sell_call", "buy_call", "sell_put", "buy_put"]
    if st == "double_calendar":
        return ["sell_call_near", "sell_put_near", "buy_call_far", "buy_put_far"]
    return ["sell_call", "sell_put"]


ORDER_TERMINAL_STATUSES = ("COMPLETE", "REJECTED", "CANCELLED")


def wait_for_order_terminal(order_id, timeout_seconds=8, poll_interval=0.5):
    """Polls Kite's order book for a specific order_id until it reaches a terminal state
    (COMPLETE / REJECTED / CANCELLED) or the timeout elapses. Returns the last status seen, or
    'TIMEOUT' if it was still open/pending when we stopped waiting (Kite market orders on NFO
    normally resolve in well under a second, so the timeout is just a safety net against a hung
    poll — it does not cancel the order)."""
    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        try:
            for o in kite.orders():
                if o.get("order_id") == order_id:
                    last_status = o.get("status")
                    break
        except Exception:
            pass
        if last_status in ORDER_TERMINAL_STATUSES:
            return last_status
        time.sleep(poll_interval)
    return last_status or "TIMEOUT"


def place_basket_orders(legs_to_place, product, order_type, sequence_for_margin=True):
    """Places each leg as a separate real order. Stops immediately on the first failure rather
    than continuing — continuing could leave a partial, unintentionally unhedged position.

    sequence_for_margin=True (the default for basket entry/close) sends every BUY leg first and
    — for MARKET orders — waits for each BUY to actually reach a terminal state before sending any
    SELL leg. Zerodha checks margin against your live positions at the moment each order hits the
    exchange, so a SELL leg fired before its offsetting BUY leg has filled can get REJECTED for
    insufficient margin even though the combo is fully hedged once both legs are in. Waiting for
    the BUY fill first lets the freed-up/hedged margin actually register before the SELL leg goes.
    Pass sequence_for_margin=False for one-off, independent leg placements/exits where there's no
    basket-level margin ordering to respect (e.g. the per-leg 'Execute this leg' button, or exiting
    an arbitrary set of live positions picked by the user)."""
    ordered = legs_to_place
    if sequence_for_margin:
        buys = [item for item in legs_to_place if item["transaction_type"] == "BUY"]
        sells = [item for item in legs_to_place if item["transaction_type"] != "BUY"]
        ordered = buys + sells

    results = []
    for item in ordered:
        txn_type = kite.TRANSACTION_TYPE_SELL if item["transaction_type"] == "SELL" else kite.TRANSACTION_TYPE_BUY
        quantity = int(item.get("quantity") or 1)
        reference_price = item.get("price")
        try:
            kwargs = dict(
                variety=kite.VARIETY_REGULAR, exchange=item.get("exchange", kite.EXCHANGE_NFO),
                tradingsymbol=item["tradingsymbol"], transaction_type=txn_type,
                quantity=quantity, product=getattr(kite, f"PRODUCT_{product}"),
                order_type=getattr(kite, f"ORDER_TYPE_{order_type}"),
                validity=kite.VALIDITY_DAY,
            )
            if item.get("tag"):
                kwargs["tag"] = str(item["tag"])[:20]
            if item.get("autoslice") is not None:
                kwargs["autoslice"] = bool(item["autoslice"])
            if item.get("market_protection") is not None and order_type in ("MARKET", "SL-M"):
                kwargs["market_protection"] = item["market_protection"]
            if order_type == "LIMIT" and reference_price:
                kwargs["price"] = float(reference_price)
            if order_type in ("MARKET", "SL-M") and "market_protection" not in kwargs:
                # -1 lets Zerodha apply its automatic market-protection band.
                kwargs["market_protection"] = -1
            try:
                order_id = kite.place_order(**kwargs)
            except TypeError as te:
                if "market_protection" in str(te):
                    # Installed kiteconnect SDK predates the market_protection parameter (a known
                    # PyPI packaging gap -- see zerodha/pykiteconnect issue #225). Retry without it;
                    # this will still work UNLESS your broker has already started enforcing the
                    # exchange's market-protection requirement, in which case upgrade the SDK:
                    #   pip install --upgrade kiteconnect
                    kwargs.pop("market_protection", None)
                    order_id = kite.place_order(**kwargs)
                else:
                    raise

            fill_status = None
            if sequence_for_margin and order_type == "MARKET":
                fill_status = wait_for_order_terminal(order_id)
                if fill_status == "REJECTED":
                    results.append({"leg": item.get("leg", "?"), "tradingsymbol": item["tradingsymbol"],
                                     "transaction_type": item["transaction_type"], "quantity": quantity,
                                     "status": "failed", "order_id": order_id, "fill_status": fill_status,
                                     "error": "Order was REJECTED by the exchange/broker.",
                                     "reference_price": reference_price})
                    break

            results.append({"leg": item.get("leg", "?"), "tradingsymbol": item["tradingsymbol"],
                             "transaction_type": item["transaction_type"], "quantity": quantity,
                             "status": "placed", "order_id": order_id, "fill_status": fill_status,
                             "estimated_realized_pnl": 0,  # filled in by caller if this is a closing trade
                             "reference_price": reference_price})
        except Exception as e:
            results.append({"leg": item.get("leg", "?"), "tradingsymbol": item["tradingsymbol"],
                             "transaction_type": item["transaction_type"], "quantity": quantity,
                             "status": "failed", "error": str(e)})
            break
    return results


@app.route("/api/execute/<pos_id>/preview")
def execute_preview(pos_id):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    position = find_position(pos_id)
    if not position:
        return jsonify({"error": "Position not found"}), 404

    try:
        orders = refresh_execution_quotes(position)
    except Exception as e:
        return jsonify({"error": f"Could not fetch live Bid/Ask: {e}"}), 502

    return jsonify({
        "position_id": pos_id,
        "symbol": position["symbol"],
        "orders": orders,
        "default_product": "NRML",
        "default_order_type": "LIMIT",
        "warning": "LIMIT prices use best Ask for BUY legs and best Bid for SELL legs. "
                   "Quotes can change before the order reaches the exchange."
    })


@app.route("/api/execute/<pos_id>/refresh-quotes")
def execute_refresh_quotes(pos_id):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    position = find_position(pos_id)
    if not position:
        return jsonify({"error": "Position not found"}), 404

    try:
        orders = refresh_execution_quotes(position)
        return jsonify({
            "position_id": pos_id,
            "symbol": position["symbol"],
            "orders": orders,
            "refreshed_at": now_ist().strftime("%H:%M:%S")
        })
    except Exception as e:
        return jsonify({"error": f"Could not refresh live Bid/Ask: {e}"}), 502


@app.route("/api/execute/<pos_id>/leg", methods=["POST"])
def execute_single_leg(pos_id):
    """Place exactly one entry leg chosen by the user from the execution review."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    if not body.get("confirmed"):
        return jsonify({"error": "Confirmation flag not set — nothing was placed."}), 400
    position = find_position(pos_id)
    if not position:
        return jsonify({"error": "Position not found"}), 404
    order = body.get("order")
    if not order or not order.get("tradingsymbol") or not order.get("transaction_type"):
        return jsonify({"error": "No valid entry leg order provided."}), 400
    product = body.get("product", "NRML")
    order_type = body.get("order_type", "LIMIT")
    order = dict(order)
    if order_type == "LIMIT" and order.get("price_source") == "AUTO":
        try:
            fresh = {o["leg"]: o for o in refresh_execution_quotes(position)}
            fq = fresh.get(order.get("leg"))
            if fq and fq.get("recommended_limit_price") is not None:
                order["price"] = fq["recommended_limit_price"]
                order["ltp"] = fq.get("ltp")
                order["bid"] = fq.get("bid")
                order["ask"] = fq.get("ask")
        except Exception as e:
            return jsonify({"error": f"Could not refresh live Bid/Ask before placement: {e}"}), 502
    if order_type == "LIMIT" and not order.get("price"):
        return jsonify({"error": "A LIMIT price is required. Refresh prices or enter a price manually."}), 400
    results = place_basket_orders([order], product, order_type, sequence_for_margin=False)
    positions = load_positions()
    for p in positions:
        if p["id"] == pos_id:
            p["broker_orders"] = p.get("broker_orders", []) + results
    save_positions(positions)
    return jsonify({"results": results, "position_id": pos_id, "order": order})


@app.route("/api/execute/<pos_id>/confirm", methods=["POST"])
def execute_confirm(pos_id):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    if not body.get("confirmed"):
        return jsonify({"error": "Confirmation flag not set — nothing was placed."}), 400

    position = find_position(pos_id)
    if not position:
        return jsonify({"error": "Position not found"}), 404

    product = body.get("product", "NRML")
    order_type = body.get("order_type", "MARKET")

    # Use the exact list of orders the user reviewed/edited in the UI if provided (each item may
    # have its own quantity and, for LIMIT orders, its own price). Falls back to the position's
    # default legs if the frontend didn't send an explicit list, for backward compatibility.
    custom_orders = body.get("orders")
    if custom_orders:
        legs_to_place = custom_orders
    else:
        default_quantity = position.get("quantity", position["lot_size"])
        legs_to_place = [{
            "leg": k, "tradingsymbol": position["legs"][k]["tradingsymbol"],
            "transaction_type": "SELL" if k.startswith("sell") else "BUY",
            "quantity": default_quantity, "price": position["legs"][k]["ltp"],
        } for k in leg_keys_for(position)]

    if not legs_to_place:
        return jsonify({"error": "No legs left to place — every leg was removed in the review screen."}), 400

    if order_type == "LIMIT":
        # Last-second server refresh for rows still marked AUTO.
        try:
            fresh = {o["leg"]: o for o in refresh_execution_quotes(position)}
            refreshed = []
            for item in legs_to_place:
                item = dict(item)
                if item.get("price_source") == "AUTO":
                    fq = fresh.get(item.get("leg"))
                    if fq and fq.get("recommended_limit_price") is not None:
                        item["price"] = fq["recommended_limit_price"]
                refreshed.append(item)
            legs_to_place = refreshed
        except Exception as e:
            return jsonify({"error": f"Could not refresh live Bid/Ask before placement: {e}"}), 502

    results = place_basket_orders(legs_to_place, product, order_type)

    positions = load_positions()
    for p in positions:
        if p["id"] == pos_id:
            p["broker_orders"] = p.get("broker_orders", []) + results
    save_positions(positions)

    any_failed = any(r["status"] == "failed" for r in results)
    placed_count = sum(1 for r in results if r["status"] == "placed")
    total_legs = len(legs_to_place)
    partial = any_failed and placed_count > 0

    return jsonify({
        "results": results,
        "partial_failure": partial,
        "note": ("PARTIAL EXECUTION: some legs placed, one failed. You may now hold an incomplete, "
                 "unhedged position. Open your Zerodha app / Kite web IMMEDIATELY to check your actual "
                 "positions and orders, and manually complete or exit as needed."
                 if partial else
                 "All legs failed — nothing was placed." if any_failed and placed_count == 0 else
                 f"All {placed_count}/{total_legs} legs placed successfully. Verify fills in your Zerodha app.")
    })


# ---------------------------------------------------------------------------
# Close / square-off a position — reverses each leg (buy back what you sold,
# sell what you bought) to flatten it before expiry.
# ---------------------------------------------------------------------------
def build_close_orders(position):
    """Reverse of the entry orders, with a fresh reference price per leg from live quotes."""
    quantity = position.get("quantity", position["lot_size"])
    leg_keys = leg_keys_for(position)
    inst_keys = [f"NFO:{position['legs'][k]['tradingsymbol']}" for k in leg_keys]
    quotes = kite_quote_bulk(inst_keys)

    orders = []
    for k in leg_keys:
        leg = position["legs"][k]
        original_txn = "SELL" if k.startswith("sell") else "BUY"
        close_txn = "BUY" if original_txn == "SELL" else "SELL"
        ref_price = extract_price(quotes.get(f"NFO:{leg['tradingsymbol']}"))
        orders.append({
            "leg": k, "tradingsymbol": leg["tradingsymbol"], "transaction_type": close_txn,
            "quantity": quantity, "price": ref_price, "reference_price": ref_price,
            "entry_price": leg["ltp"], "original_transaction_type": original_txn,
        })
    return orders


@app.route("/api/execute/<pos_id>/close/preview")
def close_preview(pos_id):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    position = find_position(pos_id)
    if not position:
        return jsonify({"error": "Position not found"}), 404
    orders = build_close_orders(position)
    return jsonify({
        "position_id": pos_id, "symbol": position["symbol"], "orders": orders,
        "default_product": "NRML", "default_order_type": "MARKET",
        "warning": "This will CLOSE/SQUARE OFF this position — buying back what you sold and selling what "
                   "you bought, at current market prices. Review carefully, then confirm to send these real "
                   "orders to your Zerodha account."
    })


@app.route("/api/execute/<pos_id>/close/leg", methods=["POST"])
def close_single_leg(pos_id):
    """Places exactly ONE closing leg right now — the close-flow counterpart of
    /api/execute/<pos_id>/leg, for manually sequencing a square-off leg by leg."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    if not body.get("confirmed"):
        return jsonify({"error": "Confirmation flag not set — nothing was placed."}), 400
    order = body.get("order")
    if not order or not order.get("tradingsymbol"):
        return jsonify({"error": "No leg order provided."}), 400

    position = find_position(pos_id)
    if not position:
        return jsonify({"error": "Position not found"}), 404

    product = body.get("product", "NRML")
    order_type = body.get("order_type", "MARKET")
    results = place_basket_orders([order], product, order_type, sequence_for_margin=False)

    positions = load_positions()
    for p in positions:
        if p["id"] == pos_id:
            p["broker_orders"] = p.get("broker_orders", []) + results
    save_positions(positions)

    return jsonify({"results": results})


@app.route("/api/execute/<pos_id>/close/confirm", methods=["POST"])
def close_confirm(pos_id):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    if not body.get("confirmed"):
        return jsonify({"error": "Confirmation flag not set — nothing was placed."}), 400

    position = find_position(pos_id)
    if not position:
        return jsonify({"error": "Position not found"}), 404

    product = body.get("product", "NRML")
    order_type = body.get("order_type", "MARKET")
    custom_orders = body.get("orders")
    legs_to_place = custom_orders if custom_orders else build_close_orders(position)

    if not legs_to_place:
        return jsonify({"error": "No legs left to place — every leg was removed in the review screen."}), 400

    results = place_basket_orders(legs_to_place, product, order_type)

    # Estimate realized P&L per leg using the reference price captured at preview/placement time
    # (NOT a confirmed fill price — market orders execute asynchronously). Purely informational.
    entry_by_leg = {o["leg"]: o.get("entry_price") for o in legs_to_place}
    for r in results:
        if r["status"] != "placed":
            continue
        entry_price = entry_by_leg.get(r["leg"])
        close_price = next((o.get("reference_price") for o in legs_to_place if o["leg"] == r["leg"]), None)
        if entry_price is not None and close_price is not None:
            is_sell_originally = r["transaction_type"] == "BUY"  # closing a BUY means original leg was a SELL
            per_share = (entry_price - close_price) if is_sell_originally else (close_price - entry_price)
            r["estimated_realized_pnl"] = round(per_share * r["quantity"], 2)

    positions = load_positions()
    still_present = None
    for p in positions:
        if p["id"] == pos_id:
            p["broker_orders"] = p.get("broker_orders", []) + results
            still_present = p

    any_failed = any(r["status"] == "failed" for r in results)
    placed_count = sum(1 for r in results if r["status"] == "placed")
    total_legs = len(legs_to_place)
    fully_closed = placed_count == total_legs and not any_failed

    if fully_closed and still_present:
        archive_closed_position(still_present, results)
        positions = [p for p in positions if p["id"] != pos_id]
        note = (f"Position fully closed and archived to trade_history.json. "
                f"Estimated realized P&L: ₹{round(sum(r.get('estimated_realized_pnl', 0) for r in results), 2)} "
                f"(based on quoted prices at close, not confirmed fills — check your contract note).")
    elif any_failed and placed_count > 0:
        note = ("PARTIAL CLOSE: some legs closed, one failed. You may now hold a mismatched position. "
                "Open your Zerodha app / Kite web IMMEDIATELY to check and manually complete the close.")
    elif any_failed:
        note = "All legs failed — nothing was closed."
    else:
        note = f"All {placed_count}/{total_legs} legs placed to close this position. Verify fills in your Zerodha app."

    save_positions(positions)
    return jsonify({"results": results, "fully_closed": fully_closed, "note": note})



# ---------------------------------------------------------------------------
# Existing Position Intelligence — live Zerodha positions
# ---------------------------------------------------------------------------
def _position_instrument_map():
    """Map live option tradingsymbols to exchange/instrument metadata."""
    mp = {}
    try:
        nfo, _ = get_instruments()
        for i in nfo:
            if i.get("segment") == "NFO-OPT":
                mp[i.get("tradingsymbol")] = i
    except Exception:
        pass
    try:
        for i in get_bse_instruments():
            if i.get("segment") == "BFO-OPT":
                mp[i.get("tradingsymbol")] = i
    except Exception:
        pass
    return mp


def _option_quote_key(inst):
    ex = inst.get("exchange") or ("BFO" if inst.get("segment") == "BFO-OPT" else "NFO")
    return f"{ex}:{inst['tradingsymbol']}"


def _classify_position_group(legs):
    """Classify a same-underlying/same-expiry basket."""
    calls = [x for x in legs if x["type"] == "CE"]
    puts = [x for x in legs if x["type"] == "PE"]
    shorts = [x for x in legs if x["side"] == "SHORT"]
    longs = [x for x in legs if x["side"] == "LONG"]

    if len(legs) == 4 and len(calls) == 2 and len(puts) == 2 and len(shorts) == 2 and len(longs) == 2:
        return "IRON CONDOR"
    if len(legs) == 2 and len(calls) == 1 and len(puts) == 1 and len(shorts) == 2:
        return "SHORT STRANGLE"
    if len(legs) == 2 and len(calls) == 1 and len(puts) == 1 and len(longs) == 2:
        return "LONG STRADDLE" if calls[0]["strike"] == puts[0]["strike"] else "LONG STRANGLE"
    if len(legs) == 2 and ((len(calls) == 2) or (len(puts) == 2)):
        return "VERTICAL SPREAD"
    if len(legs) == 4:
        return "4-LEG / REVIEW"
    return "UNCLASSIFIED"


def _intraday_position_momentum(symbol):
    """5-minute live momentum snapshot used only for management context."""
    token, err = resolve_token_for_symbol(symbol)
    if err:
        return {"error": err}
    try:
        end = now_ist()
        start = end - timedelta(days=4)
        candles = kite.historical_data(token, start, end, "5minute")
    except Exception as e:
        return {"error": str(e)}
    if len(candles) < 25:
        return {"error": "Not enough 5-minute candles"}

    c = candles[-120:]
    closes = np.array([float(x["close"]) for x in c])
    highs = np.array([float(x["high"]) for x in c])
    lows = np.array([float(x["low"]) for x in c])
    vols = np.array([float(x.get("volume") or 0) for x in c])

    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    rsi = _rsi(closes, 14)
    adx = _adx(highs, lows, closes, 14)
    vwap = None
    if np.sum(vols) > 0:
        typical = (highs + lows + closes) / 3.0
        vwap = float(np.sum(typical * vols) / np.sum(vols))

    spot = float(closes[-1])
    ret5 = (spot / closes[-2] - 1) * 100 if len(closes) >= 2 else 0
    ret30 = (spot / closes[-7] - 1) * 100 if len(closes) >= 7 else None
    ret60 = (spot / closes[-13] - 1) * 100 if len(closes) >= 13 else None
    avg_vol = float(np.mean(vols[-21:-1])) if len(vols) >= 22 and np.mean(vols[-21:-1]) > 0 else None
    rv = float(vols[-1] / avg_vol) if avg_vol else None

    direction = "BULLISH" if ((ema20 is not None and ema50 is not None and ema20 > ema50)
                               and (rsi is None or rsi >= 55)) else \
                "BEARISH" if ((ema20 is not None and ema50 is not None and ema20 < ema50)
                              and (rsi is None or rsi <= 45)) else "MIXED"

    strength = "STRONG" if adx is not None and adx >= 25 else "MODERATE" if adx is not None and adx >= 20 else "WEAK/RANGE"

    return {
        "spot": round(spot, 2), "ema20": round(ema20, 2) if ema20 is not None else None,
        "ema50": round(ema50, 2) if ema50 is not None else None,
        "rsi": round(rsi, 1) if rsi is not None else None,
        "adx": round(adx, 1) if adx is not None else None,
        "vwap": round(vwap, 2) if vwap is not None else None,
        "return_5m_pct": round(ret5, 3), "return_30m_pct": round(ret30, 3) if ret30 is not None else None,
        "return_60m_pct": round(ret60, 3) if ret60 is not None else None,
        "relative_volume": round(rv, 2) if rv is not None else None,
        "direction": direction, "strength": strength,
        "above_vwap": bool(vwap is not None and spot > vwap),
    }


def _position_management_action(group, momentum):
    """Transparent rule-based triage. It recommends a management action; it never places orders."""
    dte = group["dte"]
    pnl = group["pnl"]
    captured = group.get("profit_captured_pct")
    call_risk = group.get("call_risk_score", 0)
    put_risk = group.get("put_risk_score", 0)
    call_delta = abs(group.get("short_call_delta") or 0)
    put_delta = abs(group.get("short_put_delta") or 0)
    direction = momentum.get("direction") if momentum and not momentum.get("error") else "MIXED"
    strength = momentum.get("strength") if momentum and not momentum.get("error") else "UNKNOWN"

    reasons, actions = [], []
    threatened = None
    if group["strategy"] == "IRON CONDOR":
        if direction == "BULLISH" and call_risk >= put_risk:
            threatened = "CALL"
        elif direction == "BEARISH" and put_risk >= call_risk:
            threatened = "PUT"
        elif call_risk > put_risk * 1.25:
            threatened = "CALL"
        elif put_risk > call_risk * 1.25:
            threatened = "PUT"

    if captured is not None and captured >= 70 and dte <= 10:
        action, level = "CLOSE ENTIRE POSITION / TAKE PROFIT", "HIGH"
        reasons.append(f"{captured:.0f}% of estimated entry credit has been captured with only {dte} DTE.")
    elif group["strategy"] == "IRON CONDOR" and threatened and (
        (threatened == "CALL" and (call_delta >= 0.30 or call_risk >= 75)) or
        (threatened == "PUT" and (put_delta >= 0.30 or put_risk >= 75))
    ):
        action, level = f"ADJUST {threatened} SIDE — DO NOT WAIT FOR EXPIRY", "HIGH"
        reasons.append(f"{threatened} side is the threatened side based on spot distance and short-leg delta.")
        reasons.append(f"Short {threatened} delta is {call_delta if threatened=='CALL' else put_delta:.2f}.")
        if strength == "STRONG":
            reasons.append("Underlying momentum is strong, increasing breakout/gamma risk.")
    elif group["strategy"] == "IRON CONDOR" and dte <= 3 and (call_risk >= 55 or put_risk >= 55):
        action, level = "REDUCE RISK / CONSIDER FULL EXIT", "HIGH"
        reasons.append(f"{dte} DTE with a short strike under meaningful pressure.")
    elif group["strategy"] == "IRON CONDOR" and pnl < 0 and (call_risk >= 55 or put_risk >= 55):
        action, level = f"ADJUST THREATENED {threatened or 'SIDE'}", "MEDIUM"
        reasons.append("Position is losing while one side is becoming materially closer to the underlying.")
    elif group["strategy"] == "IRON CONDOR" and captured is not None and captured >= 50:
        action, level = "HOLD / PROTECT PROFIT", "LOW"
        reasons.append(f"{captured:.0f}% of estimated entry credit is captured and no severe side breach is detected.")
    elif group["strategy"] in ("LONG STRADDLE", "LONG STRANGLE"):
        # Opposite economics from everything above: this position PAID a debit and wants a big
        # move, so it never had an "entry credit" to capture — profit_captured_pct is None by
        # construction. Judge it instead against the debit paid (entry_credit_total is negative
        # for a debit trade) and against how much runway is left before theta finishes the job.
        entry_debit_total = abs(group.get("entry_credit_total", 0) or 0)
        if entry_debit_total and pnl >= POSITION_DEBIT_PROFIT_TARGET * entry_debit_total:
            action, level = "BOOK PROFIT — CONSIDER CLOSING", "MEDIUM"
            reasons.append(f"Profit (₹{pnl}) has reached {int(POSITION_DEBIT_PROFIT_TARGET*100)}% of the debit "
                           f"paid (₹{round(entry_debit_total,2)}) — long options can give profit back fast if "
                           f"the move stalls or IV drops.")
        elif entry_debit_total and pnl <= -POSITION_DEBIT_STOP_PCT * entry_debit_total:
            action, level = "CUT LOSS — EXPECTED MOVE HASN'T SHOWN UP", "HIGH"
            reasons.append(f"Loss (₹{abs(pnl)}) has reached {int(POSITION_DEBIT_STOP_PCT*100)}%+ of the debit "
                           f"paid (₹{round(entry_debit_total,2)}) — theta is doing its job against you.")
        elif dte <= POSITION_DEBIT_EXPIRY_WARNING_DAYS:
            action, level = "CLOSE BEFORE EXPIRY — THETA/GAMMA RISK", "MEDIUM"
            reasons.append(f"Only {dte} DTE left on a long-premium position — decay accelerates fastest right here.")
        elif strength == "STRONG" and pnl > 0:
            action, level = "HOLD — MOVE APPEARS TO BE DEVELOPING", "LOW"
            reasons.append(f"Underlying momentum is {strength.lower()} and the position is in profit — let the "
                           f"move run, but watch for an IV crush after any anticipated event.")
        else:
            action, level = "HOLD AND MONITOR", "LOW"
            reasons.append("No move of consequence yet on this long-premium position — it is decaying with "
                           "time; re-check DTE and P&L regularly rather than waiting passively for expiry.")
    else:
        action, level = "HOLD AND MONITOR", "LOW"
        reasons.append("No current rule-based trigger for adjustment or full exit.")

    if direction in ("BULLISH", "BEARISH"):
        reasons.append(f"Underlying is currently {direction.lower()} with {strength.lower()} momentum.")
    if momentum.get("above_vwap") is True:
        reasons.append("Price is above VWAP.")
    elif momentum.get("above_vwap") is False:
        reasons.append("Price is below VWAP.")

    return {"action": action, "level": level, "threatened_side": threatened,
            "reasons": reasons,
            "execution_note": "Recommendation only. Review live option-chain prices and margin before placing any adjustment."}


@app.route("/api/existing-position-analysis")
def existing_position_analysis():
    """Analyse every currently open Zerodha NFO/BFO option basket as a whole."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401

    try:
        raw = kite.positions().get("net", [])
        instruments = _position_instrument_map()
        rows = []
        for p in raw:
            if p.get("exchange") not in ("NFO", "BFO"):
                continue
            qty = int(p.get("quantity") or 0)
            ts = p.get("tradingsymbol")
            if qty == 0 or not ts or ts not in instruments:
                continue
            ins = instruments[ts]
            rows.append({
                "tradingsymbol": ts, "exchange": p.get("exchange"),
                "quantity": abs(qty), "side": "LONG" if qty > 0 else "SHORT",
                "entry_price": float(p.get("average_price") or 0),
                "ltp": float(p.get("last_price") or 0),
                "pnl": float(p.get("pnl") or 0),
                "type": ins.get("instrument_type"), "strike": float(ins.get("strike") or 0),
                "expiry": str(ins.get("expiry")), "underlying": ins.get("name"),
                "lot_size": int(ins.get("lot_size") or 1),
                "instrument": ins,
            })

        # Group by underlying + expiry. This intentionally groups the user's actual
        # open legs rather than relying on positions.json.
        grouped = {}
        for r in rows:
            key = (r["underlying"], r["expiry"])
            grouped.setdefault(key, []).append(r)

        analyses = []
        for (underlying, expiry), legs in grouped.items():
            strategy = _classify_position_group(legs)
            spot, spot_err = get_spot_price(underlying)
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            dte = max((exp_date - now_ist().date()).days, 0)

            # Live quotes for executable exit prices and current leg Greeks.
            keys = [_option_quote_key(x["instrument"]) for x in legs]
            quotes = kite_quote_bulk(keys, force_refresh=True)
            short_call_delta = short_put_delta = None
            call_risk = put_risk = 0.0
            initial_credit_total = 0.0
            current_close_debit = 0.0

            for r in legs:
                q = quotes.get(_option_quote_key(r["instrument"])) or {}
                bid, ask = extract_bid_ask(q)
                r["bid"], r["ask"] = bid, ask
                exit_px = bid if r["side"] == "LONG" else ask
                r["exit_price"] = exit_px
                # Short entry contributes positive credit; long entry is debit.
                sign_credit = 1 if r["side"] == "SHORT" else -1
                initial_credit_total += sign_credit * r["entry_price"] * r["quantity"]
                if exit_px is not None:
                    # Cost to close a short = buy at ask; proceeds from closing long = sell at bid.
                    current_close_debit += (exit_px * r["quantity"]) * (1 if r["side"] == "SHORT" else -1)

            if initial_credit_total > 0:
                current_value_profit = initial_credit_total - current_close_debit
                captured = max(0.0, min(100.0, current_value_profit / initial_credit_total * 100))
            else:
                current_value_profit = sum(r["pnl"] for r in legs)
                captured = None

            if spot is not None:
                for r in legs:
                    T = max((exp_date - now_ist().date()).days, 0) / 365.0
                    ltp = r["ltp"]
                    iv = implied_vol(ltp, spot, r["strike"], T, r["type"]) if ltp and T > 0 else 0.25
                    delta = bs_delta(spot, r["strike"], T, RISK_FREE_RATE, iv, r["type"]) if T > 0 else (1.0 if ((r["type"]=="CE" and spot>r["strike"]) or (r["type"]=="PE" and spot<r["strike"])) else 0.0)
                    r["delta"] = round(float(delta), 3)
                    r["iv_pct"] = round(float(iv*100), 1)
                    if r["side"] == "SHORT" and r["type"] == "CE":
                        short_call_delta = r["delta"]
                    if r["side"] == "SHORT" and r["type"] == "PE":
                        short_put_delta = r["delta"]

            short_calls = [r for r in legs if r["side"]=="SHORT" and r["type"]=="CE"]
            short_puts = [r for r in legs if r["side"]=="SHORT" and r["type"]=="PE"]
            if spot is not None:
                if short_calls:
                    sc = short_calls[0]
                    call_risk = min(100.0, abs(spot-sc["strike"])/max(spot,1)*1000 + abs(short_call_delta or 0)*150)
                if short_puts:
                    sp = short_puts[0]
                    put_risk = min(100.0, abs(spot-sp["strike"])/max(spot,1)*1000 + abs(short_put_delta or 0)*150)
                # Distance is the better directional measure: closer strike = higher risk.
                if short_calls:
                    dist = (short_calls[0]["strike"]-spot)/spot*100
                    call_risk = min(100.0, max(0.0, 100.0 - dist*40) + abs(short_call_delta or 0)*50)
                if short_puts:
                    dist = (spot-short_puts[0]["strike"])/spot*100
                    put_risk = min(100.0, max(0.0, 100.0 - dist*40) + abs(short_put_delta or 0)*50)

            momentum = _intraday_position_momentum(underlying)
            health = 100.0
            if pnl := sum(r["pnl"] for r in legs):
                if pnl < 0: health -= min(30, abs(pnl)/max(abs(initial_credit_total), 1)*30)
            health -= min(30, max(call_risk, put_risk)*0.30)
            if dte <= 3: health -= 15
            elif dte <= 7: health -= 8
            if momentum.get("strength") == "STRONG": health -= 8
            health = round(max(0, min(100, health)), 0)

            group = {
                "underlying": underlying, "expiry": expiry, "dte": dte, "strategy": strategy,
                "legs": [{k:v for k,v in r.items() if k != "instrument"} for r in legs],
                "pnl": round(sum(r["pnl"] for r in legs), 2),
                "entry_credit_total": round(initial_credit_total, 2),
                "current_close_debit": round(current_close_debit, 2),
                "profit_captured_pct": round(captured, 1) if captured is not None else None,
                "spot": round(spot, 2) if spot is not None else None,
                "short_call_delta": short_call_delta, "short_put_delta": short_put_delta,
                "call_risk_score": round(call_risk, 1), "put_risk_score": round(put_risk, 1),
                "health_score": int(health), "momentum": momentum,
            }
            group["recommendation"] = _position_management_action(group, momentum)
            # Scenario distances to the short strikes.
            group["scenario"] = {}
            if spot is not None:
                for pct in (-2,-1,-0.5,0.5,1,2):
                    s = spot*(1+pct/100)
                    scenario_pnl = 0.0
                    T = max(dte,0)/365.0
                    for r in legs:
                        iv = (r.get("iv_pct") or 20)/100
                        theo = bs_price(s,r["strike"],T,RISK_FREE_RATE,iv,r["type"]) if T>0 else max((s-r["strike"]) if r["type"]=="CE" else (r["strike"]-s),0)
                        # mark position to theoretical option value
                        scenario_pnl += (theo-r["entry_price"]) * r["quantity"] * (1 if r["side"]=="LONG" else -1)
                    group["scenario"][f"{pct:+g}%"] = round(scenario_pnl,2)
            analyses.append(group)

        analyses.sort(key=lambda x: x["health_score"])
        return jsonify({
            "positions": analyses,
            "count": len(analyses),
            "refreshed_at": now_ist().strftime("%H:%M:%S"),
            "note": "Rule-based live position management. Recommendations are decision support, not automatic orders or guarantees. Greeks/IV are model estimates."
        })
    except Exception as e:
        logger.exception("Existing position analysis failed")
        return jsonify({"error": str(e)}), 400


@app.route("/api/broker-positions")
def broker_positions():
    """Live F&O positions straight from your Zerodha account (Kite's net positions() call) —
    independent of this tool's own tracked Iron Condor / Strangle baskets in positions.json, and
    independent of which strategy or basket a leg originally came from. For each open NFO leg this
    returns the entry (average) price, live LTP, and running P&L reported by Kite itself, so the
    Order Management tab can show exactly what your account currently holds and let you price and
    fire an exit — for one leg or several at once — straight from here."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    try:
        pos = kite.positions()
        net = pos.get("net", [])
        rows = []
        open_rows = []
        for p in net:
            if p.get("exchange") != "NFO":
                continue
            qty = int(p.get("quantity") or 0)
            if qty == 0:
                continue  # already flat — nothing open on this tradingsymbol
            row = {
                "tradingsymbol": p.get("tradingsymbol"),
                "product": p.get("product"),
                "quantity": qty,
                "side": "LONG" if qty > 0 else "SHORT",
                "average_price": p.get("average_price"),
                "last_price": p.get("last_price"),
                "pnl": p.get("pnl"),
                "close_price": p.get("close_price"),
                "bid": None,
                "ask": None,
                "exit_price": None,
                "exit_price_basis": None,
            }
            rows.append(row)
            if p.get("tradingsymbol"):
                open_rows.append(row)

        # Fetch LIVE market depth for every open NFO leg.  Do not use the position
        # response's close_price as Bid/Ask: it is not the current executable quote.
        # A LONG position is closed with SELL at Bid; a SHORT position is closed
        # with BUY at Ask.  This is also what the frontend uses for the displayed
        # immediately-executable P&L.
        if open_rows:
            quote_keys = [f"NFO:{r['tradingsymbol']}" for r in open_rows]
            quotes = kite_quote_bulk(quote_keys, force_refresh=True)
            for r in open_rows:
                q = quotes.get(f"NFO:{r['tradingsymbol']}") or {}
                bid, ask = extract_bid_ask(q)
                r["bid"] = bid
                r["ask"] = ask
                if r["side"] == "LONG":
                    r["exit_price"] = bid
                    r["exit_price_basis"] = "BID"
                else:
                    r["exit_price"] = ask
                    r["exit_price_basis"] = "ASK"

        return jsonify({"positions": rows, "refreshed_at": now_ist().strftime("%H:%M:%S")})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/broker-positions/exit", methods=["POST"])
def broker_positions_exit():
    """Squares off one or more live Zerodha F&O positions directly by tradingsymbol — backs the
    Order Management tab's per-row 'Exit' button and the 'Exit Selected' multi-select action.
    Independent of this tool's own tracked baskets; works on whatever legs you pick, in whatever
    combination. A LONG position is squared off with a SELL, a SHORT position with a BUY. Each leg
    can optionally carry its own exit price (LIMIT) — legs left blank use MARKET. These legs are NOT
    run through the BUY-before-SELL basket sequencing (see place_basket_orders) since they're
    independent square-offs you chose yourself, not a hedged multi-leg entry."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    if not body.get("confirmed"):
        return jsonify({"error": "Confirmation flag not set — nothing was placed."}), 400
    legs = body.get("legs")
    if not legs:
        return jsonify({"error": "No legs provided."}), 400

    product = body.get("product", "NRML")

    legs_to_place = []
    for lg in legs:
        if not lg.get("tradingsymbol"):
            continue
        qty = abs(int(lg.get("quantity") or 0))
        if qty <= 0:
            continue
        side = str(lg.get("side", "LONG")).upper()
        close_txn = "SELL" if side == "LONG" else "BUY"
        price = lg.get("price")
        legs_to_place.append({
            "leg": lg["tradingsymbol"], "tradingsymbol": lg["tradingsymbol"],
            "transaction_type": close_txn, "quantity": qty,
            "price": float(price) if price not in (None, "") else None,
            "_original_side": side,
        })

    if not legs_to_place:
        return jsonify({"error": "No valid legs to place."}), 400

    # Zerodha market orders are not used by this exit path.  For every leg whose
    # custom price is blank, fetch a FRESH quote immediately before placement:
    #   LONG -> SELL at best Bid
    #   SHORT -> BUY at best Ask
    # These are marketable LIMIT orders and are intended to execute immediately
    # at the current executable side, subject to the quote still being available
    # when the order reaches the exchange.
    inst_keys = [f"NFO:{lg['tradingsymbol']}" for lg in legs_to_place]
    try:
        quotes = kite_quote_bulk(inst_keys, force_refresh=True)
    except Exception as e:
        return jsonify({"error": f"Could not fetch live Bid/Ask for exit: {e}"}), 502

    missing = []
    for lg in legs_to_place:
        if lg["price"] is not None:
            continue  # user explicitly supplied a custom LIMIT price
        q = quotes.get(f"NFO:{lg['tradingsymbol']}") or {}
        bid, ask = extract_bid_ask(q)
        auto_price = bid if lg["_original_side"] == "LONG" else ask
        if auto_price is None:
            missing.append(lg["tradingsymbol"])
        else:
            lg["price"] = auto_price

    if missing:
        return jsonify({
            "error": "Live Bid/Ask unavailable for: " + ", ".join(missing) +
                     ". No exit orders were placed. Refresh positions and try again."
        }), 502

    for lg in legs_to_place:
        lg.pop("_original_side", None)

    # Always LIMIT here.  Blank custom prices are automatically converted to the
    # correct marketable Bid/Ask price above.
    order_type = "LIMIT"
    results = place_basket_orders(legs_to_place, product, order_type, sequence_for_margin=False)
    return jsonify({
        "results": results,
        "order_type": order_type,
        "note": "Auto-priced exits use fresh Bid for LONG positions and fresh Ask for SHORT positions."
    })


@app.route("/api/broker/account")
def broker_account():
    if not require_session(): return jsonify({"error":"not_logged_in"}),401
    out={}
    try: out["profile"]=kite.profile()
    except Exception as e: out["profile_error"]=str(e)
    try: out["margins"]=kite.margins()
    except Exception as e: out["margins_error"]=str(e)
    try: out["holdings"]=kite.holdings()
    except Exception as e: out["holdings_error"]=str(e)
    try: out["positions"]=kite.positions()
    except Exception as e: out["positions_error"]=str(e)
    return jsonify(out)

@app.route("/api/quote/<path:instrument>")
def broker_quote(instrument):
    if not require_session(): return jsonify({"error":"not_logged_in"}),401
    try:
        key=instrument if ":" in instrument else f"NSE:{instrument.upper()}"
        return jsonify({"quote":kite_quote_bulk([key]).get(key)})
    except Exception as e: return jsonify({"error":str(e)}),400

@app.route("/api/trades")
def broker_trades():
    if not require_session(): return jsonify({"error":"not_logged_in"}),401
    try: return jsonify({"trades":kite.trades()})
    except Exception as e: return jsonify({"error":str(e)}),400

@app.route("/api/charges/orders", methods=["POST"])
def broker_charges_orders():
    if not require_session(): return jsonify({"error":"not_logged_in"}),401
    body=request.json or {}; orders=body.get("orders") or []
    if not orders: return jsonify({"error":"orders required"}),400
    try:
        if hasattr(kite,"order_charges"):
            return jsonify({"charges":kite.order_charges(orders)})
        return jsonify({"charges":None,"note":"Installed kiteconnect SDK does not expose order_charges; use estimate_charges locally."})
    except Exception as e: return jsonify({"error":str(e)}),400

@app.route("/api/gtt")
def gtt_list():
    if not require_session(): return jsonify({"error":"not_logged_in"}),401
    try: return jsonify({"triggers":kite.get_gtts()})
    except Exception as e: return jsonify({"error":str(e)}),400

@app.route("/api/gtt", methods=["POST"])
def gtt_create():
    if not require_session(): return jsonify({"error":"not_logged_in"}),401
    body=request.json or {}
    try: return jsonify({"trigger_id":kite.place_gtt(body["trigger_type"],body["condition"],body["orders"])})
    except Exception as e: return jsonify({"error":str(e)}),400

@app.route("/api/gtt/<int:trigger_id>", methods=["PUT","DELETE"])
def gtt_manage(trigger_id):
    if not require_session(): return jsonify({"error":"not_logged_in"}),401
    try:
        if request.method=="DELETE": return jsonify({"ok":kite.delete_gtt(trigger_id)})
        body=request.json or {}; return jsonify({"ok":kite.modify_gtt(trigger_id,body.get("condition"),body.get("orders"))})
    except Exception as e: return jsonify({"error":str(e)}),400

@app.route("/api/orders")
def list_orders():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    try:
        orders = kite.orders()
        return jsonify({"orders": orders})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/orders/<order_id>/cancel", methods=["POST"])
def cancel_order_route(order_id):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/orders/<order_id>/modify", methods=["POST"])
def modify_order_route(order_id):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    try:
        kwargs = {"variety": kite.VARIETY_REGULAR, "order_id": order_id}
        if body.get("quantity"):
            kwargs["quantity"] = int(body["quantity"])
        if body.get("price"):
            kwargs["price"] = float(body["price"])
        if body.get("order_type"):
            kwargs["order_type"] = getattr(kite, f"ORDER_TYPE_{body['order_type']}")
        kite.modify_order(**kwargs)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# Price chart
# ---------------------------------------------------------------------------
INTERVAL_MAX_DAYS = {
    "5minute": 30, "15minute": 30, "60minute": 90, "day": 720,
}


@app.route("/api/chart/<symbol>")
def chart(symbol):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    symbol = symbol.upper()
    interval = request.args.get("interval", "day")
    if interval not in INTERVAL_MAX_DAYS:
        return jsonify({"error": f"Unsupported interval '{interval}'. Use one of: {', '.join(INTERVAL_MAX_DAYS)}"}), 400

    if symbol in INDEX_SYMBOLS:
        _, nse = get_instruments()
        token = None
        wanted = INDEX_SYMBOLS[symbol].split(":")[1]
        for i in nse:
            if i["segment"] == "INDICES" and i["tradingsymbol"] == wanted:
                token = i["instrument_token"]
                break
        if not token:
            return jsonify({"error": f"Could not resolve chart instrument token for {symbol}"}), 404
    else:
        _, nse = get_instruments()
        nse_match = [i for i in nse if i["exchange"] == "NSE" and i["tradingsymbol"] == symbol]
        if not nse_match:
            return jsonify({"error": f"{symbol} not found on NSE"}), 404
        token = nse_match[0]["instrument_token"]

    requested_days = int(request.args.get("days", 60))
    max_days = INTERVAL_MAX_DAYS[interval]
    days = min(requested_days, max_days)
    clamped = requested_days > max_days

    to_date = now_ist()
    from_date = to_date - timedelta(days=days + (15 if interval == "day" else 5))
    candles = kite.historical_data(token, from_date, to_date, interval)

    if interval != "day":
        # The window above is padded (weekends/holidays could otherwise leave fewer trading
        # sessions than requested), so it can return MORE trading days than `days` asked for.
        # Trim down to exactly the most recent `days` trading days so a short lookback (e.g. 1
        # day) doesn't silently keep showing extra earlier sessions -- which is what made the
        # chart look like it "wasn't updating" when you changed the lookback control.
        by_day = {}
        for c in candles:
            d = c["date"]
            key = d.date() if hasattr(d, "date") else str(d)[:10]
            by_day.setdefault(key, []).append(c)
        wanted_days = sorted(by_day.keys())[-days:]
        candles = [c for day_key in wanted_days for c in by_day[day_key]]

    def fmt_date(c):
        d = c["date"]
        return d.isoformat() if hasattr(d, "isoformat") else str(d)

    return jsonify({
        "symbol": symbol, "interval": interval, "clamped_to_days": days if clamped else None,
        "candles": [{"t": fmt_date(c), "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]} for c in candles],
    })


# ---------------------------------------------------------------------------
# News / event-risk headlines
# ---------------------------------------------------------------------------
def _get_headlines_best_effort(symbol, max_items=3):
    """Shared by the screener (top-N picks) and /api/news/<symbol>. Best-effort keyword scan
    of public RSS feeds — NOT sentiment analysis, NOT a verified event-risk signal. Returns
    (list_of_headline_dicts, error_string_or_None)."""
    symbol = symbol.upper()
    sources = [
        ("Google News",
         f"https://news.google.com/rss/search?q={requests.utils.quote(symbol + ' NSE share')}&hl=en-IN&gl=IN&ceid=IN:en"),
        ("Yahoo Finance",
         f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}.NS&region=IN&lang=en-IN"),
    ]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
               "Accept": "application/rss+xml, application/xml, text/xml, */*"}
    errors = []
    ssl_issue_seen = False

    for name, url in sources:
        try:
            headlines = _fetch_rss(url, headers, verify=True)
            if headlines:
                return headlines[:max_items], None
            errors.append(f"{name} returned no items")
        except requests.exceptions.SSLError:
            ssl_issue_seen = True
            errors.append(f"{name}: SSL certificate verification failed")
            if ALLOW_INSECURE_NEWS:
                try:
                    headlines = _fetch_rss(url, headers, verify=False)
                    if headlines:
                        return headlines[:max_items], None
                except Exception as e2:
                    errors.append(f"{name} (insecure retry) also failed: {e2}")
        except Exception as e:
            errors.append(f"{name} failed: {e}")

    guidance = ""
    if ssl_issue_seen:
        guidance = (" This looks like a network TLS-interception issue (corporate/government firewall) "
                     "rather than a real absence of news — see /api/news/<symbol> for the full explanation.")
    return [], "Could not fetch headlines. " + " | ".join(errors) + guidance


def _fetch_rss(url, headers, verify=True, timeout=10):
    resp = requests.get(url, headers=headers, timeout=timeout, verify=verify)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = root.findall(".//item")[:5]
    headlines = []
    for it in items:
        headlines.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "pub_date": (it.findtext("pubDate") or "").strip(),
        })
    return headlines


@app.route("/api/news/<symbol>")
def news(symbol):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    symbol = symbol.upper()
    headlines, error = _get_headlines_best_effort(symbol, max_items=5)
    if headlines:
        return jsonify({"symbol": symbol, "headlines": headlines,
                         "note": "Best-effort headline scan, not verified analysis. "
                                 "Read the actual articles before treating this as an event-risk signal."})

    guidance = ""
    if error and "SSL certificate verification failed" in error:
        guidance = (" This looks like your network (office/government firewall, antivirus, or a proxy) is "
                     "intercepting HTTPS traffic with its own certificate — common on corporate/government "
                     "networks. Kite API calls aren't affected since those go through Kite's own SDK. To fix "
                     "properly: ask your IT team for the organization's root CA certificate and set it via the "
                     "REQUESTS_CA_BUNDLE environment variable. As a quick workaround for this headlines feature "
                     "only (not recommended on untrusted networks), you can set ALLOW_INSECURE_NEWS=true as an "
                     "environment variable before running backend.py.")
    return jsonify({"symbol": symbol, "headlines": [],
                     "error": "Could not fetch news from any source. " + (error or "") + guidance})


# ---------------------------------------------------------------------------
def _ema(values, period):
    """Simple exponential moving average over `values` (oldest-first), seeded with the first value."""
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    ema = float(values[0])
    for v in values[1:]:
        ema = alpha * float(v) + (1 - alpha) * ema
    return ema
def _rsi(closes, period=14):
    """Wilder-style RSI (simple-average variant) over the trailing `period` bars."""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain, avg_loss = float(np.mean(gains)), float(np.mean(losses))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))
# Dynamic Delta-Neutral Adjustment Engine — ADD-ON to the existing Iron Condor / Hedged Short
# Strangle strategy above. Never touches how a position is opened (build_strategy, execute_confirm
# are untouched); it only watches already-open positions you explicitly attach to it and proposes/
# executes adjustments once net delta drifts too far. See greeks.py / risk_management.py /
# adjustment_engine.py / execution.py / delta_neutral_logging.py / backtesting.py /
# delta_neutral_state.py for the individual modules this wires together.
# =============================================================================
DELTA_POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "delta_engine_positions.json")
DELTA_LOG_FILE = os.path.join(os.path.dirname(__file__), "delta_engine_log.jsonl")
_delta_positions_lock = threading.Lock()

_delta_greeks_engine = PortfolioGreeksEngine(risk_free_rate=RISK_FREE_RATE)
_delta_logger = AdjustmentLogger(DELTA_LOG_FILE)

# Tick-level spot cache: refreshed by a dedicated 1-second-cadence thread per configured symbol so
# the "monitor spot every tick" requirement doesn't depend on the heavier option-chain refresh rate.
# A true broker websocket (KiteTicker) tick feed is a straightforward future upgrade of just this
# cache's refresh mechanism -- everything downstream reads from this dict, not from the network call
# directly, so swapping REST polling for a websocket callback later doesn't touch any other module.
_delta_spot_cache = {}
_delta_spot_cache_lock = threading.Lock()

SPOT_INDEX_TRADINGSYMBOL = {
    "NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE", "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
}


def load_delta_positions():
    if not os.path.exists(DELTA_POSITIONS_FILE):
        return []
    try:
        with open(DELTA_POSITIONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_delta_positions(positions):
    with _delta_positions_lock:
        with open(DELTA_POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2, default=str)


def _delta_spot_poll_loop():
    """Refreshes _delta_spot_cache at dn_state's spot_poll_seconds cadence (default 1s) for every
    symbol currently being monitored -- this is the tick-level spot watch. Cheap: one LTP call per
    symbol, not the full option chain."""
    while True:
        try:
            state = dn_load_state()
            if state.get("enabled"):
                monitored_symbols = {p["symbol"] for p in load_delta_positions() if p["status"] == "monitoring"}
                for sym in monitored_symbols:
                    inst = SPOT_INDEX_TRADINGSYMBOL.get(sym)
                    if not inst:
                        continue
                    try:
                        ltp_data = kite.ltp([inst])
                        price = ltp_data.get(inst, {}).get("last_price")
                        if price:
                            with _delta_spot_cache_lock:
                                _delta_spot_cache[sym] = price
                    except Exception:
                        pass
            time.sleep(max(state.get("spot_poll_seconds", 1), 1))
        except Exception:
            time.sleep(2)


def _make_delta_candidate_fetcher(position, chain_data, state):
    """Returns a candidate_fetcher closure bound to one already-fetched live option chain snapshot
    (avoids re-hitting the API once per candidate). Builds real, tradable roll/hedge candidates —
    see adjustment_engine.py's module docstring for the up/down scenario -> threatened-side mapping."""
    calls = sorted([o for o in chain_data["chain"] if o["instrument_type"] == "CE"], key=lambda x: x["strike"])
    puts = sorted([o for o in chain_data["chain"] if o["instrument_type"] == "PE"], key=lambda x: x["strike"])
    legs = position["legs"]
    qty = position["quantity"]
    low, high = state.get("delta_range_low", 0.15), state.get("delta_range_high", 0.20)
    mid_target = (low + high) / 2
    hedge_pct = state.get("hedge_distance_pct", 2.5) / 100.0
    min_premium = state.get("min_premium_for_adjustment", 8.0)

    def find_by_delta(options, target, sign):
        return min(options, key=lambda o: abs(o["delta"] - sign * target)) if options else None

    def leg_dict(o, role):
        return {"tradingsymbol": o["tradingsymbol"], "strike": o["strike"], "role": role,
                "quantity": -qty if role.startswith("sell") else qty, "ltp": o["ltp"], "delta": o["delta"]}

    def close_leg(role):
        return {"tradingsymbol": legs[role]["tradingsymbol"], "role": role, "quantity": -qty}

    def fetcher(scenario, pos, portfolio_greeks):
        candidates = []
        if scenario == "up":
            # Priority 1: roll the short PUT upward -- collects more premium AND adds offsetting
            # positive delta (see adjustment_engine.py docstring for why this offsets negative net delta).
            cur_put_k = legs["sell_put"]["strike"]
            pool = [o for o in puts if o["strike"] > cur_put_k]
            new_put = find_by_delta(pool, mid_target, -1)
            if new_put and new_put["ltp"] >= min_premium:
                delta_gain = abs(new_put["delta"] - legs["sell_put"]["delta"]) * qty
                candidates.append(dict(
                    action="roll_put_up",
                    description=f"Roll short put {cur_put_k} -> {new_put['strike']} (more premium, offsetting positive delta)",
                    legs_to_close=[close_leg("sell_put")], legs_to_open=[leg_dict(new_put, "sell_put")],
                    expected_delta_after=portfolio_greeks.net_delta + delta_gain,
                    additional_premium=(new_put["ltp"] - legs["sell_put"]["ltp"]) * qty,
                    margin_impact=0.0, risk_reduction_score=min(1.0, delta_gain / max(state.get("delta_threshold", 10.0), 1)),
                    probability_of_profit=0.60, expected_drawdown=abs(new_put["strike"] - cur_put_k) * qty * 0.3,
                ))
            # Priority 2: roll the short CALL further OTM -- directly cuts the threatened leg's delta.
            cur_call_k = legs["sell_call"]["strike"]
            pool = [o for o in calls if o["strike"] > cur_call_k]
            new_call = find_by_delta(pool, mid_target, +1)
            if new_call:
                delta_gain = abs(new_call["delta"] - legs["sell_call"]["delta"]) * qty
                candidates.append(dict(
                    action="roll_call_further_otm",
                    description=f"Roll short call {cur_call_k} -> {new_call['strike']} further OTM (cuts delta exposure directly)",
                    legs_to_close=[close_leg("sell_call")], legs_to_open=[leg_dict(new_call, "sell_call")],
                    expected_delta_after=portfolio_greeks.net_delta + delta_gain,
                    additional_premium=(new_call["ltp"] - legs["sell_call"]["ltp"]) * qty,
                    margin_impact=0.0, risk_reduction_score=min(1.0, delta_gain / max(state.get("delta_threshold", 10.0), 1)),
                    probability_of_profit=0.55, expected_drawdown=abs(new_call["strike"] - cur_call_k) * qty * 0.3,
                ))
        else:  # "down" -- mirror image, put side under stress
            cur_call_k = legs["sell_call"]["strike"]
            pool = [o for o in calls if o["strike"] < cur_call_k]
            new_call = find_by_delta(pool, mid_target, +1)
            if new_call and new_call["ltp"] >= min_premium:
                delta_gain = abs(new_call["delta"] - legs["sell_call"]["delta"]) * qty
                candidates.append(dict(
                    action="roll_call_down",
                    description=f"Roll short call {cur_call_k} -> {new_call['strike']} (more premium, offsetting negative delta)",
                    legs_to_close=[close_leg("sell_call")], legs_to_open=[leg_dict(new_call, "sell_call")],
                    expected_delta_after=portfolio_greeks.net_delta - delta_gain,
                    additional_premium=(new_call["ltp"] - legs["sell_call"]["ltp"]) * qty,
                    margin_impact=0.0, risk_reduction_score=min(1.0, delta_gain / max(state.get("delta_threshold", 10.0), 1)),
                    probability_of_profit=0.60, expected_drawdown=abs(new_call["strike"] - cur_call_k) * qty * 0.3,
                ))
            cur_put_k = legs["sell_put"]["strike"]
            pool = [o for o in puts if o["strike"] < cur_put_k]
            new_put = find_by_delta(pool, mid_target, -1)
            if new_put:
                delta_gain = abs(new_put["delta"] - legs["sell_put"]["delta"]) * qty
                candidates.append(dict(
                    action="roll_put_further_otm",
                    description=f"Roll short put {cur_put_k} -> {new_put['strike']} further OTM (cuts delta exposure directly)",
                    legs_to_close=[close_leg("sell_put")], legs_to_open=[leg_dict(new_put, "sell_put")],
                    expected_delta_after=portfolio_greeks.net_delta - delta_gain,
                    additional_premium=(new_put["ltp"] - legs["sell_put"]["ltp"]) * qty,
                    margin_impact=0.0, risk_reduction_score=min(1.0, delta_gain / max(state.get("delta_threshold", 10.0), 1)),
                    probability_of_profit=0.55, expected_drawdown=abs(new_put["strike"] - cur_put_k) * qty * 0.3,
                ))

        # Convert to Iron Fly: only meaningful for an existing iron_condor (hedges already in place) --
        # rolls BOTH short strikes to ATM for extra premium in exchange for a much tighter profit zone.
        if position.get("strategy_type") == "iron_condor" and calls and puts:
            atm_call = min(calls, key=lambda o: abs(o["strike"] - portfolio_greeks.spot))
            atm_put = min(puts, key=lambda o: abs(o["strike"] - portfolio_greeks.spot))
            added_premium = ((atm_call["ltp"] - legs["sell_call"]["ltp"]) + (atm_put["ltp"] - legs["sell_put"]["ltp"])) * qty
            candidates.append(dict(
                action="convert_iron_fly",
                description=f"Convert to Iron Fly: roll both shorts to ATM ({atm_call['strike']}/{atm_put['strike']}) for extra premium, tighter body",
                legs_to_close=[close_leg("sell_call"), close_leg("sell_put")],
                legs_to_open=[leg_dict(atm_call, "sell_call"), leg_dict(atm_put, "sell_put")],
                expected_delta_after=(atm_call["delta"] + atm_put["delta"]) * qty,
                additional_premium=added_premium, margin_impact=2000.0, risk_reduction_score=0.9,
                probability_of_profit=0.45,   # tighter body -> lower POP even though delta-neutral
                expected_drawdown=abs(portfolio_greeks.spot - legs["sell_call"]["strike"]) * qty * 0.2,
            ))

        # Add-hedge fallback: only meaningful for a naked strangle (an iron condor already carries
        # hedges) -- buys a protective option on the threatened side to hard-cap runaway delta/risk.
        if position.get("strategy_type") == "naked_strangle":
            if scenario == "up" and calls:
                target_k = legs["sell_call"]["strike"] * (1 + hedge_pct)
                hedge_opt = min(calls, key=lambda o: abs(o["strike"] - target_k))
                candidates.append(dict(
                    action="add_hedge",
                    description=f"Buy protective call {hedge_opt['strike']} ({hedge_pct*100:.1f}% OTM of short call) to cap runaway risk",
                    legs_to_close=[], legs_to_open=[leg_dict(hedge_opt, "buy_call")],
                    expected_delta_after=portfolio_greeks.net_delta + abs(hedge_opt["delta"]) * qty,
                    additional_premium=-hedge_opt["ltp"] * qty, margin_impact=-5000.0,
                    risk_reduction_score=0.8, probability_of_profit=0.5, expected_drawdown=hedge_opt["ltp"] * qty,
                ))
            elif scenario == "down" and puts:
                target_k = legs["sell_put"]["strike"] * (1 - hedge_pct)
                hedge_opt = min(puts, key=lambda o: abs(o["strike"] - target_k))
                candidates.append(dict(
                    action="add_hedge",
                    description=f"Buy protective put {hedge_opt['strike']} ({hedge_pct*100:.1f}% OTM of short put) to cap runaway risk",
                    legs_to_close=[], legs_to_open=[leg_dict(hedge_opt, "buy_put")],
                    expected_delta_after=portfolio_greeks.net_delta - abs(hedge_opt["delta"]) * qty,
                    additional_premium=-hedge_opt["ltp"] * qty, margin_impact=-5000.0,
                    risk_reduction_score=0.8, probability_of_profit=0.5, expected_drawdown=hedge_opt["ltp"] * qty,
                ))
        return candidates
    return fetcher


def _delta_position_portfolio_greeks(position, chain_data, spot):
    """Builds a PortfolioGreeks snapshot for one monitored position from a fresh chain fetch."""
    chain_by_symbol = {o["tradingsymbol"]: o for o in chain_data["chain"]}
    T = chain_data["T"]
    qty = position["quantity"]
    leg_inputs = []
    for role, leg in position["legs"].items():
        live = chain_by_symbol.get(leg["tradingsymbol"])
        ltp = live["ltp"] if live else leg["ltp"]
        iv = (live["iv"] / 100.0) if live else 0.15
        opt_type = "CE" if "call" in role else "PE"
        leg_inputs.append({
            "opt_type": opt_type, "strike": leg["strike"], "spot": spot, "T": T, "iv": iv, "ltp": ltp,
            "quantity": -qty if role.startswith("sell") else qty, "role": role,
            "tradingsymbol": leg["tradingsymbol"], "entry_premium": leg["ltp"],
        })
    return _delta_greeks_engine.portfolio_greeks(position["symbol"], spot, leg_inputs)


def _execute_delta_adjustment(position, candidate, trigger_reason, all_candidate_actions=None):
    """Executes ONE specific adjustment candidate against one position -- shared by the manual
    /api/delta-engine/suggestions/<id>/execute route (the only normal path now) and reusable for any
    future automation. Risk gates are re-checked fresh here regardless of what was true when the
    suggestion was first generated, so a stale approval can never bypass current limits."""
    state = dn_load_state()
    symbol = position["symbol"]
    adj_by_symbol = state.get("adjustments_today_by_symbol", {})
    mtm_by_symbol = state.get("daily_mtm_by_symbol", {})
    paused_symbols = set(state.get("paused_symbols_today", []))
    if symbol in paused_symbols:
        return False, f"{symbol} is paused for today (its own daily-loss limit tripped) -- adjustment blocked"

    risk_mgr = RiskManager(RiskLimits(
        max_adjustments_per_day=state.get("max_adjustments_per_day", 6),
        max_loss_per_position=state.get("max_loss_per_position", 15000.0),
        max_daily_mtm_loss=state.get("max_daily_mtm_loss", 25000.0),
        min_premium_for_adjustment=state.get("min_premium_for_adjustment", 8.0),
        profit_targets_pct=tuple(state.get("profit_targets_pct", [25, 50, 70, 90])),
        stop_loss_pct=state.get("stop_loss_pct", 200.0),
    ))
    position_mtm = (position.get("last_greeks") or {}).get("mtm", 0.0)
    proposed_premium = abs(candidate.legs_to_open[0]["ltp"]) if candidate.legs_to_open else None
    allowed, gate_reason = risk_mgr.can_adjust(
        adjustments_today=adj_by_symbol.get(symbol, 0), position_mtm=position_mtm,
        daily_mtm=mtm_by_symbol.get(symbol, 0.0), proposed_leg_premium=proposed_premium)
    if not allowed:
        return False, gate_reason

    executor = AdjustmentExecutor(place_basket_orders, product="NRML")
    result = executor.execute(candidate, execution_mode=state.get("execution_mode", "track"))
    if not result.ok:
        return False, f"Execution failed: {result.error}"

    for opened in candidate.legs_to_open:
        position["legs"][opened["role"]] = {
            "strike": opened["strike"], "ltp": opened["ltp"], "tradingsymbol": opened["tradingsymbol"],
        }
    position["pending_suggestion"] = None
    adj_by_symbol[symbol] = adj_by_symbol.get(symbol, 0) + 1
    state["adjustments_today_by_symbol"] = adj_by_symbol
    dn_save_state(state)
    _delta_logger.log_adjustment(
        ts=now_ist().isoformat(), symbol=symbol, spot=position.get("last_greeks", {}).get("spot"),
        delta_before=position.get("last_greeks", {}).get("net_delta", 0.0),
        delta_after=candidate.expected_delta_after, action=candidate.action,
        premium_collected=candidate.additional_premium, reason=trigger_reason,
        execution_mode=state.get("execution_mode", "track"),
        candidates_considered=all_candidate_actions or [candidate.action])
    return True, "Executed"


def _close_delta_position_legs(position, roles, reason):
    """Closes some or all legs of a monitored position on demand -- independent of the automatic
    profit-target/stop-loss logic, so you can act on your own judgment any time, in Track or Live."""
    state = dn_load_state()
    legs = position["legs"]
    roles = roles or list(legs.keys())
    close_legs = []
    for role in roles:
        if role not in legs:
            continue
        close_legs.append({"tradingsymbol": legs[role]["tradingsymbol"], "role": role,
                            "quantity": -position["quantity"] if role.startswith("sell") else position["quantity"]})
    if not close_legs:
        return False, "No matching open legs found to close"
    candidate = AdjustmentCandidate(action="manual_close", description=reason, legs_to_close=close_legs)
    executor = AdjustmentExecutor(place_basket_orders, product="NRML")
    result = executor.execute(candidate, execution_mode=state.get("execution_mode", "track"))
    if not result.ok:
        return False, f"Close failed: {result.error}"
    for role in roles:
        legs.pop(role, None)
    pnl_closed = (position.get("last_greeks") or {}).get("mtm", 0.0) if not legs else None
    if not legs:
        position["status"] = "closed"
        position["closed_at"] = now_ist().isoformat()
        position["exit_reason"] = reason
        position["realized_pnl"] = pnl_closed
        position["pending_suggestion"] = None
    _delta_logger.log_adjustment(
        ts=now_ist().isoformat(), symbol=position["symbol"], spot=(position.get("last_greeks") or {}).get("spot"),
        delta_before=(position.get("last_greeks") or {}).get("net_delta", 0.0), delta_after=0.0,
        action="manual_close", premium_collected=0.0, reason=reason,
        execution_mode=state.get("execution_mode", "track"), candidates_considered=[])
    return True, "Closed"


def _delta_engine_loop():
    """Background monitor loop for the Delta Neutral Adjustment Engine -- disarmed by default, only
    watches/acts on positions you've explicitly attached (via the Condor/Strangle builder's Position
    ID, or directly from your live broker positions). Recomputes portfolio Greeks and evaluates the
    adjustment threshold at greeks_poll_seconds cadence (default 5s); the underlying spot itself is
    refreshed far more often by _delta_spot_poll_loop (default 1s).

    IMPORTANT: this loop only ever ANALYZES and SUGGESTS. When a delta breach is detected it scores
    every candidate adjustment (probability of profit, risk reduction, expected extra premium, etc.)
    and stores them as position["pending_suggestion"] -- it does NOT execute anything automatically.
    You review the suggestion in the dashboard and choose whether/which one to execute, in Track or
    Live, via /api/delta-engine/suggestions/<id>/execute. The one thing that DOES still happen
    automatically is closing a position on a configured profit target or stop loss being hit -- that
    is risk protection, not a trading decision, so it isn't gated behind manual approval.

    Every symbol is analyzed and decided on INDEPENDENTLY: its own Greeks, its own delta-threshold
    check, its own adjustments-per-day / daily-loss budget. A breach on one symbol only pauses NEW
    adjustments for that specific symbol for the rest of the day; profit-target/stop-loss exits keep
    working per-position regardless of pause state."""
    while True:
        try:
            state = dn_load_state()
            today_str = now_ist().strftime("%Y-%m-%d")
            if state.get("day") != today_str:
                state["day"] = today_str
                state["adjustments_today_by_symbol"] = {}
                state["daily_mtm_by_symbol"] = {}
                state["paused_symbols_today"] = []
                dn_save_state(state)

            if not state.get("enabled"):
                time.sleep(2)
                continue

            adj_by_symbol = state.get("adjustments_today_by_symbol", {})
            mtm_by_symbol = state.get("daily_mtm_by_symbol", {})
            paused_symbols = set(state.get("paused_symbols_today", []))

            risk_mgr = RiskManager(RiskLimits(
                max_adjustments_per_day=state.get("max_adjustments_per_day", 6),
                max_loss_per_position=state.get("max_loss_per_position", 15000.0),
                max_daily_mtm_loss=state.get("max_daily_mtm_loss", 25000.0),
                min_premium_for_adjustment=state.get("min_premium_for_adjustment", 8.0),
                profit_targets_pct=tuple(state.get("profit_targets_pct", [25, 50, 70, 90])),
                stop_loss_pct=state.get("stop_loss_pct", 200.0),
            ))
            engine = AdjustmentEngine(delta_threshold=state.get("delta_threshold", 10.0),
                                       gamma_threshold=state.get("gamma_threshold"))
            executor = AdjustmentExecutor(place_basket_orders, product="NRML")

            positions = load_delta_positions()
            for position in positions:
                if position["status"] != "monitoring":
                    continue
                symbol = position["symbol"]
                try:
                    chain_data, err = get_chain_for_symbol(symbol)
                    if err:
                        state["last_error"] = f"{symbol}: {err.get('error')}"
                        continue
                    with _delta_spot_cache_lock:
                        spot = _delta_spot_cache.get(symbol, chain_data["spot"])
                    pg = _delta_position_portfolio_greeks(position, chain_data, spot)
                    position["last_greeks"] = pg.as_dict()
                    # Accumulate THIS symbol's daily MTM only -- separate bucket per symbol, never
                    # pooled with any other symbol's positions.
                    mtm_by_symbol[symbol] = round(mtm_by_symbol.get(symbol, 0.0) + pg.mtm, 2)

                    # Profit target / stop loss -- the ONE thing that stays automatic (risk
                    # protection, not a discretionary trading decision). Exit all legs immediately.
                    hit_target = risk_mgr.profit_target_hit(position.get("entry_credit", 0.0), pg.mtm)
                    hit_sl = risk_mgr.stop_loss_hit(position.get("entry_credit", 0.0), pg.mtm)
                    if hit_target or hit_sl:
                        reason = f"Profit target {hit_target}% reached" if hit_target else f"Stop loss ({state.get('stop_loss_pct')}%) hit"
                        close_legs = [{"tradingsymbol": leg["tradingsymbol"], "role": role,
                                       "quantity": -position["quantity"] if role.startswith("sell") else position["quantity"]}
                                      for role, leg in position["legs"].items()]
                        close_candidate = AdjustmentCandidate(action="close_all", description=reason,
                                                               legs_to_close=close_legs)
                        executor.execute(close_candidate, execution_mode=state.get("execution_mode", "track"))
                        position["status"] = "closed"
                        position["closed_at"] = now_ist().isoformat()
                        position["exit_reason"] = reason
                        position["realized_pnl"] = pg.mtm
                        position["pending_suggestion"] = None
                        _delta_logger.log_adjustment(ts=now_ist().isoformat(), symbol=symbol,
                            spot=spot, delta_before=pg.net_delta, delta_after=0.0, action="close_all",
                            premium_collected=pg.mtm, reason=reason, execution_mode=state.get("execution_mode", "track"))
                        continue

                    # This symbol's OWN daily-loss budget -- checked independently of every other symbol.
                    if risk_mgr.breached_daily_loss(mtm_by_symbol[symbol]) and symbol not in paused_symbols:
                        paused_symbols.add(symbol)
                        state["last_error"] = f"{symbol}: daily MTM loss (Rs {mtm_by_symbol[symbol]:.0f}) breached its own limit -- new adjustments paused for {symbol} only for the rest of today"

                    recommended, all_candidates, trigger_reason = engine.recommend(
                        position, pg, _make_delta_candidate_fetcher(position, chain_data, state))
                    state["last_recommendation"] = {
                        "symbol": symbol, "trigger_reason": trigger_reason,
                        "recommended": vars(recommended) if recommended else None,
                        "at": now_ist().isoformat(),
                    }
                    if recommended is None or recommended.action == "no_action":
                        position["pending_suggestion"] = None
                        continue

                    # Annotate each candidate with whether it's currently executable, so the
                    # dashboard can show "blocked: <reason>" without you having to click Execute
                    # to find out -- but the ACTUAL gate is always re-checked fresh at execute time.
                    annotated = []
                    for c in all_candidates:
                        proposed_premium = abs(c.legs_to_open[0]["ltp"]) if c.legs_to_open else None
                        ok, reason_txt = risk_mgr.can_adjust(
                            adjustments_today=adj_by_symbol.get(symbol, 0), position_mtm=pg.mtm,
                            daily_mtm=mtm_by_symbol[symbol], proposed_leg_premium=proposed_premium)
                        if symbol in paused_symbols:
                            ok, reason_txt = False, f"{symbol} paused today (daily-loss limit)"
                        cd = vars(c)
                        cd["executable"] = ok
                        cd["block_reason"] = None if ok else reason_txt
                        annotated.append(cd)

                    position["pending_suggestion"] = {
                        "trigger_reason": trigger_reason, "detected_at": now_ist().isoformat(),
                        "candidates": annotated,
                    }
                except Exception as e:
                    state["last_error"] = f"{symbol}: {e}"

            state["adjustments_today_by_symbol"] = adj_by_symbol
            state["daily_mtm_by_symbol"] = mtm_by_symbol
            state["paused_symbols_today"] = sorted(paused_symbols)
            state["last_scan_at"] = now_ist().isoformat()

            save_delta_positions(positions)
            dn_save_state(state)
        except Exception as e:
            try:
                state = dn_load_state()
                state["last_error"] = str(e)
                dn_save_state(state)
            except Exception:
                pass
        time.sleep(max(dn_load_state().get("greeks_poll_seconds", 5), 2))


# --- Delta Neutral Engine API routes ---
@app.route("/api/delta-engine/state")
def delta_engine_state():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    return jsonify(dn_load_state())


@app.route("/api/delta-engine/config", methods=["POST"])
def delta_engine_config():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    state = dn_load_state()
    for key in DELTA_ENGINE_CONFIGURABLE_KEYS:
        if key in body:
            state[key] = body[key]
    dn_save_state(state)
    return jsonify({"ok": True, "state": state})


@app.route("/api/delta-engine/set-execution-mode", methods=["POST"])
def delta_engine_set_execution_mode():
    """Acknowledgement-gated mode selection -- kept OUT of the bulk config
    route so a stray Settings save can never silently switch this to placing real orders."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    mode = body.get("mode")
    if mode not in ("live", "track"):
        return jsonify({"error": "mode must be 'live' or 'track'"}), 400
    if mode == "live" and body.get("ack") is not True:
        return jsonify({"error": "Switching to Live mode requires explicit confirmation (ack: true) "
                                  "that future adjustments will place REAL orders on your live Zerodha account."}), 400
    state = dn_load_state()
    state["execution_mode"] = mode
    dn_save_state(state)
    return jsonify({"ok": True, "state": state})


@app.route("/api/delta-engine/arm", methods=["POST"])
def delta_engine_arm():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    state = dn_load_state()
    state["enabled"], state["disarm_reason"] = True, None
    dn_save_state(state)
    return jsonify({"ok": True, "state": state})


@app.route("/api/delta-engine/disarm", methods=["POST"])
def delta_engine_disarm():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    state = dn_load_state()
    state["enabled"] = False
    dn_save_state(state)
    return jsonify({"ok": True, "state": state})


@app.route("/api/delta-engine/track/<pos_id>", methods=["POST"])
def delta_engine_track(pos_id):
    """Attaches an already-built Iron Condor / Strangle position (from positions.json, built via the
    existing /api/strategy endpoint) to the Delta Neutral Engine for monitoring. Does not place any
    order itself -- the position must already be a real (or, in execution_mode="track", intended)
    open position."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    position = find_position(pos_id)
    if not position:
        return jsonify({"error": "Position not found"}), 404
    if position.get("strategy_type") not in ("iron_condor", "naked_strangle"):
        return jsonify({"error": "Only Iron Condor / Naked Strangle positions are supported by the Delta Neutral Engine"}), 400
    legs = position["legs"]
    credit_per_share = sum(v["ltp"] for k, v in legs.items() if k.startswith("sell")) \
        - sum(v["ltp"] for k, v in legs.items() if k.startswith("buy"))
    tracked = {
        "id": pos_id, "symbol": position["symbol"], "strategy_type": position["strategy_type"],
        "legs": {k: {"strike": v["strike"], "ltp": v["ltp"], "tradingsymbol": v["tradingsymbol"]} for k, v in legs.items()},
        "quantity": position.get("quantity", position["lot_size"]), "lot_size": position["lot_size"],
        "entry_credit": round(credit_per_share * position.get("quantity", position["lot_size"]), 2),
        "status": "monitoring", "added_at": now_ist().isoformat(), "closed_at": None,
        "exit_reason": None, "realized_pnl": None, "last_greeks": None, "pending_suggestion": None,
    }
    positions = load_delta_positions()
    positions = [p for p in positions if p["id"] != pos_id]
    positions.append(tracked)
    save_delta_positions(positions)
    return jsonify({"ok": True, "tracked": tracked})


def _classify_broker_legs_for_symbol(symbol):
    """Finds every open NFO option leg under a given underlying (e.g. "SBIN") straight from your
    live Zerodha positions (the same data as the "Open F&O positions" table), and classifies each
    one as sell_call / buy_call / sell_put / buy_put using Kite's own authoritative instrument dump
    (strike/instrument_type per tradingsymbol) rather than guessing from the tradingsymbol text.
    This is what lets you attach a position you opened directly in Zerodha (or anywhere else) --
    no positions.json entry from this app's own Condor/Strangle builder is required."""
    symbol = symbol.upper()
    nfo, _ = get_instruments()
    meta_by_symbol = {i["tradingsymbol"]: i for i in nfo if i.get("name") == symbol and i.get("segment") == "NFO-OPT"}
    pos = kite.positions()
    net = pos.get("net", [])
    legs_by_role = {}
    quantity = None
    lot_size = None
    for p in net:
        ts = p.get("tradingsymbol")
        if p.get("exchange") != "NFO" or ts not in meta_by_symbol:
            continue
        qty = int(p.get("quantity") or 0)
        if qty == 0:
            continue
        meta = meta_by_symbol[ts]
        opt_type = meta.get("instrument_type")
        role = ("sell_" if qty < 0 else "buy_") + ("call" if opt_type == "CE" else "put")
        if role in legs_by_role:
            return None, (f"Found more than one open {role.replace('_', ' ')} leg on {symbol} -- "
                           f"this engine expects one clean 4-leg (or 2-leg) condor/strangle shape per "
                           f"symbol. Close/simplify down to one leg per role before attaching.")
        legs_by_role[role] = {
            "strike": meta.get("strike"), "tradingsymbol": ts,
            "ltp": p.get("last_price"), "entry_price": p.get("average_price"),
        }
        quantity = abs(qty)
        lot_size = meta.get("lot_size")
    if not legs_by_role or "sell_call" not in legs_by_role or "sell_put" not in legs_by_role:
        return None, (f"No open short call + short put pair found for {symbol} in your live Zerodha "
                       f"positions. This engine needs at least both short legs of the condor/strangle "
                       f"to be open right now.")
    strategy_type = "iron_condor" if {"buy_call", "buy_put"} <= legs_by_role.keys() else "naked_strangle"
    return {"symbol": symbol, "strategy_type": strategy_type, "legs": legs_by_role,
            "quantity": quantity, "lot_size": lot_size}, None


@app.route("/api/delta-engine/broker-legs/<symbol>")
def delta_engine_broker_legs(symbol):
    """Preview what would be attached for a symbol before committing -- lets the UI show the
    detected legs/strikes so you can confirm it matched the right position."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    result, err = _classify_broker_legs_for_symbol(symbol)
    if err:
        return jsonify({"error": err}), 400
    return jsonify(result)


@app.route("/api/delta-engine/attach-broker", methods=["POST"])
def delta_engine_attach_broker():
    """Attaches a position for monitoring straight from your LIVE Zerodha broker positions -- for
    a condor/strangle you opened outside this tool's own Condor/Strangle builder tab (e.g. placed
    directly in Kite), so there's no positions.json Position ID to type in. Just give the underlying
    symbol (e.g. "SBIN") and this finds and classifies the open legs itself."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    result, err = _classify_broker_legs_for_symbol(symbol)
    if err:
        return jsonify({"error": err}), 400
    legs = result["legs"]
    credit_per_share = (sum(v["entry_price"] for k, v in legs.items() if k.startswith("sell"))
                         - sum(v["entry_price"] for k, v in legs.items() if k.startswith("buy")))
    pos_id = f"BROKER-{symbol}-{int(time.time())}"
    tracked = {
        "id": pos_id, "symbol": symbol, "strategy_type": result["strategy_type"],
        "legs": {k: {"strike": v["strike"], "ltp": v["entry_price"], "tradingsymbol": v["tradingsymbol"]}
                 for k, v in legs.items()},
        "quantity": result["quantity"], "lot_size": result["lot_size"],
        "entry_credit": round(credit_per_share * result["quantity"], 2),
        "source": "broker", "status": "monitoring", "added_at": now_ist().isoformat(),
        "closed_at": None, "exit_reason": None, "realized_pnl": None, "last_greeks": None,
        "pending_suggestion": None,
    }
    positions = load_delta_positions()
    positions.append(tracked)
    save_delta_positions(positions)
    return jsonify({"ok": True, "tracked": tracked})


@app.route("/api/delta-engine/resume/<symbol>", methods=["POST"])
def delta_engine_resume_symbol(symbol):
    """Manually un-pauses a symbol whose own daily-loss limit tripped earlier today, if you've
    reviewed it and want the engine to resume proposing/executing adjustments for it (profit-target
    and stop-loss exits keep working for a paused symbol regardless -- this only affects new
    adjustments)."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    symbol = symbol.upper()
    state = dn_load_state()
    paused = [s for s in state.get("paused_symbols_today", []) if s != symbol]
    state["paused_symbols_today"] = paused
    dn_save_state(state)
    return jsonify({"ok": True, "state": state})


@app.route("/api/delta-engine/positions")
def delta_engine_positions():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    return jsonify({"positions": load_delta_positions()})


@app.route("/api/delta-engine/positions/<pos_id>/untrack", methods=["POST"])
def delta_engine_untrack(pos_id):
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    positions = load_delta_positions()
    for p in positions:
        if p["id"] == pos_id:
            p["status"] = "closed"
            p["closed_at"] = now_ist().isoformat()
            p["exit_reason"] = "Manually untracked"
    save_delta_positions(positions)
    return jsonify({"ok": True})


@app.route("/api/delta-engine/suggestions")
def delta_engine_suggestions():
    """Every pending adjustment suggestion, one per monitored position that currently has a delta
    breach -- each candidate carries its probability of profit, risk-reduction score, expected extra
    premium, and whether it's currently executable given today's risk budget. Nothing here has been
    or will be executed automatically; you choose what (if anything) to act on."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    positions = load_delta_positions()
    out = []
    for p in positions:
        if p["status"] == "monitoring" and p.get("pending_suggestion"):
            out.append({"position_id": p["id"], "symbol": p["symbol"], **p["pending_suggestion"]})
    return jsonify({"suggestions": out})


@app.route("/api/delta-engine/suggestions/<pos_id>/execute", methods=["POST"])
def delta_engine_execute_suggestion(pos_id):
    """Executes ONE specific candidate from a position's current pending suggestion -- the only way
    an adjustment is ever placed. Runs in whichever execution_mode (Track/Live) is currently set."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    action = body.get("action")
    if not action:
        return jsonify({"error": "action is required (the candidate's action name to execute)"}), 400
    positions = load_delta_positions()
    position = next((p for p in positions if p["id"] == pos_id), None)
    if not position:
        return jsonify({"error": "Position not found"}), 404
    suggestion = position.get("pending_suggestion")
    if not suggestion:
        return jsonify({"error": "No pending suggestion for this position -- it may have already been acted on or cleared"}), 400
    candidate_dict = next((c for c in suggestion["candidates"] if c["action"] == action), None)
    if not candidate_dict:
        return jsonify({"error": f"No candidate with action '{action}' in the current suggestion"}), 400
    candidate = AdjustmentCandidate(**{k: v for k, v in candidate_dict.items() if k in AdjustmentCandidate.__dataclass_fields__})
    ok, message = _execute_delta_adjustment(position, candidate, suggestion["trigger_reason"],
                                             all_candidate_actions=[c["action"] for c in suggestion["candidates"]])
    save_delta_positions(positions)
    if not ok:
        return jsonify({"error": message}), 400
    return jsonify({"ok": True, "message": message, "position": position})


@app.route("/api/delta-engine/suggestions/<pos_id>/dismiss", methods=["POST"])
def delta_engine_dismiss_suggestion(pos_id):
    """Clears the current pending suggestion without executing anything -- the next monitoring cycle
    will re-evaluate and generate a fresh one if the breach is still there."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    positions = load_delta_positions()
    for p in positions:
        if p["id"] == pos_id:
            p["pending_suggestion"] = None
    save_delta_positions(positions)
    return jsonify({"ok": True})


@app.route("/api/delta-engine/positions/<pos_id>/close-legs", methods=["POST"])
def delta_engine_close_legs(pos_id):
    """Manually closes some or all legs of a monitored position, in Track or Live (whichever
    execution_mode is currently set), independent of the automatic profit-target/stop-loss logic --
    for when you'd rather act on your own judgment. Body: {"roles": ["sell_call", ...]} -- omit or
    pass an empty list to close every open leg."""
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    body = request.json or {}
    roles = body.get("roles") or []
    positions = load_delta_positions()
    position = next((p for p in positions if p["id"] == pos_id), None)
    if not position:
        return jsonify({"error": "Position not found"}), 404
    reason = body.get("reason") or ("Manual close (all legs)" if not roles else f"Manual close: {', '.join(roles)}")
    ok, message = _close_delta_position_legs(position, roles, reason)
    save_delta_positions(positions)
    if not ok:
        return jsonify({"error": message}), 400
    return jsonify({"ok": True, "message": message, "position": position})


@app.route("/api/delta-engine/logs")
def delta_engine_logs():
    if not require_session():
        return jsonify({"error": "not_logged_in"}), 401
    limit = int(request.args.get("limit", 200))
    return jsonify({"logs": _delta_logger.read_recent(limit)})


# ============================================================================
import sqlite3
from statistics import mean
AI_DB_FILE = os.path.join(os.path.dirname(__file__), "ai_evolution_fresh.db")
AI_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
AI_HIST_INTERVAL = os.environ.get("AI_HIST_INTERVAL", "5minute")
def _ai_ts_naive(value):
    """Normalize any stored/Kite timestamp to naive IST for safe comparisons.

    Kite returns timezone-aware candle timestamps while some local runtime timestamps
    are intentionally stored as aware IST strings. Mixing those with now_ist() (naive)
    caused the previous `offset-naive and offset-aware datetimes` failure.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            return None
    if dt.tzinfo is not None:
        return dt.astimezone(IST).replace(tzinfo=None)
    return dt
def ai_db():
    c=sqlite3.connect(AI_DB_FILE, timeout=30, check_same_thread=False)
    c.row_factory=sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return c
def ai_init_db():
    """Keep Stock AI's historical archive and log; leave all existing tables intact."""
    c=ai_db()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS ai_events(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,level TEXT,event TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS ai_hist_candles(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,symbol TEXT,interval TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,oi REAL,UNIQUE(symbol,interval,ts));
        CREATE INDEX IF NOT EXISTS idx_ai_hist_symbol_interval_ts ON ai_hist_candles(symbol,interval,ts);
        CREATE INDEX IF NOT EXISTS idx_ai_hist_interval_ts ON ai_hist_candles(interval,ts);
        """)
        c.commit()
    finally:c.close()

def ai_log(level,event,detail=""):
    try:
        c=ai_db();c.execute("INSERT INTO ai_events VALUES(NULL,?,?,?,?)",(datetime.now().isoformat(),level,event,detail));c.commit();c.close()
    except Exception: pass
def ai_market_open():
    try:
        n=now_ist(); return n.weekday()<5 and datetime.strptime("09:15","%H:%M").time() <= n.time() <= datetime.strptime("15:30","%H:%M").time()
    except: return False
_HIST_EMA_WEIGHT_CACHE = {}
def _hist_feature_row(rows, i, arrays=None):
    """Fast, deterministic historical feature builder.

    The old implementation rebuilt the complete price history for every feature row.
    With ~14k candles that made historical pre-training unnecessarily expensive on a
    small VM.  This version only reads the windows it actually needs and keeps the
    feature definition compatible with the live ML feature names.
    """
    i=int(i)
    if i < 0 or i >= len(rows):
        return {}
    if arrays is None:
        arrays={
            "close":np.asarray([float(r["close"]) for r in rows],dtype=float),
            "high":np.asarray([float(r["high"]) for r in rows],dtype=float),
            "low":np.asarray([float(r["low"]) for r in rows],dtype=float),
            "volume":np.asarray([float(r.get("volume") or 0) for r in rows],dtype=float),
        }
    closes,highs,lows,vols=arrays["close"],arrays["high"],arrays["low"],arrays["volume"]
    c=float(closes[i])
    if not np.isfinite(c) or c<=0:return {}
    def ret(n):
        j=i-int(n)
        return ((c/closes[j])-1)*100 if j>=0 and closes[j] else 0.0
    def ema(n):
        # Preserve the previous bounded EMA definition, but evaluate the recurrence
        # with a vector dot-product instead of a Python loop.  This is materially
        # faster on the small Oracle VM while keeping the same 4*n bounded window.
        n=int(n)
        lo=max(0,i-n*4)
        x=closes[lo:i+1]
        if len(x)==0:return c
        a=2/(n+1); r=1-a
        m=len(x)-1
        if m<=0:return float(x[0])
        # EMA_i = a*sum(x[i-k]*r**k, k=0..m-1) + x[lo]*r**m
        cache_key=(n,m)
        weights=_HIST_EMA_WEIGHT_CACHE.get(cache_key)
        if weights is None:
            weights=(a*np.power(r,np.arange(m-1,-1,-1,dtype=np.float32))).astype(np.float32)
            _HIST_EMA_WEIGHT_CACHE[cache_key]=weights
        return float(np.dot(x[1:],weights) + x[0]*(r**m))
    def rsi(n=14):
        lo=max(0,i-int(n))
        d=np.diff(closes[lo:i+1])
        if len(d)<n:return 50.0
        up=float(np.mean(np.maximum(d,0))); dn=float(np.mean(np.maximum(-d,0)))
        return 100.0 if dn==0 and up>0 else 50.0 if dn==0 else 100-(100/(1+up/dn))
    w10=closes[max(0,i-9):i+1]
    v10=float(np.std(w10)/np.mean(w10)*100) if len(w10)>=5 and np.mean(w10)!=0 else 0.0
    wh=highs[max(0,i-19):i+1]; wl=lows[max(0,i-19):i+1]
    rng20=float((np.max(wh)-np.min(wl))/c*100) if len(wh)>=10 else 0.0
    wv=vols[max(0,i-19):i+1]; avg_v=float(np.mean(wv)) if len(wv) else 0.0
    vr=float(vols[i]/avg_v) if avg_v>0 else 1.0
    e9,e20,e50=ema(9),ema(20),ema(50)
    trend=max(0,min(25,12.5+(e20/e50-1)*500)) if e50 else 12.5
    mom=max(0,min(25,12.5+ret(5)*3+ret(20)))
    if arrays is not None and arrays.get("ts") is not None:
        ts=str(arrays["ts"][i])
    else:
        row_i=rows[i]
        ts=str(row_i[0] if not isinstance(row_i,dict) else row_i["ts"])
    try:
        dt=datetime.fromisoformat(ts.replace("Z","+00:00"))
        if dt.tzinfo: dt=dt.astimezone(IST).replace(tzinfo=None)
        minutes=dt.hour*60+dt.minute; ang=2*math.pi*(minutes/(24*60))
        sinv,cosv=math.sin(ang),math.cos(ang)
    except Exception:
        sinv=cosv=0.0
    return {
        "ret_1":ret(1),"ret_3":ret(3),"ret_5":ret(5),"ret_10":ret(10),"ret_20":ret(20),
        "ema_gap_9_20":(e9/e20-1)*100 if e20 else 0,
        "ema_gap_20_50":(e20/e50-1)*100 if e50 else 0,
        "rsi14":rsi(),"volatility_10":v10,"range_20":rng20,"volume_ratio":vr,
        "trend_score":trend,"momentum_score":mom,"pcr_norm":1.0,"iv_norm":0.0,
        "time_sin":sinv,"time_cos":cosv,
    }
compute_index_features = _hist_feature_row

def _stock_pattern_features(rows, i, arrays=None):
    """Stock-only structural features from already cached 5-minute candles.

    No broker request occurs here. Keeping this separate from `_hist_feature_row`
    means the shared direction model and Index AI retain their original fast feature
    path; Stock AI invokes structural work only for live stock snapshots and its
    historical setup-success research layer.
    """
    i=int(i)
    if i < 0 or i >= len(rows):return {}
    if arrays is None:
        arrays={
            'close':np.asarray([float(r['close']) for r in rows],dtype=float),
            'high':np.asarray([float(r['high']) for r in rows],dtype=float),
            'low':np.asarray([float(r['low']) for r in rows],dtype=float),
            'volume':np.asarray([float(r.get('volume') or 0) if isinstance(r,dict) else float(r['volume'] or 0) for r in rows],dtype=float),
            'ts':[str(r.get('ts')) if isinstance(r,dict) else str(r['ts']) for r in rows],
        }
    closes,highs,lows,vols=arrays['close'],arrays['high'],arrays['low'],arrays['volume']
    ts_list=arrays.get('ts')
    if ts_list is None:
        ts_list=[str(r.get('ts')) if isinstance(r,dict) else str(r['ts']) for r in rows]
        arrays['ts']=ts_list
    c=float(closes[i])
    if not np.isfinite(c) or c<=0:return {}
    def ret(n):
        j=i-int(n);return ((c/closes[j])-1)*100 if j>=0 and closes[j] else 0.0
    def prior_range(n):
        lo=max(0,i-int(n));hh=highs[lo:i];ll=lows[lo:i]
        if len(hh)<3:return c,c
        return float(np.max(hh)),float(np.min(ll))
    def range_pos(n):
        hh,ll=prior_range(n);width=hh-ll
        return float(np.clip(2*((c-ll)/width)-1,-2.5,2.5)) if width>0 else 0.0
    def breakout(n):
        hh,ll=prior_range(n)
        if hh>0 and c>hh:return float((c/hh-1)*100)
        if ll>0 and c<ll:return float(-(ll/c-1)*100)
        return 0.0
    cur_day=str(ts_list[i])[:10];session_start=i
    for j in range(i-1,max(-1,i-90),-1):
        if str(ts_list[j])[:10]!=cur_day:break
        session_start=j
    def row_open(idx):
        try:
            r=rows[idx];v=r.get('open') if isinstance(r,dict) else r['open']
            return float(v or closes[idx])
        except Exception:return float(closes[idx])
    day_hi=float(np.max(highs[session_start:i+1]));day_lo=float(np.min(lows[session_start:i+1]));day_width=day_hi-day_lo
    day_position=float(np.clip(2*((c-day_lo)/day_width)-1,-1,1)) if day_width>0 else 0.0
    or_end=min(i+1,session_start+3);or_hi=float(np.max(highs[session_start:or_end]));or_lo=float(np.min(lows[session_start:or_end]));or_width=or_hi-or_lo
    opening_range_position=float(np.clip((c-(or_hi+or_lo)/2)/(or_width/2),-3,3)) if or_width>0 else 0.0
    sess_vol=vols[session_start:i+1];sess_tp=(highs[session_start:i+1]+lows[session_start:i+1]+closes[session_start:i+1])/3
    sv=float(np.sum(sess_vol));vwap=float(np.dot(sess_tp,sess_vol)/sv) if sv>0 else float(np.mean(sess_tp))
    prev_close=float(closes[session_start-1]) if session_start>0 else row_open(session_start)
    last3=vols[max(session_start,i-2):i+1];base_start=max(session_start,i-12);base_end=max(base_start,i-2);base_vol=vols[base_start:base_end]
    if len(base_vol)<3:base_vol=vols[max(0,i-19):i]
    base_mean=float(np.mean(base_vol)) if len(base_vol) else 0.0
    med_vol=float(np.median(vols[max(0,i-20):i])) if i>0 else 0.0
    r5_hi=float(np.max(highs[max(0,i-4):i+1]));r5_lo=float(np.min(lows[max(0,i-4):i+1]));recent_range=max(0.0,r5_hi-r5_lo)
    prior_hi=highs[max(0,i-24):max(0,i-4)];prior_lo=lows[max(0,i-24):max(0,i-4)]
    prior_width=float(np.max(prior_hi)-np.min(prior_lo)) if len(prior_hi)>=5 else recent_range
    wh6=highs[max(0,i-5):i+1];wl6=lows[max(0,i-5):i+1]
    structure_6=float((np.sign(np.diff(wh6)).sum()+np.sign(np.diff(wl6)).sum())/(2*(len(wh6)-1))) if len(wh6)>=3 else 0.0
    return {
        'range_pos_20':range_pos(20),'range_pos_50':range_pos(50),
        'breakout_20':breakout(20),'breakout_50':breakout(50),
        'vwap_distance':float((c/vwap-1)*100 if vwap>0 else 0.0),
        'opening_range_position':opening_range_position,
        'gap_pct':float(np.clip((row_open(session_start)/prev_close-1)*100 if prev_close>0 else 0.0,-15,15)),
        'volume_accel':float(np.clip(float(np.mean(last3))/base_mean,0,8)) if base_mean>0 and len(last3) else 1.0,
        'compression_ratio':float(np.clip(recent_range/prior_width,0,5)) if prior_width>0 else 1.0,
        'momentum_accel':float(ret(3)-.30*ret(10)),'day_position':day_position,
        'structure_6':float(np.clip(structure_6,-1,1)),
        'abnormal_volume':float(np.clip(float(vols[i])/med_vol,0,10)) if med_vol>0 else 1.0,
    }
_AI_LIVE_BAR_LOCK = threading.Lock()
_AI_LIVE_BAR_REFRESH = {}
def ai_refresh_live_bars(symbol):
    """Refresh the recent tail independently of slow archive backfill/training."""
    with _AI_LIVE_BAR_LOCK:
        now = time.monotonic()
        if now - _AI_LIVE_BAR_REFRESH.get(symbol, -1e9) < 60:
            return
        _AI_LIVE_BAR_REFRESH[symbol] = now
        token, err = resolve_token_for_symbol(symbol)
        if err:
            ai_log("ERROR", "LIVE_BAR_REFRESH", f"{symbol}: {err}")
            return
        end = now_ist()
        candles, err = _hist_fetch_chunk(token, end - timedelta(days=10), end)
        if err:
            ai_log("ERROR", "LIVE_BAR_REFRESH", f"{symbol}: {err}")
            return
        _hist_insert_candles(symbol, candles)
def _get_recent_index_bars(symbol, n):
    """Read the most recent n bars for `symbol` from ai_hist_candles — the exact
    same table/columns used for historical training — so live prediction reads
    from the identical data source as training, not a separately-maintained
    snapshot cache."""
    c = ai_db()
    rows = c.execute(
        "SELECT ts,open,close,high,low,volume FROM ai_hist_candles WHERE symbol=? AND interval=? ORDER BY ts DESC LIMIT ?",
        (symbol, AI_HIST_INTERVAL, int(n))).fetchall()
    c.close()
    return list(reversed(rows))
def _hist_insert_candles(symbol, candles):
    """Insert a historical response and deduplicate by symbol/interval/timestamp."""
    added=0
    c=ai_db()
    for x in candles:
        try:
            cur=c.execute("INSERT INTO ai_hist_candles(ts,symbol,interval,open,high,low,close,volume,oi) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,interval,ts) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,oi=excluded.oi",
                      (str(x["date"]),symbol,AI_HIST_INTERVAL,float(x["open"]),float(x["high"]),float(x["low"]),float(x["close"]),float(x.get("volume") or 0),float(x.get("oi") or 0)))
            # STAGE 1 FIX: sqlite3.Connection has no .rowcount attribute (only the
            # cursor returned by execute() does). The old `c.rowcount` line raised
            # AttributeError on every row, silently caught below, so rows_added
            # has always reported 0 here regardless of how much data actually
            # landed -- exactly the false-zero display the spec prohibits (Sec 29),
            # just one layer down in the ingestion telemetry rather than the UI.
            added += cur.rowcount
        except Exception as e:
            ai_log("ERROR","HIST_CANDLE_INSERT",f"{symbol}: {type(e).__name__}: {e}")
    c.commit(); c.close()
    return added
def _hist_fetch_chunk(token, start, end):
    """Fetch one legal-sized Kite historical window with a small retry/backoff."""
    last_err=None
    for attempt in range(3):
        try:
            return kite.historical_data(token,start,end,AI_HIST_INTERVAL), None
        except Exception as e:
            last_err=e
            if attempt<2:
                time.sleep(0.8*(attempt+1))
    return [], str(last_err)
# Serve frontend
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "index.html")



# AI DESK V2 — new section, additive tables in the existing database
# ===========================================================================
from contextlib import contextmanager
# Embedded engine: this backend needs no separate ai_engine_v2.py file.
# A private factory preserves the engine namespace without changing other sections.
def _build_desk_engine_class():
    """AI Desk v2: historical direction models, forward evidence and durable orders.
    Injected broker/archive adapter; no credentials and no import-time workers.
    """
    import json
    import math
    import threading
    import time
    import uuid
    from datetime import datetime, timedelta, timezone
    import numpy as np

    IST = timezone(timedelta(hours=5, minutes=30))
    FEATURES = ['ret_1','ret_3','ret_5','ret_10','ret_20','ema_gap_9_20',
                'ema_gap_20_50','rsi14','volatility_10','range_20','volume_ratio',
                'trend_score','momentum_score','time_sin','time_cos']
    DEFAULTS = dict(mode='paper', armed=False, live_override=False, capital=25000., risk=1500.,
        daily_loss=7500., max_positions=3, confidence=.58, stop_pct=12.,
        reward_r=1.8, max_spread=1., max_hold=90, square_off='15:15',
        lots=0, cooldown=10, slippage_bps=5., fee_per_order=25., partial_take_r=1.)
    TERMINAL = {'COMPLETE','CANCELLED','REJECTED'}

    def stamp(): return datetime.now(IST).isoformat()
    def dt(v):
        x=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        return x.replace(tzinfo=IST) if x.tzinfo is None else x.astimezone(IST)
    def finite(v): return isinstance(v,(int,float)) and math.isfinite(v)
    def square_off_due(cfg, when=None):
        when=when or datetime.now(IST)
        return when.time() >= datetime.strptime(str(cfg['square_off']),'%H:%M').time()
    def sigmoid(x): return 1/(1+np.exp(-np.clip(x,-30,30)))
    def auc(y,p):
        y=np.asarray(y);p=np.asarray(p); a=int(y.sum());b=len(y)-a
        if not a or not b:return None
        order=np.argsort(p);ranks=np.empty(len(p),float);i=0
        while i<len(p):
            j=i+1
            while j<len(p) and p[order[j]]==p[order[i]]:j+=1
            ranks[order[i:j]]=(i+1+j)/2;i=j
        return float((ranks[y==1].sum()-a*(a+1)/2)/(a*b))
    def fit(X,Y):
        X=np.asarray(X,float);Y=np.asarray(Y,float)
        mu=X.mean(0);sd=np.maximum(X.std(0),1e-5);z=np.clip((X-mu)/sd,-6,6)
        w=np.zeros(X.shape[1]);bias=0.
        for _ in range(220):
            err=sigmoid(z@w+bias)-Y
            w-=.06*(z.T@err/len(Y)+.02*w);bias-=.06*err.mean()
        return dict(mu=mu.tolist(),sd=sd.tolist(),w=w.tolist(),bias=float(bias))
    def predict(model,X):
        return sigmoid(np.clip((np.asarray(X)-model['mu'])/model['sd'],-6,6)@np.asarray(model['w'])+model['bias'])

    class Engine:
        def __init__(self, adapter):
            self.a=adapter;self.lock=threading.RLock();self.train_lock=threading.Lock()
            self.signals={};self.health={};self.mismatches=set();self.training=dict(status='IDLE',progress=0)
            self.record_cache={};self.recorder_error=None
            self.last_cycle=None;self.stop_event=threading.Event();self.started=False
            self.init_db()
            # A process restart must never silently re-arm real trading.
            cfg=self.config();cfg['armed']=False;self.put('config',cfg)
            self.event('SYSTEM','Desk restarted; entries disarmed. Existing orders will reconcile.')
            # Recover intents that crashed before any broker submission was possible.
            with self.db() as c:
                c.execute("UPDATE desk_positions SET status='CANCELLED' WHERE status='ENTRY_PENDING' AND qty=0 AND NOT EXISTS (SELECT 1 FROM desk_orders WHERE position_id=desk_positions.id)")
            for order in self.rows("SELECT * FROM desk_orders WHERE mode='paper' AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')"):
                self.apply_order(order['id'],dict(status='CANCELLED',filled_quantity=order['filled'],average_price=order['notional']/order['filled'] if order['filled'] else 0))
            for position in self.active():
                self.record_trade(position['id'],'RESTART_GAP',dict(note='Process restarted; observations during downtime are unavailable.'))
        def db(self):return self.a.db()
        def init_db(self):
            with self.db() as c:
                c.executescript('''
                CREATE TABLE IF NOT EXISTS desk_trade_events(id INTEGER PRIMARY KEY,position_id TEXT,ts TEXT,kind TEXT,payload TEXT);
                CREATE INDEX IF NOT EXISTS desk_trade_events_position ON desk_trade_events(position_id,id);
                CREATE TABLE IF NOT EXISTS desk_exit_reviews(id INTEGER PRIMARY KEY,position_id TEXT,ts TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS desk_meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS desk_models(id TEXT PRIMARY KEY,ts TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS desk_events(id INTEGER PRIMARY KEY,ts TEXT,kind TEXT,symbol TEXT,detail TEXT);
                CREATE TABLE IF NOT EXISTS desk_positions(id TEXT PRIMARY KEY,ts TEXT,symbol TEXT,mode TEXT,contract TEXT,exchange TEXT,
                  lot INTEGER,qty INTEGER DEFAULT 0,entry REAL DEFAULT 0,stop REAL,target REAL,risk REAL,
                  status TEXT,exit_ts TEXT,pnl REAL DEFAULT 0,fees REAL DEFAULT 0,setup TEXT,exit_reason TEXT);
                CREATE TABLE IF NOT EXISTS desk_orders(id TEXT PRIMARY KEY,ts TEXT,position_id TEXT,side TEXT,qty INTEGER,
                  mode TEXT,tag TEXT UNIQUE,broker_id TEXT,status TEXT,filled INTEGER DEFAULT 0,notional REAL DEFAULT 0,
                  limit_price REAL,reason TEXT,error TEXT);
                CREATE TABLE IF NOT EXISTS desk_observations(id TEXT PRIMARY KEY,symbol TEXT,ts TEXT,target_ts TEXT,
                  spot REAL,features TEXT,model_id TEXT,prob REAL,label INTEGER,settled_ts TEXT);
                CREATE INDEX IF NOT EXISTS desk_obs_label ON desk_observations(label);
                ''')
                if 'reduced' not in [r[1] for r in c.execute('PRAGMA table_info(desk_positions)')]:
                    c.execute('ALTER TABLE desk_positions ADD COLUMN reduced INTEGER DEFAULT 0')
        def get(self,key,default=None):
            with self.db() as c:r=c.execute('SELECT value FROM desk_meta WHERE key=?',(key,)).fetchone()
            return json.loads(r[0]) if r else default
        def put(self,key,value):
            with self.db() as c:c.execute('INSERT OR REPLACE INTO desk_meta VALUES(?,?)',(key,json.dumps(value)))
        def event(self,kind,detail,symbol=''):
            with self.db() as c:c.execute('INSERT INTO desk_events VALUES(NULL,?,?,?,?)',(stamp(),kind,symbol,str(detail)))
        def record_trade(self,pid,kind,payload):
            # Recorder failures must not prevent broker reconciliation or protective exits.
            try:
                with self.db() as c:
                    if self.recorder_error:
                        c.execute('INSERT INTO desk_trade_events(position_id,ts,kind,payload) VALUES(?,?,?,?)',
                            (pid,stamp(),'RECORDER_GAP',json.dumps(dict(note=self.recorder_error))))
                    c.execute('INSERT INTO desk_trade_events(position_id,ts,kind,payload) VALUES(?,?,?,?)',
                        (pid,stamp(),kind,json.dumps(payload,default=str)))
                self.recorder_error=None
                return True
            except Exception:
                self.recorder_error='Some events could not be saved; recording is incomplete.'
                return False
        def record_observation(self,p,q=None,error=None,decision=None):
            now=time.monotonic();prev=self.record_cache.get(p['id'],{})
            sig=self.signals.get(p['symbol'],{})
            signal={k:sig.get(k) for k in ('direction','confidence','spot','bar_ts','ts','bar_age','model_id')}
            key=(error,decision,signal.get('direction'),signal.get('bar_ts'),p.get('stop'),p.get('target'))
            # At most one routine sample per second, plus decision / signal / gap changes.
            if prev.get('key')==key and now-prev.get('at',0)<1:return
            if error and prev.get('key')==key and now-prev.get('at',0)<30:return
            payload=dict(signal=signal,stop=p.get('stop'),target=p.get('target'),remaining_qty=p['qty'],decision=decision,
                seconds_since_previous=round(now-prev['at'],3) if prev else None)
            if error:payload['error']=str(error)
            else:
                payload['quote']={k:q.get(k) for k in ('bid','ask','spread_pct','bid_qty','ask_qty','volume','oi','quote_ts','exchange_ts','source')}
                payload['open_pnl_at_bid']=(q['bid']-p['entry'])*p['qty']
                payload['net_total_at_bid']=payload['open_pnl_at_bid']+p['pnl']-p['fees']
            if self.record_trade(p['id'],'DATA_GAP' if error else 'OBSERVATION',payload):
                self.record_cache[p['id']]=dict(at=now,key=key)
        def trade_timeline(self,pid,after=0,limit=200):
            rows=self.rows('SELECT * FROM desk_trade_events WHERE position_id=? AND id>? ORDER BY id LIMIT ?', (pid,after,limit+1))
            return dict(events=[dict(id=r['id'],ts=r['ts'],kind=r['kind'],data=json.loads(r['payload'])) for r in rows[:limit]],
                next_after=rows[limit-1]['id'] if len(rows)>limit else None)
        def config(self):return {**DEFAULTS,**self.get('config',{})}
        def rows(self,sql,args=()):
            with self.db() as c:return [dict(r) for r in c.execute(sql,args).fetchall()]
        def active(self):return self.rows("SELECT * FROM desk_positions WHERE status IN ('OPEN','ENTRY_PENDING','EXIT_PENDING') ORDER BY ts")
        def model(self):
            r=self.rows('SELECT payload FROM desk_models ORDER BY ts DESC,rowid DESC LIMIT 1')
            return json.loads(r[0]['payload']) if r else None
        def save_config(self,body):
            with self.lock:
                cfg=self.config()
                bounds=dict(capital=(1000,10000000),risk=(100,100000),daily_loss=(100,1000000),max_positions=(1,5),
                  confidence=(.55,.85),stop_pct=(3,40),reward_r=(1,4),max_spread=(.1,3),max_hold=(5,360),
                  lots=(0,50),cooldown=(0,120),slippage_bps=(1,100),fee_per_order=(1,500),partial_take_r=(.5,3))
                for k,v in body.items():
                    if k=='square_off':
                        if not isinstance(v,str) or len(v)!=5 or not '10:00'<=v<='15:20':raise ValueError('Square-off must be 10:00–15:20 IST')
                        datetime.strptime(v,'%H:%M');cfg[k]=v
                    elif k in bounds:
                        if isinstance(v,bool):raise ValueError('Invalid '+k)
                        if k=='cooldown' and (v is None or isinstance(v,str) and v.strip().lower() in ('na','n/a','')):v=0
                        v=float(v);lo,hi=bounds[k]
                        if not math.isfinite(v) or not lo<=v<=hi:raise ValueError(f'{k}: expected {lo} to {hi}')
                        if k in ('max_positions','max_hold','cooldown','lots') and not v.is_integer():raise ValueError(k+' must be a whole number')
                        cfg[k]=int(v) if k in ('max_positions','max_hold','cooldown','lots') else v
                    else:raise ValueError('Unsupported setting: '+k)
                if cfg['partial_take_r']>=cfg['reward_r']:raise ValueError('Partial take-profit must be below the full target in R')
                if cfg['risk']>cfg['capital']:raise ValueError('Risk budget exceeds capital per trade')
                self.put('config',cfg);self.event('SETTINGS','Risk settings updated');return cfg
        def train(self):
            if not self.train_lock.acquire(False):return False
            threading.Thread(target=self._train,daemon=True,name='desk-train').start();return True
        def _train(self):
            try:
                self.training=dict(status='READING ARCHIVE',progress=5)
                samples=[];coverage=[]
                for j,symbol in enumerate(self.a.symbols):
                    rows=[r for r in self.a.bars(symbol,20000) if dt(r['ts'])+timedelta(minutes=5)<=datetime.now(IST)]
                    coverage.append(dict(symbol=symbol,bars=len(rows),first=str(rows[0]['ts']) if rows else None,last=str(rows[-1]['ts']) if rows else None))
                    if len(rows)<300:continue
                    arr={k:np.array([float(r[k] or 0) for r in rows]) for k in ('close','high','low','volume')}
                    arr['ts']=[str(r['ts']) for r in rows]
                    for i in range(200,len(rows)-3,3):
                        t=dt(rows[i]['ts']);end=dt(rows[i+3]['ts'])
                        if (end-t).total_seconds()!=900:continue
                        f=self.a.features(rows,i,arr);x=[f[k] for k in FEATURES]
                        if all(finite(v) for v in x):samples.append((t.timestamp(),end.timestamp(),x,int(rows[i+3]['close']>rows[i]['close'])))
                    self.training=dict(status='BUILDING FEATURES',progress=10+20*(j+1)/max(1,len(self.a.symbols)),symbol=symbol)
                samples.sort(key=lambda r:r[0])
                if len(samples)<300:raise ValueError('Need at least 300 labelled observations from five-minute history. Connect Kite and sync the archive.')
                split=int(len(samples)*.8);cut=samples[split][0]
                train=[r for r in samples[:split] if r[1]<cut];val=[r for r in samples[split:] if r[0]>=cut]
                X=[r[2] for r in train];Y=[r[3] for r in train]
                if len(set(Y))<2:raise ValueError('Training requires both UP and DOWN outcomes')
                self.training=dict(status='FITTING & VALIDATING',progress=80)
                m=fit(X,Y);pred=predict(m,[r[2] for r in val]);y=np.array([r[3] for r in val])
                m.update(id=uuid.uuid4().hex[:12],ts=stamp(),features=FEATURES,train_samples=len(train),validation_samples=len(val),
                    accuracy=float(((pred>=.5)==y).mean()*100),auc=auc(y,pred),brier=float(np.mean((pred-y)**2)),
                    baseline=float(max(y.mean(),1-y.mean())*100),coverage=coverage,
                    train_end=datetime.fromtimestamp(max(r[1] for r in train),IST).isoformat(),
                    validation_start=datetime.fromtimestamp(cut,IST).isoformat(),horizon_minutes=15)
                with self.db() as c:c.execute('INSERT INTO desk_models VALUES(?,?,?)',(m['id'],stamp(),json.dumps(m)))
                self.training=dict(status='COMPLETE',progress=100,model_id=m['id']);self.event('LEARN',f"Model {m['id']} trained on {len(train)}; chronological validation {len(val)}")
            except Exception as e:
                self.training=dict(status='ERROR',progress=0,error=str(e));self.event('ERROR','Training: '+str(e))
            finally:self.train_lock.release()
        def settle_observations(self):
            pending=self.rows('SELECT * FROM desk_observations WHERE label IS NULL ORDER BY ts LIMIT 1500')
            by_symbol={}
            for r in pending:by_symbol.setdefault(r['symbol'],[]).append(r)
            for symbol,items in by_symbol.items():
                bars=self.a.bars(symbol,3000);lookup={dt(r['ts']).isoformat():r for r in bars}
                for r in items:
                    bar=lookup.get(r['target_ts'])
                    if bar and dt(bar['ts'])+timedelta(minutes=5)<=datetime.now(IST):
                        label=int(float(bar['close'])>r['spot'])
                        with self.db() as c:c.execute('UPDATE desk_observations SET label=?,settled_ts=? WHERE id=? AND label IS NULL',(label,stamp(),r['id']))
            # Separate online model; historical holdout is never used to train it.
            evidence=self.rows('SELECT * FROM desk_observations WHERE label IS NOT NULL ORDER BY ts DESC LIMIT 1200')
            evidence.reverse();n=len(evidence)
            if n>=80 and (not self.get('online_model') or evidence[-1]['ts']!=self.get('online_last_sample')) and (datetime.now(IST)-dt(self.get('online_trained_at', '2000-01-01T00:00:00+05:30'))).total_seconds()>=300 and len({r['label'] for r in evidence})==2:
                split=int(n*.8);cut=dt(evidence[split]['ts']);train=[r for r in evidence[:split] if dt(r['target_ts'])<cut];val=evidence[split:]
                if len(train)<40:return
                m=fit([json.loads(r['features']) for r in train],[r['label'] for r in train]);p=predict(m,[json.loads(r['features']) for r in val]);y=np.array([r['label'] for r in val])
                acc=float(((p>=.5)==y).mean()*100);baseline=float(max(y.mean(),1-y.mean())*100);brier=float(np.mean((p-y)**2))
                m.update(samples=n,accuracy=acc,auc=auc(y,p),baseline=baseline,brier=brier,validation_samples=len(val),ts=stamp())
                self.put('online_model',m);self.put('online_trained_count',n);self.put('online_last_sample',evidence[-1]['ts']);self.put('online_trained_at',stamp())
        def direction_snapshot(self,symbol,refresh=True,record_observation=True,max_bar_age=15):
            """Run the common directional model without touching an option chain.

            Stock AI uses this as its cheap first-stage full-universe scan. The normal
            index desk still calls inspect(). Stock-only validation/stability gates are
            activated only when the subclass sets ``strict_direction_gate``.
            """
            if refresh:self.a.refresh(symbol)
            bars=self.a.bars(symbol,260)
            now=datetime.now(IST)
            bars=[r for r in bars if dt(r['ts'])+timedelta(minutes=5)<=now]
            s=dict(symbol=symbol,ts=stamp(),decision='WAIT',reason='Waiting for history',chart=[dict(ts=str(r['ts']),close=r['close']) for r in bars[-60:]])
            if len(bars)<220:return s
            last=bars[-1];age=(now-dt(last['ts'])).total_seconds()/60
            s.update(spot=last['close'],bar_ts=str(last['ts']),bar_age=round(age,1))
            m=self.model()
            if not m:s['reason']='Train the historical model';return s
            arr={k:np.array([float(r[k] or 0) for r in bars]) for k in ('close','high','low','volume')};arr['ts']=[str(r['ts']) for r in bars]
            f=self.a.features(bars,len(bars)-1,arr);x=[f[k] for k in FEATURES]
            if not all(finite(v) for v in x):s['reason']='Invalid features';return s
            hist=float(predict(m,x));online=self.get('online_model');op=float(predict(online,x)) if online else None
            use_online=bool(online and (online.get('auc') or 0)>=.52 and online.get('baseline') is not None and float(online.get('accuracy') or 0)>=float(online.get('baseline') or 0)-2)
            p=.8*hist+.2*op if use_online else hist
            s.update(p_up=p,historical_probability=hist,online_probability=op,model_id=m['id'],features=x,
                regime='UPTREND' if f['ema_gap_20_50']>.05 else 'DOWNTREND' if f['ema_gap_20_50']<-.05 else 'RANGE',
                direction='UP' if p>=.5 else 'DOWN',confidence=max(p,1-p))
            extra_names=getattr(self.a,'extra_feature_names',())
            extra_fn=getattr(self.a,'extra_features',None)
            if extra_names and callable(extra_fn):
                extra=extra_fn(bars,len(bars)-1,arr) or {}
                s['pattern_features']={k:float(extra.get(k) or 0.) for k in extra_names}
                s['pattern_feature_version']=int(getattr(self.a,'pattern_feature_version',1))
            s['factors']=sorted([dict(name=FEATURES[i],impact=float(v)) for i,v in enumerate(np.clip((np.array(x)-m['mu'])/m['sd'],-6,6)*m['w'])],key=lambda d:abs(d['impact']),reverse=True)[:4]
            if age<0 or age>max_bar_age:s['reason']='Stale index candles';return s
            # Forward labels accrue without requiring a trade, breaking the no-trades/no-learning loop.
            target=dt(last['ts'])+timedelta(minutes=15)
            if record_observation and self.a.market_open() and target.date()==dt(last['ts']).date() and target.strftime('%H:%M')<='15:25':
                key=symbol+dt(last['ts']).isoformat()
                with self.db() as c:c.execute('INSERT OR IGNORE INTO desk_observations VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',
                    (key,symbol,dt(last['ts']).isoformat(),target.isoformat(),float(last['close']),json.dumps(x),m['id'],p))
            cfg=self.config()
            strict=bool(getattr(self,'strict_direction_gate',False))
            aucv=m.get('auc');acc=m.get('accuracy');baseline=m.get('baseline');brier=m.get('brier')
            model_ok=bool(aucv is not None and acc is not None and baseline is not None and brier is not None and
                float(aucv)>=float(getattr(self,'direction_min_auc',.52)) and
                float(acc)>=float(baseline)-float(getattr(self,'direction_max_acc_deficit',2.0)) and
                float(brier)<=float(getattr(self,'direction_max_brier',.26)))
            s.update(direction_model_auc=aucv,direction_model_accuracy=acc,direction_model_baseline=baseline,direction_model_brier=brier,
                direction_model_entry_eligible=model_ok)
            # Stock AI still measures multi-bar directional persistence for REVERSAL-EXIT
            # confirmation, but it is deliberately NOT an entry gate. Fresh entries use the
            # latest completed bar plus model, liquidity, success/EV and pre-order checks.
            s['signal_stable']=True;s['stability_bars_required']=1;s['stability_bars_confirmed']=1
            if strict:
                needed=max(1,min(3,int(cfg.get('stability_bars',2))))
                probs=[]
                for idx in range(len(bars)-needed,len(bars)):
                    fi=self.a.features(bars,idx,arr);xi=[fi.get(k) for k in FEATURES]
                    if not all(finite(v) for v in xi):probs=[];break
                    hi=float(predict(m,xi));oi=float(predict(online,xi)) if use_online else None
                    probs.append(.8*hi+.2*oi if use_online else hi)
                stable=bool(len(probs)==needed and all((q>=.5)==(p>=.5) and max(q,1-q)>=getattr(self,'stability_min_confidence',.53) for q in probs))
                confirmed=0
                for q in reversed(probs):
                    if (q>=.5)==(p>=.5) and max(q,1-q)>=getattr(self,'stability_min_confidence',.53):confirmed+=1
                    else:break
                s.update(signal_stable=stable,stability_bars_required=needed,stability_bars_confirmed=confirmed,
                    stability_probabilities=[round(float(q),6) for q in probs])
            if not self.a.market_open():s['reason']='Market closed · historical prediction only';return s
            if strict and not model_ok:
                s['reason']=f"Direction model validation gate failed · AUC {float(aucv or 0):.3f}; accuracy {float(acc or 0):.1f}% vs baseline {float(baseline or 0):.1f}%; Brier {float(brier or 9):.3f}"
                return s
            if s['confidence']<cfg['confidence']:s['reason']='Direction below confidence threshold';return s
            # Do not delay a fresh entry solely to wait for a second confirming candle.
            # signal_stable remains available to supervise() for reversal-exit confirmation.
            s.update(decision='CANDIDATE',reason='Direction qualifies for option-liquidity check')
            return s
        def inspect(self,symbol):
            s=self.direction_snapshot(symbol,refresh=True,record_observation=True,max_bar_age=15)
            if s.get('decision')!='CANDIDATE':return s
            return self.option_candidate(s)
        def option_candidate(self,s):
            # Build a quote-qualified candidate; this method never submits an order.
            s=dict(s)
            cfg=self.config();p=s['p_up']
            data,err=self.a.chain(s['symbol'])
            symbol=s['symbol']
            if err:s.update(decision='WAIT',reason='Option chain: '+str(err));return s
            typ='CE' if p>.5 else 'PE';candidates=[]
            for o in data.get('chain',[]):
                if o.get('instrument_type')!=typ or not finite(o.get('delta')):continue
                ask=float(o.get('ask') or 0);bid=float(o.get('bid') or 0)
                if ask<=0 or bid<=0 or bid>ask:continue
                spread=(ask-bid)/((ask+bid)/2)*100
                if spread>cfg['max_spread'] or not .3<=abs(o['delta'])<=.7:continue
                candidates.append((abs(abs(o['delta'])-.5)+spread/10,o))
            if not candidates:s.update(decision='WAIT',reason='No liquid CE/PE contract within spread limit');return s
            o=min(candidates,key=lambda r:r[0])[1]
            expiry=o.get('expiry');dte=None
            try:dte=(expiry-now_ist().date()).days if hasattr(expiry,'__sub__') else None
            except Exception:dte=None
            s.update(contract=o['tradingsymbol'],exchange=self.a.exchange(symbol),
               lot=int(o.get('lot_size') or data.get('lot_size') or 0),tick=float(o.get('tick_size') or .05),option_type=typ,
               option_delta=float(o.get('delta') or 0),option_volume=float(o.get('volume') or 0),option_oi=float(o.get('oi') or 0),
               option_expiry=str(expiry) if expiry is not None else None,dte=dte)
            q,why=self.a.quote(s['exchange'],s['contract'])
            if why:s.update(decision='WAIT',reason=why);return s
            s.update(bid=q['bid'],ask=q['ask'],spread=q['spread_pct'],depth_qty=q['ask_qty'],quote_ts=q.get('quote_ts'))
            if q['spread_pct'] is None or not 0<=q['spread_pct']<=cfg['max_spread']:s.update(decision='WAIT',reason='Option spread widened');return s
            if s['lot']<=0:s.update(decision='WAIT',reason='Invalid contract lot size');return s
            entry=round(math.ceil(q['ask']*(1+cfg['slippage_bps']/10000)/s['tick'])*s['tick'],2);risk=entry*cfg['stop_pct']/100
            fees=2*cfg['fee_per_order']
            max_lots=max(0,int(min(max(0.,cfg['capital']-fees)/entry,max(0.,cfg['risk']-fees)/risk,q['ask_qty'])//s['lot']))
            lots=cfg['lots'] or max_lots
            s.update(requested_lots=cfg['lots'],max_lots=max_lots,lots=lots)
            if lots>max_lots:
                s['qty']=0;s.update(decision='WAIT',reason=f'Requested {lots} lots exceed capital, risk or visible depth limit ({max_lots} lots)');return s
            qty=lots*s['lot']
            s.update(entry=entry,stop=entry-risk,target=entry+risk*cfg['reward_r'],qty=qty)
            if qty<=0:s.update(decision='WAIT',reason='Budget or visible ask depth cannot fund one lot');return s
            s.update(decision='READY',reason=f'{typ} qualifies: direction, quote freshness, spread and size passed')
            return s
        def evidence(self):
            rows=self.rows("SELECT * FROM desk_positions WHERE status='CLOSED' AND mode='paper' ORDER BY exit_ts")
            vals=[r['pnl']-r['fees'] for r in rows];wins=sum(x for x in vals if x>0);loss=-sum(x for x in vals if x<0)
            n=len(vals);span=(dt(rows[-1]['exit_ts'])-dt(rows[0]['ts'])).total_seconds()/86400 if rows else 0
            avg=sum((r['pnl']-r['fees'])/max(r['risk'],.01) for r in rows)/max(n,1)
            pf=wins/loss if loss else None;wr=sum(x>0 for x in vals)/max(n,1)*100
            checks=[dict(name='Closed paper trades',value=n,required=60,ok=n>=60),dict(name='Record span · days',value=round(span,1),required=10,ok=span>=10),
                dict(name='Net win rate · %',value=round(wr,1),required=42,ok=wr>=42),dict(name='Net expectancy · R',value=round(avg,3),required=.05,ok=avg>=.05),
                dict(name='Profit factor',value=round(pf,2) if pf is not None else None,required=1.2,ok=(pf>=1.2 if pf is not None else wins>0))]
            total=0;curve=[]
            for r,v in zip(rows,vals):total+=v;curve.append(dict(ts=r['exit_ts'],pnl=round(total,2)))
            return dict(ready=all(x['ok'] for x in checks),checks=checks,trades=n,net=round(total,2),win_rate=wr,equity=curve)
        def block(self):
            cfg=self.config()
            if not self.a.connected():return 'Connect Kite to receive market data'
            if not self.a.market_open():return 'Market closed · next scan during trading hours'
            if self.get('halt'):return self.get('halt')
            if self.a.legacy_open():return 'Legacy positions remain open · close them before new entries'
            if not cfg['armed']:return cfg['mode'].upper()+' selected · entries DISARMED; learning runs independently'
            if any(p['qty']>0 and self.health.get(p['id'],{}).get('bid') is None for p in self.active()):return 'Position quote unavailable · new entries paused'
            # No pre-square-off entry runway. A valid signal may enter any time before
            # square-off. Once the same square-off exit condition is active, a fresh
            # position would be immediately due for exit, so new entries are suppressed.
            if square_off_due(cfg):return 'Intraday square-off exit condition active · no new entries'
            if self.train_lock.locked():return 'Model training · new entries paused'
            if cfg['mode']=='live' and not (cfg.get('live_override') is True or self.evidence()['ready']):return 'Live locked · build the forward paper record'
            if self.get('close_requested'):return 'Close requested · waiting for all exits'
            if hasattr(self,'shared_risk'):
                shared=self.shared_risk.snapshot(cfg['mode'])
                if shared['entry_block']:return shared['entry_block']
            if self.day_pnl(cfg['mode'])<=-cfg['daily_loss']:return 'Daily loss limit reached'
            return None
        def day_pnl(self,mode):
            today=datetime.now(IST).date().isoformat()
            total=sum(r['pnl']-r['fees'] for r in self.rows("SELECT pnl,fees FROM desk_positions WHERE mode=? AND (status!='CLOSED' OR exit_ts LIKE ?)",(mode,today+'%')))
            for r in self.active():
                if r['mode']==mode and r['qty']:
                    mark=self.health.get(r['id'],{}).get('bid')
                    if mark is not None:total+=(mark-r['entry'])*r['qty']
            return total
        def submit(self,p,side,qty,price,reason):
            oid=uuid.uuid4().hex;tag='DSK'+oid[:17];cfg=self.config()
            if qty<=0:return
            if side=='BUY' and self.get('close_requested'):
                with self.db() as c:c.execute("UPDATE desk_positions SET status='CANCELLED',exit_reason='Close requested before submission' WHERE id=? AND qty=0",(p['id'],))
                self.record_trade(p['id'],'ENTRY_CANCELLED_BEFORE_SUBMISSION',dict(reason='Close requested; no broker request sent'))
                return
            if side=='BUY' and hasattr(self,'shared_risk'):
                try:shared=self.shared_risk.snapshot(p['mode'])
                except Exception:
                    with self.db() as c:c.execute("UPDATE desk_positions SET status='CANCELLED',exit_reason='Shared risk unavailable before submission' WHERE id=? AND qty=0",(p['id'],))
                    self.record_trade(p['id'],'ENTRY_CANCELLED_BEFORE_SUBMISSION',dict(reason='Shared risk unavailable; no broker request sent'))
                    return
                if shared['liquidate']:
                    with self.db() as c:c.execute("UPDATE desk_positions SET status='CANCELLED',exit_reason=? WHERE id=? AND qty=0",(shared['reason'],p['id']))
                    self.record_trade(p['id'],'ENTRY_CANCELLED_BEFORE_SUBMISSION',dict(reason=shared['reason']))
                    return
            if self.rows("SELECT id FROM desk_orders WHERE position_id=? AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')",(p['id'],)):return
            # Intent is durable before contacting the broker. Unknown submissions are never blindly retried.
            with self.db() as c:
                c.execute('INSERT INTO desk_orders(id,ts,position_id,side,qty,mode,tag,status,limit_price,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (oid,stamp(),p['id'],side,qty,p['mode'],tag,'SUBMITTING',price,reason))
                c.execute('UPDATE desk_positions SET status=? WHERE id=?',('ENTRY_PENDING' if side=='BUY' else 'EXIT_PENDING',p['id']))
                if side=='SELL':
                    snapshot=dict(reason=reason,limit_price=price,requested_qty=qty,observed_bid=self.health.get(p['id'],{}).get('bid'),stop=p.get('stop'),target=p.get('target'),signal={k:self.signals.get(p['symbol'],{}).get(k) for k in ('direction','confidence','spot','bar_ts','ts')},risk_config=self.config())
                    c.execute('INSERT INTO desk_exit_reviews(position_id,ts,payload) VALUES(?,?,?)',(p['id'],stamp(),json.dumps(snapshot)))
            self.record_trade(p['id'],'ORDER_INTENT',dict(order_id=oid,side=side,qty=qty,limit_price=price,reason=reason,mode=p['mode'],stop=p.get('stop'),target=p.get('target'),signal=self.signals.get(p['symbol'],{})))
            if p['mode']=='paper':
                self.apply_order(oid,dict(status='COMPLETE',filled_quantity=qty,average_price=price,order_id='PAPER-'+oid));return
            try:
                broker_id=self.a.place(p['exchange'],p['contract'],side,qty,price,tag)
                with self.db() as c:c.execute("UPDATE desk_orders SET broker_id=?,status='OPEN' WHERE id=?",(str(broker_id),oid))
                self.record_trade(p['id'],'BROKER_ACK',dict(order_id=oid,broker_id=str(broker_id),side=side))
                self.event('ORDER',f'{side} submitted; awaiting confirmed fill',p['symbol'])
            except Exception as e:
                if type(e).__name__=='BrokerDeferred':
                    self.apply_order(oid,dict(status='REJECTED',filled_quantity=0,average_price=0,status_message=str(e)))
                    return
                with self.db() as c:c.execute("UPDATE desk_orders SET status='UNKNOWN',error=? WHERE id=?",(str(e),oid))
                self.record_trade(p['id'],'SUBMISSION_UNCERTAIN',dict(order_id=oid,error=str(e)))
                self.put('halt','Order submission uncertain · inspect broker orders');self.event('ERROR','Submission uncertain: '+str(e),p['symbol'])
        def apply_order(self,oid,update):
            guard=getattr(self,'shared_risk',None)
            with guard.lock if guard else self.lock:
                return self._apply_order_locked(oid,update)
        def _apply_order_locked(self,oid,update):
            with self.db() as c:
                o=dict(c.execute('SELECT * FROM desk_orders WHERE id=?',(oid,)).fetchone());p=dict(c.execute('SELECT * FROM desk_positions WHERE id=?',(o['position_id'],)).fetchone())
                filled=int(update.get('filled_quantity') or 0);avg=float(update.get('average_price') or 0);status=str(update.get('status') or 'OPEN')
                if not 0<=filled<=o['qty'] or filled<o['filled'] or (filled and (avg<=0 or not math.isfinite(avg))):raise ValueError('Invalid broker fill')
                if status=='COMPLETE' and filled!=o['qty']:raise ValueError('Incomplete quantity reported COMPLETE')
                delta=filled-o['filled'];notional=avg*filled;delta_value=notional-o['notional']
                if delta:
                    fee=self.config()['fee_per_order'] if not o['filled'] else 0
                    if o['side']=='BUY':
                        qty=p['qty']+delta;entry=(p['qty']*p['entry']+delta_value)/qty
                        c.execute('UPDATE desk_positions SET qty=?,entry=?,risk=?,fees=fees+? WHERE id=?',(qty,entry,max(.01,entry-p['stop'])*qty,fee,p['id']))
                    else:
                        if delta>p['qty']:raise ValueError('Exit fill exceeds owned quantity')
                        qty=p['qty']-delta;pnl=delta_value-p['entry']*delta
                        if o['reason']=='Partial profit at planned R':c.execute('UPDATE desk_positions SET reduced=1,stop=MAX(stop,entry) WHERE id=?',(p['id'],))
                        c.execute('UPDATE desk_positions SET qty=?,pnl=pnl+?,fees=fees+?,exit_reason=? WHERE id=?',(qty,pnl,fee,o['reason'],p['id']))
                c.execute('UPDATE desk_orders SET broker_id=?,status=?,filled=?,notional=?,error=? WHERE id=?',
                  (str(update.get('order_id') or o['broker_id'] or ''),status,filled,notional,update.get('status_message'),oid))
                if status in TERMINAL:
                    row=c.execute('SELECT qty FROM desk_positions WHERE id=?',(p['id'],)).fetchone();qty=row[0]
                    closed=qty==0 and o['side']=='SELL';new='CLOSED' if closed else 'OPEN' if qty else 'CANCELLED'
                    c.execute('UPDATE desk_positions SET status=?,exit_ts=? WHERE id=?',(new,stamp() if closed else None,p['id']))
            if delta or status!=o['status'] or notional!=o['notional']:
                self.record_trade(p['id'],'FILL' if delta else 'ORDER_STATUS',dict(order_id=oid,side=o['side'],status=status,filled_total=filled,fill_delta=delta,average_price=avg,incremental_fill_price=delta_value/delta if delta else None,broker_id=update.get('order_id') or o['broker_id'],broker_timestamp=update.get('exchange_update_timestamp') or update.get('order_timestamp'),reason=o['reason'],error=update.get('status_message')))
            if status in TERMINAL and not self.rows("SELECT id FROM desk_positions WHERE id=? AND status IN ('OPEN','ENTRY_PENDING','EXIT_PENDING')",(p['id'],)):self.record_cache.pop(p['id'],None)
            if delta:self.event('FILL',f"{o['side']} {delta} units confirmed at cumulative average {avg:.2f}",p['symbol'])
            if status in ('REJECTED','CANCELLED'):self.event(status,update.get('status_message') or status,p['symbol'])
        def broker_match(self,o,book,manual_id=None):
            p=self.rows('SELECT * FROM desk_positions WHERE id=?',(o['position_id'],))[0]
            bid=str(manual_id or o['broker_id'] or '')
            matches=[r for r in book if str(r.get('order_id'))==bid] if bid else [r for r in book if r.get('tag')==o['tag']]
            if not matches and bid:
                history=self.a.order_history(bid)
                if history:matches=[history[-1]]
            if len(matches)!=1:raise ValueError('No unique broker match. Supply the exact Kite order ID; do not delete the pending record.')
            r=matches[0]
            if (r.get('tradingsymbol')!=p['contract'] or r.get('exchange')!=p['exchange'] or
                r.get('transaction_type')!=o['side'] or int(r.get('quantity') or 0)!=o['qty'] or
                r.get('product')!=p.get('product','MIS')):raise ValueError('Broker order contract, side, quantity or product does not match this entry')
            if manual_id:
                when=r.get('order_timestamp')
                if not when or abs((dt(when)-dt(o['ts'])).total_seconds())>120:raise ValueError('Manual order ID does not match the local submission time')
                if self.rows('SELECT id FROM desk_orders WHERE broker_id=? AND id!=?',(bid,o['id'])):raise ValueError('Broker ID already belongs to another local order')
            return r
        def recover_order(self,oid,body):
            # Caller owns the engine's execution/risk lock. No automatic re-arming.
            cfg=self.config();cfg['armed']=False;self.put('config',cfg)
            rows=self.rows('SELECT * FROM desk_orders WHERE id=?',(oid,))
            if not rows:raise ValueError('Order not found')
            o=rows[0]
            if o['mode']!='live':raise ValueError('Recovery is for live broker orders')
            if o['status'] in TERMINAL:return dict(status=o['status'],message='Order is already terminal; clear the resolved halt if no other order is pending.')
            p=self.rows('SELECT * FROM desk_positions WHERE id=?',(o['position_id'],))[0]
            if body.get('action')=='verify-not-placed':
                if body.get('ack') is not True:raise ValueError('Explicit confirmation of checking Kite Orders, Trades and Positions is required')
                if o['side']!='BUY' or o['broker_id'] or o['filled'] or p['qty'] or o['status'] not in ('UNKNOWN','SUBMITTING'):raise ValueError('Only an unacknowledged zero-fill BUY can be verified as not placed')
                age=(datetime.now(IST)-dt(o['ts'])).total_seconds()
                if dt(o['ts']).date()!=datetime.now(IST).date() or age<120:raise ValueError('This check requires a same-day order at least two minutes old. Older records require broker evidence.')
                for r in self.a.orders():
                    if r.get('tag')==o['tag'] or (r.get('exchange')==p['exchange'] and r.get('tradingsymbol')==p['contract']):raise ValueError('A broker order exists for this contract. Reconcile its order ID instead.')
                if any(r.get('exchange')==p['exchange'] and r.get('tradingsymbol')==p['contract'] for r in self.a.trades()):raise ValueError('Broker fills exist for this contract; cannot mark not placed')
                if any(r.get('exchange')==p['exchange'] and r.get('tradingsymbol')==p['contract'] and int(r.get('quantity') or 0)!=0 for r in self.a.holdings()):raise ValueError('Broker position exists; cannot mark not placed')
                # This is an explicit operator attestation supported by broker snapshots,
                # not a fabricated broker cancellation. Keep the original row and audit log.
                with self.db() as c:
                    c.execute("UPDATE desk_orders SET status='REJECTED',error='Operator verified NOT PLACED; broker order/trade/position checks empty' WHERE id=?",(oid,))
                    c.execute("UPDATE desk_positions SET status='CANCELLED',exit_reason='Verified not placed by operator' WHERE id=?",(p['id'],))
                self.event('RECOVERY','Operator confirmed NOT PLACED after broker checks; local intent retired: '+oid,p['symbol'])
                result=dict(status='NOT_PLACED',message='Local entry request retired after your confirmation and broker checks.')
            else:
                supplied=str(body.get('broker_id') or '').strip()
                r=self.broker_match(o,self.a.orders(),supplied or None)
                self.apply_order(oid,r)
                if r['status'] not in TERMINAL:
                    self.a.cancel(str(r['order_id']))
                    # Cancellation acceptance is not final status. Re-read before closing.
                    refreshed=self.rows('SELECT * FROM desk_orders WHERE id=?',(oid,))[0]
                    r=self.broker_match(refreshed,self.a.orders())
                    self.apply_order(oid,r)
                self.event('RECOVERY','Cancel/reconcile requested for '+str(r['order_id']),p['symbol'])
                result=dict(status=r['status'],message='Broker status reconciled. Filled quantity remains owned; only the unfilled remainder is cancelled.')
            remaining=self.rows("SELECT id FROM desk_orders WHERE mode='live' AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')")
            self.verify_positions()
            halt=self.get('halt') or ''
            if not remaining and not self.mismatches and any(halt.startswith(x) for x in ('Unresolved broker order','Order submission uncertain','Broker reconciliation:')):
                self.put('halt',None);result['message']+=' Order block cleared. Entries remain disarmed; re-arm when ready.'
            else:result['message']+=' Any remaining order or risk block still needs resolution.'
            self.record_trade(p['id'],'OPERATOR_RECOVERY',dict(order_id=oid,action=body.get('action'),result=result))
            return result
        def reconcile(self):
            pending=self.rows("SELECT * FROM desk_orders WHERE mode='live' AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')")
            if not pending:return
            try:book=self.a.orders()
            except Exception as e:
                if type(e).__name__=='BrokerDeferred':return
                raise
            for o in pending:
                try:
                    r=self.broker_match(o,book);self.apply_order(o['id'],r)
                    if r['status'] not in TERMINAL and (datetime.now(IST)-dt(o['ts'])).total_seconds()>45:self.a.cancel(str(r['order_id']))
                except Exception as e:
                    # One unresolved order must not abort supervision of other positions.
                    if type(e).__name__=='BrokerDeferred':continue
                    self.put('halt','Broker reconciliation: '+str(e))
                    with self.db() as c:c.execute('UPDATE desk_orders SET error=? WHERE id=?',(str(e),o['id']))
            # A remaining uncertainty blocks entries, while verified positions are managed.
        def verify_positions(self):
            live=[p for p in self.active() if p['mode']=='live']
            if not live:self.mismatches=set();return
            actual={}
            try:holdings=self.a.holdings()
            except Exception as e:
                if type(e).__name__=='BrokerDeferred':return
                self.mismatches={p['id'] for p in live}
                raise
            self.mismatches=set()
            for p in holdings:
                if p.get('product')=='MIS':
                    key=(p.get('exchange'),p.get('tradingsymbol'))
                    actual[key]=actual.get(key,0)+int(p.get('quantity') or 0)
            expected={}
            for p in live:
                key=(p['exchange'],p['contract']);expected[key]=expected.get(key,0)+p['qty']
            for key,qty in expected.items():
                if actual.get(key,0)!=qty:
                    self.mismatches.update(p['id'] for p in live if (p['exchange'],p['contract'])==key)
            if self.mismatches:
                self.put('halt','Broker/local position mismatch · reconcile in Kite before continuing')
                for pid in self.mismatches:self.health[pid]={'reason':'Broker quantity mismatch; automated exit suspended'}

        def enter(self,s):
            cfg=self.config()
            if self.block() or s.get('decision')!='READY':return
            if cfg['mode']=='live':
                if any(p.get('exchange')==s['exchange'] and p.get('tradingsymbol')==s['contract'] and int(p.get('quantity') or 0)!=0 for p in self.a.holdings()):
                    self.event('WAIT','Existing broker position in this contract; no entry',s['symbol']);return
            guard=getattr(self,'shared_risk',None)
            with guard.lock if guard else self.lock:
                cfg=self.config()
                if not cfg['armed']:return
                s=dict(s)
                if guard:
                    reason=guard.admit(self,s)
                    if reason:
                        self.signals[s['symbol']]=dict(s,decision='WAIT',reason=reason)
                        return
                active=self.active()
                if len(active)>=cfg['max_positions'] or any(p['symbol']==s['symbol'] for p in active):return
                recent=self.rows('SELECT ts,exit_ts FROM desk_positions WHERE symbol=? ORDER BY ts DESC LIMIT 1',(s['symbol'],))
                if cfg['cooldown']>0 and recent and (datetime.now(IST)-dt(recent[0]['exit_ts'] or recent[0]['ts'])).total_seconds()<cfg['cooldown']*60:return
                s=dict(s);s['risk_config_at_entry']={k:cfg.get(k) for k in ('risk','capital','daily_loss','max_positions','cooldown','lots','stop_pct','reward_r','max_hold','square_off','live_override','confidence','fee_per_order','slippage_bps','max_spread','fallback_confidence','min_success_prob','min_expected_r','stability_bars')}
                pid=uuid.uuid4().hex;entry=s['entry'];qty=s['qty'];risk=(entry-s['stop'])*qty
                with self.db() as c:c.execute('INSERT INTO desk_positions(id,ts,symbol,mode,contract,exchange,lot,stop,target,risk,status,setup) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (pid,stamp(),s['symbol'],cfg['mode'],s['contract'],s['exchange'],s['lot'],s['stop'],s['target'],risk,'ENTRY_PENDING',json.dumps(s)))
                if guard and guard.config()['one_trade_per_arm']:
                    cfg['armed']=False;self.put('config',cfg)
                    self.event('CONTROL','One entry attempt used; entries disarmed',s['symbol'])
            p=self.rows('SELECT * FROM desk_positions WHERE id=?',(pid,))[0]
            self.record_trade(pid,'ENTRY_PLAN',s)
            limit=math.ceil(entry/s.get('tick',.05))*s.get('tick',.05)
            self.submit(p,'BUY',qty,round(limit,2),'Qualified direction and risk checks')
        def manage(self,close_all=False):
            cfg=self.config()
            if hasattr(self,'shared_risk'):
                for o in self.rows("SELECT * FROM desk_orders WHERE mode='live' AND side='BUY' AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')"):
                    try:liquidate=self.shared_risk.snapshot('live')['liquidate']
                    except Exception:liquidate=False
                    if liquidate and o['broker_id']:
                        try:self.a.cancel(o['broker_id'])
                        except Exception as e:self.record_trade(o['position_id'],'CANCEL_DEFERRED',dict(reason=str(e)))
            for p in self.active():
                if p['qty']<=0:continue
                if p['id'] in self.mismatches:
                    self.record_observation(p,error='Broker quantity mismatch; automated exit suspended');continue
                q,err=self.a.quote(p['exchange'],p['contract'])
                if err:
                    self.health[p['id']]={'reason':err};self.record_observation(p,error=err);continue
                bid=q['bid'];quote_age=max(0.,(datetime.now(IST)-dt(q['quote_ts'])).total_seconds()) if q.get('quote_ts') else 0.
                self.health[p['id']]=dict(bid=bid,observed_mono=time.monotonic()-quote_age,reason='HOLD · within plan')
                setup=json.loads(p['setup']);sig=self.signals.get(p['symbol'],{})
                reason=None
                try:shared=self.shared_risk.snapshot(p['mode']) if hasattr(self,'shared_risk') else {}
                except Exception:
                    shared={};self.record_trade(p['id'],'RISK_DATA_GAP',dict(note='Shared risk calculation unavailable; local stop/target supervision continues.'))
                if shared.get('liquidate'):reason=shared['reason']
                elif close_all:reason='Manual close all'
                elif bid<=p['stop']:reason='Stop reached'
                elif bid>=p['target']:reason='Target reached'
                elif square_off_due(cfg):reason='Intraday square-off'
                elif cfg.get('max_hold') is not None and (datetime.now(IST)-dt(p['ts'])).total_seconds()>=cfg['max_hold']*60:reason='Maximum holding time'
                elif self.day_pnl(p['mode'])<=-cfg['daily_loss']:reason='Daily loss limit'
                elif sig.get('bar_age',999)<=15 and sig.get('confidence',0)>=cfg['confidence'] and sig.get('direction')!=setup.get('direction') and (not getattr(self,'strict_direction_gate',False) or sig.get('signal_stable') is True):reason='Direction reversed'
                elif not p.get('reduced') and p['qty']>=2*p['lot'] and bid>=p['entry']+max(.01,p['entry']-p['stop'])*cfg['partial_take_r']:reason='Partial profit at planned R'
                self.record_observation(p,q,decision=reason or 'HOLD')
                if reason:
                    self.health[p['id']]['reason']=reason
                    if p['status']!='OPEN':
                        pending=self.rows("SELECT * FROM desk_orders WHERE position_id=? AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')",(p['id'],))
                        for o in pending:
                            if o['mode']=='live' and o['side']=='BUY' and o['broker_id']:self.a.cancel(o['broker_id'])
                        continue
                    price=bid*(1-cfg['slippage_bps']/10000);tick=setup.get('tick',.05)
                    desired=max(p['lot'],(p['qty']//p['lot']//2)*p['lot']) if reason=='Partial profit at planned R' else p['qty']
                    qty=min(desired,int(q['bid_qty']))
                    if qty<=0:continue
                    # Paper simulates only visible bid quantity. Live exits use owned quantity;
                    # exchange partial fills remain pending and are reconciled, not fabricated.
                    self.submit(p,'SELL',qty if p['mode']=='paper' else desired,max(tick,round(math.floor(price/tick)*tick,2)),reason)
        def maybe_train(self):
            if self.train_lock.locked() or self.training.get('status')=='ERROR':return
            model=self.model()
            latest=[self.a.bars(symbol,1) for symbol in self.a.symbols]
            newest=max((dt(rows[-1]['ts']) for rows in latest if rows),default=None)
            enough=sum(max(0,len(self.a.bars(symbol,2000))-203)//3 for symbol in self.a.symbols)>=300
            if not model and enough:self.train()
            elif model and newest and not self.a.market_open() and datetime.now(IST).strftime('%H:%M')>='15:35' and newest>dt(model['ts']):self.train()

        def cycle(self):
            if not self.lock.acquire(False):return
            try:
                self.last_cycle=stamp()
                if not self.a.connected():return
                self.reconcile()
                self.verify_positions()
                if not self.a.market_open():
                    for symbol in self.a.symbols:
                        try:self.signals[symbol]=self.inspect(symbol)
                        except Exception as e:self.signals[symbol]=dict(symbol=symbol,decision='WAIT',reason=str(e))
                    self.maybe_train()
                    self.settle_observations()
                    return
                self.manage(close_all=bool(self.get('close_requested',False)))
                for symbol in self.a.symbols:
                    try:
                        s=self.inspect(symbol);previous=self.signals.get(symbol,{})
                        self.signals[symbol]=s
                        if (s.get('decision'),s.get('reason'))!=(previous.get('decision'),previous.get('reason')):self.event(s['decision'],s['reason'],symbol)
                        self.enter(s)
                    except Exception as e:
                        self.signals[symbol]=dict(symbol=symbol,ts=stamp(),decision='WAIT',reason=str(e));self.event('ERROR',str(e),symbol)
                self.maybe_train()
                self.settle_observations()
                self.manage(close_all=bool(self.get('close_requested',False)))
                if self.get('close_requested') and not self.active():self.put('close_requested',False)
            except Exception as e:self.event('ERROR','Cycle: '+str(e));self.put('halt','Engine error · '+str(e))
            finally:self.lock.release()
        def start(self):
            if self.started:return
            self.started=True
            def run():
                while not self.stop_event.is_set():self.cycle();self.stop_event.wait(15)
            threading.Thread(target=run,daemon=True,name='ai-desk-v2').start()
        def control(self,action,body):
            with self.lock:
                cfg=self.config()
                if action=='live-override':
                    enabled=body.get('enabled')
                    if not isinstance(enabled,bool):raise ValueError('Override enabled must be true or false')
                    if enabled and body.get('ack') is not True:raise ValueError('Manual live override requires explicit acknowledgement')
                    cfg.update(live_override=enabled,armed=False)
                    self.event('LIVE_OVERRIDE','Enabled' if enabled else 'Disabled')
                elif action=='mode':
                    if body.get('mode') not in ('paper','live'):raise ValueError('Invalid execution mode')
                    if self.active():raise ValueError('Close active positions before switching execution mode')
                    if body['mode']=='live' and body.get('ack') is not True:raise ValueError('Acknowledge live mode selection; arming is separate')
                    cfg.update(mode=body['mode'],armed=False)
                elif action=='arm':
                    if hasattr(self,'shared_risk'):
                        blocked=self.shared_risk.snapshot(cfg['mode'])['entry_block']
                        if blocked:raise ValueError(blocked)
                    if not self.a.connected():raise ValueError('Connect Kite first')
                    if self.get('halt'):raise ValueError(self.get('halt'))
                    if not self.model():raise ValueError('Train a model first')
                    if cfg['mode']=='live' and (not (cfg.get('live_override') is True or self.evidence()['ready']) or body.get('ack') is not True):raise ValueError('Live evidence or manual override / acknowledgement missing')
                    if self.a.legacy_open():raise ValueError('Legacy positions must be reconciled and closed first')
                    cfg['armed']=True
                elif action=='disarm':cfg['armed']=False
                elif action=='close':
                    cfg['armed']=False;self.put('close_requested',True)
                    self.put('config',cfg)
                    if self.a.connected():
                        self.reconcile()
                        self.verify_positions()
                        for o in self.rows("SELECT * FROM desk_orders WHERE mode='live' AND side='BUY' AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')"):
                            if o['broker_id']:self.a.cancel(o['broker_id'])
                    if self.a.connected() and self.a.market_open():self.manage(close_all=True)
                elif action=='clear-halt':
                    if self.rows("SELECT id FROM desk_orders WHERE mode='live' AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')"):raise ValueError('Broker orders still unresolved')
                    self.verify_positions()
                    if self.mismatches:raise ValueError('Broker positions still differ from the desk ledger')
                    self.put('halt',None);cfg['armed']=False
                else:raise ValueError('Unknown action')
                self.put('config',cfg);self.event('CONTROL',action);return cfg
        def trade_review(self,pid):
            rows=self.rows('SELECT * FROM desk_positions WHERE id=?',(pid,))
            if not rows:raise ValueError('Trade not found')
            p=rows[0];setup=json.loads(p.get('setup') or '{}')
            orders=self.rows('SELECT * FROM desk_orders WHERE position_id=? ORDER BY ts,id',(pid,))
            bought=sum(o['filled'] for o in orders if o['side']=='BUY');sold=sum(o['filled'] for o in orders if o['side']=='SELL')
            buy_value=sum(o['notional'] for o in orders if o['side']=='BUY');sell_value=sum(o['notional'] for o in orders if o['side']=='SELL')
            entry=buy_value/bought if bought else (p.get('entry') or None);exit_price=sell_value/sold if sold else None
            stored_entry_only=not bought and bool(entry)
            exit_record=self.rows('SELECT ts,payload FROM desk_exit_reviews WHERE position_id=? ORDER BY id DESC LIMIT 1',(pid,))
            recorded_reason=p.get('exit_reason') or next((o.get('reason') for o in reversed(orders) if o['side']=='SELL' and o['filled'] and o.get('reason')),None)
            if not recorded_reason and exit_record:recorded_reason=json.loads(exit_record[0]['payload']).get('reason')
            if not recorded_reason:recorded_reason='Exit reason was not saved in this historical record' if p['status']=='CLOSED' else 'No completed exit recorded'
            explanations=[]
            if p['mode']=='paper':explanations.append('Paper trade: fills and P&L are simulated, not broker executions.')
            if stored_entry_only:explanations.append('A stored entry price and position result exist, but entry-order fills are missing. This trade cannot be reconciled to broker fills from this database alone.')
            elif not bought:explanations.append('No entry fill is recorded; this is an unfilled request, not a completed losing trade.')
            if sold and entry:
                change=(exit_price/entry-1)*100
                explanations.append(f'Average option exit premium was {change:+.2f}% relative to average entry. Realized P&L is derived from confirmed fills; estimated fees are deducted separately.')
            explanations.append('Recorded exit trigger: '+recorded_reason)
            explanations.append('The model estimates underlying direction, not the probability of an option trade being profitable.')
            counts=self.rows('SELECT kind,COUNT(*) n FROM desk_trade_events WHERE position_id=? GROUP BY kind',(pid,))
            coverage={r['kind']:r['n'] for r in counts}
            limitation='Recorded observations are samples from existing supervision, not every exchange tick. Underlying signal prices may be from older candles (see bar_ts). News, continuous IV and auction imbalance are not recorded; market causes cannot be proven. Offline periods and gaps cannot be reconstructed. The directional model is not retrained directly from trade P&L; separate success models may learn from completed paper outcomes when their validation gates pass.'
            if not coverage.get('ENTRY_PLAN'):limitation+=' Historical trade: no event recorder entry record; only previously stored evidence is available.'
            if self.recorder_error:limitation+=' '+self.recorder_error
            observations=self.rows("SELECT MIN(CAST(json_extract(payload,'$.quote.bid') AS REAL)) low, MAX(CAST(json_extract(payload,'$.quote.bid') AS REAL)) high, MAX(CAST(json_extract(payload,'$.quote.spread_pct') AS REAL)) widest_spread FROM desk_trade_events WHERE position_id=? AND kind='OBSERVATION'",(pid,))[0]
            drop=self.rows("""WITH samples AS (SELECT ts,CAST(json_extract(payload,'$.quote.bid') AS REAL) bid,
                LAG(CAST(json_extract(payload,'$.quote.bid') AS REAL)) OVER (ORDER BY id) previous_bid,
                LAG(ts) OVER (ORDER BY id) previous_ts FROM desk_trade_events WHERE position_id=? AND kind='OBSERVATION')
                SELECT ts,previous_ts,bid,previous_bid,bid-previous_bid change FROM samples WHERE previous_bid>0 ORDER BY change LIMIT 1""",(pid,))
            opposite=self.rows("SELECT ts,json_extract(payload,'$.signal.direction') direction,json_extract(payload,'$.signal.bar_ts') bar_ts FROM desk_trade_events WHERE position_id=? AND kind='OBSERVATION' AND json_extract(payload,'$.signal.direction') IS NOT NULL AND json_extract(payload,'$.signal.direction')!=? ORDER BY id LIMIT 1",(pid,setup.get('direction','')))
            changes=dict(largest_sampled_bid_drop=drop[0] if drop and drop[0]['change']<0 else None,first_signal_different_from_entry=opposite[0] if opposite else None)
            if changes['largest_sampled_bid_drop']:
                v=changes['largest_sampled_bid_drop'];explanations.append(f"Largest recorded bid decline between samples: {v['previous_bid']:.2f} to {v['bid']:.2f}, between {v['previous_ts']} and {v['ts']}. Unobserved moves between samples are unknown.")
            if opposite:explanations.append('A monitored direction signal differed from the entry plan at '+opposite[0]['ts']+'; its source candle time is recorded separately. This alone does not establish the cause of loss.')
            if observations['low'] is not None:
                explanations.append(f"Observed option bids ranged from {observations['low']:.2f} to {observations['high']:.2f}; these are sampled prices, not guaranteed intratrade extremes.")
            if not setup.get('risk_config_at_entry'):limitation+=' Historical risk settings were not captured for this trade; current settings must not be substituted.'
            direction=setup.get('direction')
            if direction:
                expected='The saved signal expected the underlying to move '+str(direction)+'. This was a direction forecast, not a guarantee of option profit.'
            elif str(p.get('contract','')).endswith('PE'):
                expected='The recorded contract is a put: the position has bearish exposure. This is inferred from the contract; the original model forecast was not saved.'
            elif str(p.get('contract','')).endswith('CE'):
                expected='The recorded contract is a call: the position has bullish exposure. This is inferred from the contract; the original model forecast was not saved.'
            else:expected='The original entry thesis was not saved in this historical record.'
            if setup.get('entry') is not None:expected+=f" Planned premium {setup['entry']}; planned stop {setup.get('stop','not recorded')}; planned target {setup.get('target','not recorded')}."
            net=p['pnl']-p['fees']
            actual=f"Stored realized result: {p['pnl']:+.2f}; estimated costs: {p['fees']:.2f}; net realized: {net:+.2f}. Status: {p['status']}."
            if entry:actual+=f" Average entry premium available: {entry:.2f}"+(' (position ledger only).' if stored_entry_only else ' (order fills).')
            if exit_price:actual+=f" Average exit premium: {exit_price:.2f}."
            interpretations={'Stop reached':'The engine recorded its stop condition. This explains the exit decision; it does not establish whether the underlying, volatility or liquidity caused the premium decline.',
                'Target reached':'The engine recorded its target condition and requested an exit.',
                'Maximum holding time':'The holding-time setting triggered the exit. A timed exit can close either a profit or a loss.',
                'Intraday square-off':'The configured square-off time triggered the exit; this was not necessarily a stop or target.',
                'Manual close all':'A manual close-all request triggered the exit.',
                'Direction reversed':'The monitored direction signal changed enough to meet the reversal-exit rule.',
                'Partial profit at planned R':'The engine requested a partial profit exit; remaining quantity is recorded separately.'}
            why=interpretations.get(recorded_reason,recorded_reason)
            changed=' '.join(x for x in explanations if x.startswith(('Largest recorded','A monitored direction')))
            if not changed:changed='No recorded price/signal transition establishes what suddenly changed.'+(' No during-trade observations were saved for this historical trade.' if not coverage.get('OBSERVATION') else ' Available samples cannot establish the market cause.')
            summary=dict(expected=expected,actual=actual,exit_decision=why,what_changed=changed,
                evidence_quality='Recorded entry and sampled observations available' if coverage.get('ENTRY_PLAN') and coverage.get('OBSERVATION') else 'Partial historical evidence; unavailable fields are marked Not recorded',
                learning='Review records do not automatically retrain the model from this trade’s profit or loss.')
            explanations=[expected,actual,why,changed]+explanations
            timeline=self.trade_timeline(pid)
            if not timeline['events'] and orders:
                timeline=dict(events=[dict(id='legacy-'+str(i),ts=o['ts'],kind='LEGACY_ORDER_RECORD',data=dict(side=o['side'],qty=o['qty'],filled=o['filled'],status=o['status'],reason=o.get('reason') or 'Not recorded',note='Historical order snapshot. This timestamp is the submission time, not a reconstructed fill timestamp.')) for i,o in enumerate(orders)],next_after=None)
            return dict(position={k:v for k,v in p.items() if k!='setup'},
                entry_plan={k:setup.get(k) for k in ('ts','bar_ts','bar_age','direction','p_up','confidence','model_id','regime','spot','contract','entry','stop','target','qty','lots','max_lots','requested_lots','spread','reason','risk_config_at_entry','shared_signal_key','shared_risk_at_entry')},
                outcome=dict(bought=bought,sold=sold,remaining=p['qty'],average_entry=entry,average_exit=exit_price,
                    realized=p['pnl'],estimated_costs=p['fees'],net_realized=p['pnl']-p['fees']),
                review_summary=summary,recorded_exit_reason=recorded_reason,explanations=explanations,limitations=limitation,coverage=coverage,observed_range=observations,observed_changes=changes,timeline=timeline,recorder_error=self.recorder_error,
                orders=[{k:o.get(k) for k in ('ts','side','qty','filled','notional','limit_price','status','broker_id','reason','error')} for o in orders],
                exit_observations=[dict(ts=r['ts'],snapshot=json.loads(r['payload'])) for r in self.rows('SELECT ts,payload FROM desk_exit_reviews WHERE position_id=? ORDER BY id',(pid,))])
        def state(self):
            cfg=self.config();model=self.model();evidence=self.evidence();active=self.active()
            for p in active:
                p.update(self.health.get(p['id'],{}));p['unrealized']=(p['bid']-p['entry'])*p['qty'] if p.get('bid') else None;p.pop('setup',None)
            obs=self.rows('SELECT COUNT(*) total,SUM(label IS NOT NULL) settled FROM desk_observations')[0]
            signals=[]
            for symbol in self.a.symbols:
                s=dict(self.signals.get(symbol,dict(symbol=symbol,decision='WAIT',reason='Waiting for first market scan')));s.pop('features',None);signals.append(s)
            if model:model={k:v for k,v in model.items() if k not in ('mu','sd','w','bias')}
            online=self.get('online_model');online={k:online.get(k) for k in ('samples','accuracy','auc','baseline','brier','validation_samples','ts')} if online else None
            trades=self.rows("SELECT id,ts,symbol,mode,contract,qty,entry,status,exit_ts,pnl,fees,risk,exit_reason FROM desk_positions ORDER BY ts DESC LIMIT 100")
            for trade in trades:
                if not trade.get('exit_reason'):
                    last=self.rows("SELECT reason FROM desk_orders WHERE position_id=? AND side='SELL' AND filled>0 AND reason IS NOT NULL ORDER BY ts DESC LIMIT 1",(trade['id'],))
                    trade['exit_reason']=last[0]['reason'] if last else ('Not recorded · open Trade review' if trade['status']=='CLOSED' else 'No completed exit recorded')
            return dict(version='2.0',recorder_error=self.recorder_error,timestamp=stamp(),connected=self.a.connected(),backend_instance=str(os.getpid()),market_open=self.a.market_open(),config=cfg,
                block=self.block(),last_cycle=self.last_cycle,training=self.training,model=model,online=online,evidence=evidence,
                observations=obs,signals=signals,positions=active,trades=trades,
                orders=self.rows('SELECT * FROM desk_orders ORDER BY ts DESC LIMIT 60'),
                events=self.rows('SELECT * FROM desk_events ORDER BY id DESC LIMIT 70'),
                pnl=dict(paper=round(self.day_pnl('paper'),2),live=round(self.day_pnl('live'),2)),halt=self.get('halt'),legacy=self.a.legacy_open())

    return Engine

DeskEngine = _build_desk_engine_class()


class DeskAdapter:
    symbols = tuple(AI_SYMBOLS)
    @contextmanager
    def db(self):
        c=ai_db()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:c.close()
    def connected(self):return bool(require_session())
    def market_open(self):return ai_market_open()
    def exchange(self,symbol):return INDEX_OPTION_EXCHANGE.get(symbol,"NFO")
    def bars(self,symbol,n):return [dict(r) for r in _get_recent_index_bars(symbol,n)]
    def features(self,rows,i,arrays):return compute_index_features(rows,i,arrays)
    def refresh(self,symbol):
        if self.connected():ai_refresh_live_bars(symbol)
    def chain(self,symbol):return get_chain_for_symbol(symbol)
    def quote(self,exchange,contract):
        try:
            key=f"{exchange}:{contract}"
            q=kite_quote_bulk([key],force_refresh=True).get(key)
            if not q:return None,"Missing option quote"
            ts=_ai_ts_naive(q.get("timestamp"))
            if ts is None:return None,"Quote has no exchange timestamp"
            age=(now_ist()-ts).total_seconds()
            if age < -5 or age > 60:return None,f"Stale option quote ({age:.0f}s)"
            st=quote_stats(q)
            if not st.get("bid") or not st.get("ask") or st["bid"]>st["ask"]:return None,"No executable bid/ask"
            if not all(math.isfinite(float(st[k])) for k in ("bid","ask")):return None,"Invalid option prices"
            st.update(quote_ts=ts.isoformat(),source="Kite REST; timestamp IST")
            return st,None
        except Exception as e:return None,str(e)
    def place(self,exchange,contract,side,qty,price,tag):
        # Bounded LIMIT order. Accepted is not filled; v2 reconciles broker results.
        return kite.place_order(variety="regular",exchange=exchange,tradingsymbol=contract,
            transaction_type=side,quantity=int(qty),product="MIS",order_type="LIMIT",
            validity="DAY",price=float(price),tag=tag)
    def orders(self):return kite.orders()
    def order_history(self,order_id):return kite.order_history(order_id)
    def trades(self):return kite.trades()
    def holdings(self):return kite.positions().get("net",[])
    def cancel(self,order_id):return kite.cancel_order(variety="regular",order_id=order_id)
    def legacy_open(self):return []

ai_init_db()
def acquire_desk_process_lock():
    # Hold one OS file lock for the entire process; never run multiple traders.
    global _DESK_PROCESS_LOCK
    _DESK_PROCESS_LOCK=open(AI_DB_FILE+".engine.lock","a+")
    try:
        if os.name=="nt":
            import msvcrt
            _DESK_PROCESS_LOCK.seek(0);_DESK_PROCESS_LOCK.write("0");_DESK_PROCESS_LOCK.flush();_DESK_PROCESS_LOCK.seek(0)
            msvcrt.locking(_DESK_PROCESS_LOCK.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(_DESK_PROCESS_LOCK,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        raise RuntimeError("Another AI backend owns this database. Use one process; no preload/reloader.")

if os.environ.get("AI_START_WORKERS","1")=="1":acquire_desk_process_lock()
if AI_HIST_INTERVAL != "5minute":raise RuntimeError("AI Desk v2 requires AI_HIST_INTERVAL=5minute")
# Prevent separate worker processes from independently spending the same risk budget
# and each issuing a full allocation of Kite requests against the same ledgers.
def _claim_ai_worker_lease():
    if os.environ.get('AI_START_WORKERS','1')!='1':return None
    path=os.path.abspath(AI_DB_FILE)+'.worker.lock'
    handle=open(path,'a+b')
    try:
        if os.name=='nt':
            import msvcrt
            handle.seek(0,2)
            if handle.tell()==0:handle.write(b'0');handle.flush()
            handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        raise SystemExit('Another AI backend worker owns these ledgers. Run one backend process; shared risk and broker throttling require a single owner.')

_AI_WORKER_LEASE=_claim_ai_worker_lease()

# STOCK OPTIONS AI — isolated ledger/models; same validated desk execution core.
# Full F&O directional monitoring, then bounded deep option-chain/liquidity checks.
# API reference: https://kite.trade/docs/connect/v3/market-quotes/
# API reference: https://kite.trade/docs/connect/v3/historical/
# ===========================================================================
STOCK_FULL_SCAN_DEEP_N = 24
STOCK_FULL_REFRESH_BATCH = 28
STOCK_HISTORY_REFRESH_SECONDS = 210
STOCK_HISTORY_PACE_SECONDS = 0.36
# On the first Kite connection the Stock AI begins a resumable deep backfill in
# the background. One calendar year is long enough to expose the directional and
# setup-success models to multiple regimes while keeping the local archive and
# broker request volume bounded. Override with STOCK_HISTORY_BACKFILL_DAYS if a
# different retention target is required.
STOCK_HISTORY_BACKFILL_DAYS = max(90, int(os.environ.get('STOCK_HISTORY_BACKFILL_DAYS', '365')))
STOCK_HISTORY_BACKFILL_CHUNK_DAYS = max(15, min(60, int(os.environ.get('STOCK_HISTORY_BACKFILL_CHUNK_DAYS', '60'))))
STOCK_HISTORY_BACKFILL_PACE_SECONDS = max(0.36, float(os.environ.get('STOCK_HISTORY_BACKFILL_PACE_SECONDS', '0.40')))
STOCK_HISTORY_BACKFILL_RETRY_SECONDS = max(60, int(os.environ.get('STOCK_HISTORY_BACKFILL_RETRY_SECONDS', '900')))
STOCK_AI_BUILD = 'equity-universe-v6-patterns-20260916'
STOCK_FULL_SCAN_BAR_MAX_AGE = 15
# Stock-only admission gates. These do not alter the independent Index AI desk.
STOCK_DIRECTION_MIN_AUC = 0.52
STOCK_DIRECTION_MAX_ACC_DEFICIT = 2.0
STOCK_DIRECTION_MAX_BRIER = 0.26
STOCK_STABILITY_MIN_CONFIDENCE = 0.53
STOCK_STRICT_DEFAULTS = dict(
    min_success_prob=0.55,      # validated/blended setup probability required for READY
    min_expected_r=0.15,       # after estimated fees + one-way exit slippage
    fallback_confidence=0.64,   # stricter direction gate until success models validate
    stability_bars=2,           # reversal-exit confirmation bars only; NOT an entry gate
)
STOCK_SUCCESS_MIN_SAMPLES = 40
STOCK_SUCCESS_MIN_AUC = 0.52
STOCK_SUCCESS_MAX_BRIER = 0.26
STOCK_SUCCESS_FEATURE_VERSION = 2
STOCK_PATTERN_FEATURE_VERSION = 1
STOCK_HIST_SUCCESS_FEATURE_VERSION = 2
STOCK_HIST_SUCCESS_MIN_SAMPLES = 300
STOCK_HIST_SUCCESS_MIN_AUC = 0.52
STOCK_HIST_SUCCESS_MAX_BRIER = 0.26
STOCK_HIST_SUCCESS_MAX_SAMPLES = 24000
STOCK_HIST_SUCCESS_PER_SYMBOL = 160
STOCK_HIST_SUCCESS_RETRY_SECONDS = 900
STOCK_HIST_MIN_ARCHIVE_COVERAGE = max(0.50, min(1.0, float(os.environ.get('STOCK_HIST_MIN_ARCHIVE_COVERAGE', '0.70'))))
STOCK_DIRECTION_FEATURES = ['ret_1','ret_3','ret_5','ret_10','ret_20','ema_gap_9_20',
    'ema_gap_20_50','rsi14','volatility_10','range_20','volume_ratio','trend_score','momentum_score','time_sin','time_cos']
STOCK_PATTERN_FEATURES = [
    'range_pos_20','range_pos_50','breakout_20','breakout_50','vwap_distance',
    'opening_range_position','gap_pct','volume_accel','compression_ratio','momentum_accel',
    'day_position','structure_6','abnormal_volume'
]
STOCK_HIST_SUCCESS_FEATURES = [
    'direction_confidence','ret_1_aligned','ret_3_aligned','ret_5_aligned','ret_20_aligned',
    'ema_9_20_aligned','ema_20_50_aligned','rsi_alignment','trend_alignment','momentum_alignment',
    'volatility_10','range_20','volume_ratio','time_sin','time_cos','stop_barrier_pct','reward_r',
    'range_pos_20_aligned','range_pos_50_aligned','breakout_20_aligned','breakout_50_aligned',
    'vwap_distance_aligned','opening_range_aligned','gap_aligned','volume_accel','compression_ratio',
    'momentum_accel_aligned','day_position_aligned','structure_6_aligned','abnormal_volume'
]
STOCK_SUCCESS_FEATURES = [
    'direction_confidence','historical_strength','online_strength','model_agreement',
    'trend_score','momentum_score','volatility_10','range_20','volume_ratio','regime_alignment',
    'spread_quality','depth_ratio','delta_quality','log_option_volume','log_option_oi',
    'dte_scaled','bar_freshness','reward_r','stop_pct_scaled','cost_r'
]

def _stock_success_sigmoid(x):
    return 1.0/(1.0+np.exp(-np.clip(x,-30,30)))

def _stock_success_fit(X,Y):
    X=np.asarray(X,float);Y=np.asarray(Y,float)
    mu=X.mean(0);sd=np.maximum(X.std(0),1e-5);z=np.clip((X-mu)/sd,-6,6)
    w=np.zeros(X.shape[1]);b=0.0
    for _ in range(260):
        err=_stock_success_sigmoid(z@w+b)-Y
        w-=.05*(z.T@err/len(Y)+.03*w);b-=.05*err.mean()
    return dict(mu=mu.tolist(),sd=sd.tolist(),w=w.tolist(),bias=float(b))

def _stock_success_predict(model,X):
    z=np.clip((np.asarray(X,float)-np.asarray(model['mu']))/np.asarray(model['sd']),-6,6)
    return _stock_success_sigmoid(z@np.asarray(model['w'])+float(model['bias']))

def _stock_direction_predict(model,X):
    # Same standardized logistic form used by the shared directional desk model.
    z=np.clip((np.asarray(X,float)-np.asarray(model['mu']))/np.asarray(model['sd']),-6,6)
    return float(_stock_success_sigmoid(z@np.asarray(model['w'])+float(model['bias'])))

def _stock_success_auc(y,p):
    y=np.asarray(y,int);p=np.asarray(p,float);a=int(y.sum());b=len(y)-a
    if not a or not b:return None
    order=np.argsort(p);ranks=np.empty(len(p),float);i=0
    while i<len(p):
        j=i+1
        while j<len(p) and p[order[j]]==p[order[i]]:j+=1
        ranks[order[i:j]]=(i+1+j)/2;i=j
    return float((ranks[y==1].sum()-a*(a+1)/2)/(a*b))

class StockDeskAdapter(DeskAdapter):
    extra_feature_names=tuple(STOCK_PATTERN_FEATURES)
    pattern_feature_version=STOCK_PATTERN_FEATURE_VERSION
    def extra_features(self,rows,i,arrays):return _stock_pattern_features(rows,i,arrays)
    def __init__(self):
        self.symbols=()
        self.refreshed={}
        self.history_lock=threading.Lock()
    @contextmanager
    def db(self):
        c=sqlite3.connect(os.path.join(os.path.dirname(__file__),'stock_options_ai.db'),timeout=30)
        c.row_factory=sqlite3.Row
        try:
            c.execute('PRAGMA journal_mode=WAL')
            yield c
            c.commit()
        except Exception:
            c.rollback();raise
        finally:c.close()
    def refresh(self,symbol):
        # Fast tail refresh used by the live scanner. Deep history is collected by the
        # independent resumable backfill worker below so a one-year archive never blocks
        # entry execution or the 5-second position supervisor.
        with self.history_lock:
            if time.monotonic()-self.refreshed.get(symbol,-1e9)<STOCK_HISTORY_REFRESH_SECONDS:return
            token,err=resolve_token_for_symbol(symbol)
            if err:raise ValueError(err)
            end=now_ist()
            recent=self.bars(symbol,1)
            start=end-timedelta(days=20)
            if recent:
                last=_ai_ts_naive(recent[-1]['ts'])
                if last:start=max(start,last-timedelta(minutes=10))
            candles,err=_hist_fetch_chunk(token,start,end)
            if err:raise ValueError(str(err))
            _hist_insert_candles(symbol,candles)
            self.refreshed[symbol]=time.monotonic()
    def history_bounds(self,symbol):
        c=ai_db()
        try:
            r=c.execute("SELECT COUNT(*) n,MIN(ts) first_ts,MAX(ts) last_ts FROM ai_hist_candles WHERE symbol=? AND interval=?",(symbol,AI_HIST_INTERVAL)).fetchone()
            return dict(n=int(r['n'] or 0),first_ts=r['first_ts'],last_ts=r['last_ts']) if r else dict(n=0,first_ts=None,last_ts=None)
        finally:c.close()
    def backfill_history(self,symbol,target_days=STOCK_HISTORY_BACKFILL_DAYS,progress=None,pause_check=None):
        """Resumably fill older 5-minute stock history, newest missing chunk first.

        This intentionally uses the shared historical-data request gate. It never holds
        the live refresh lock across the long backfill and persists every successful chunk
        immediately, so a disconnect/restart simply resumes from the oldest stored candle.
        """
        token,err=resolve_token_for_symbol(symbol)
        if err:raise ValueError(err)
        now=now_ist();target_start=now-timedelta(days=int(target_days));bounds=self.history_bounds(symbol)
        first=_ai_ts_naive(bounds.get('first_ts')) if bounds.get('first_ts') else None
        # Work backwards so the most recent missing months become usable first.
        cursor_end=(first-timedelta(minutes=1)) if first else now
        if cursor_end<=target_start:
            return dict(symbol=symbol,status='COMPLETE',rows_added=0,chunks=0,bounds=bounds)
        added_total=0;chunks=0;fetched_total=0;error=None;max_available=False
        while cursor_end>target_start:
            if pause_check:
                reason=pause_check()
                if reason:
                    bounds=self.history_bounds(symbol)
                    return dict(symbol=symbol,status='PAUSED_LIVE_PRIORITY',rows_added=added_total,fetched=fetched_total,
                        chunks=chunks,error=None,bounds=bounds,target_start=str(target_start),pause_reason=str(reason))
            start=max(target_start,cursor_end-timedelta(days=STOCK_HISTORY_BACKFILL_CHUNK_DAYS))
            candles,fetch_err=_hist_fetch_chunk(token,start,cursor_end)
            if fetch_err:
                error=str(fetch_err);break
            # A legal historical request that returns no candles immediately before an
            # already-populated archive means Kite has no older data for this current
            # instrument/symbol.  Treat that as MAX_AVAILABLE rather than retrying forever.
            # We require an existing usable archive so a totally empty/mis-mapped symbol
            # is still treated as an error and remains visible for investigation.
            if not candles:
                current=self.history_bounds(symbol)
                if int(current.get('n') or 0)>=240 and current.get('first_ts'):
                    max_available=True
                    break
                error='No historical candles returned for current instrument';break
            added=_hist_insert_candles(symbol,candles);added_total+=added;fetched_total+=len(candles);chunks+=1
            if progress:
                try:progress(dict(symbol=symbol,start=str(start),end=str(cursor_end),rows_added=added_total,chunks=chunks))
                except Exception:pass
            # Do not skip across a failed window: next run resumes exactly at the oldest
            # successfully persisted boundary.
            cursor_end=start-timedelta(seconds=1)
            time.sleep(STOCK_HISTORY_BACKFILL_PACE_SECONDS)
        bounds=self.history_bounds(symbol)
        first=_ai_ts_naive(bounds.get('first_ts')) if bounds.get('first_ts') else None
        complete=bool(first and first<=target_start+timedelta(days=3))
        # If every legal older-history window down to the target was checked without an
        # API error, but the archive still cannot reach the one-year boundary, the current
        # instrument simply has no more usable older candles.  Resolve it as MAX_AVAILABLE
        # rather than retrying the same zero-progress windows forever.  Genuine request/
        # token errors remain ERROR/PARTIAL and are surfaced in the UI for investigation.
        exhausted_without_error=bool(error is None and cursor_end<=target_start and int(bounds.get('n') or 0)>=240 and first)
        if not complete and exhausted_without_error:max_available=True
        status='COMPLETE' if complete else 'MAX_AVAILABLE' if max_available else ('PARTIAL' if chunks else 'ERROR')
        return dict(symbol=symbol,status=status,rows_added=added_total,fetched=fetched_total,chunks=chunks,error=error,
            bounds=bounds,target_start=str(target_start),available_from=str(first) if first else None)
    def chain(self,symbol):
        exchange,opts=get_option_instruments_for_symbol(symbol)
        today=now_ist().date()
        expiries=sorted({o['expiry'] for o in opts if (o['expiry']-today).days>=MIN_DAYS_TO_EXPIRY})
        if not expiries:return None,f'No stock expiry at least {MIN_DAYS_TO_EXPIRY} calendar days away'
        data,err=get_chain_for_symbol(symbol,str(expiries[0]))
        if err:return None,err
        data=dict(data)
        data['chain']=[o for o in data['chain'] if float(o.get('volume') or 0)>=max(1,int(o.get('lot_size') or 1))*10 and float(o.get('oi') or 0)>=max(1,int(o.get('lot_size') or 1))*20]
        return data,None

class StockDeskEngine(DeskEngine):
    strict_direction_gate=True
    direction_min_auc=STOCK_DIRECTION_MIN_AUC
    direction_max_acc_deficit=STOCK_DIRECTION_MAX_ACC_DEFICIT
    direction_max_brier=STOCK_DIRECTION_MAX_BRIER
    stability_min_confidence=STOCK_STABILITY_MIN_CONFIDENCE
    def config(self):
        cfg=super().config()
        for k,v in STOCK_STRICT_DEFAULTS.items():cfg.setdefault(k,v)
        return cfg
    def save_config(self,body):
        body=dict(body or {})
        extra_keys=set(STOCK_STRICT_DEFAULTS)
        extras={k:body.pop(k) for k in list(body) if k in extra_keys}
        parsed={}
        bounds=dict(min_success_prob=(.50,.80),min_expected_r=(0.,1.5),fallback_confidence=(.58,.90),stability_bars=(1,3))
        for k,v in extras.items():
            if isinstance(v,bool):raise ValueError('Invalid '+k)
            v=float(v);lo,hi=bounds[k]
            if not math.isfinite(v) or not lo<=v<=hi:raise ValueError(f'{k}: expected {lo} to {hi}')
            if k=='stability_bars' and not v.is_integer():raise ValueError(k+' must be a whole number')
            parsed[k]=int(v) if k=='stability_bars' else v
        cfg=super().save_config(body) if body else super().config()
        for k,v in STOCK_STRICT_DEFAULTS.items():cfg.setdefault(k,v)
        cfg.update(parsed)
        self.put('config',cfg)
        if parsed and not body:self.event('SETTINGS','Stock AI quality gates updated')
        return cfg
    def __init__(self,adapter):
        super().__init__(adapter)
        self.a.symbols=tuple(self.get('stock_universe',[]))
        self.scan_status={'status':'IDLE','completed':0,'total':0,'deep_total':0,'deep_completed':0}
        self.ban_checked=0;self.banned=None;self.ban_error='Ban list not checked yet'
        self.risk_lock=threading.Lock();self.entry_lock=threading.Lock();self.candidate_lock=threading.Lock()
        self.execution_event=threading.Event();self.execution_candidates=[];self.execution_generation=0
        self.execution_status={'status':'IDLE'};self.refresh_cursor=0
        self.success_lock=threading.Lock();self.success_model_cache=None;self.success_model_cache_at=0.0
        self.hist_success_lock=threading.Lock();self.hist_success_model_cache=None;self.hist_success_model_cache_at=0.0
        self.hist_success_thread=None;self.hist_success_last_attempt=0.0
        self.history_backfill_thread=None;self.history_backfill_last_attempt=0.0;self.history_backfill_universe=()
        self.history_backfill_status=dict(status='WAITING FOR KITE',target_days=STOCK_HISTORY_BACKFILL_DAYS,completed=0,total=0,rows_added=0)
    def _full_mode(self):
        return self.get('stock_universe_info',{}).get('mode')!='Custom watchlist'
    def select_universe(self,body=None):
        body=body or {}
        if self.config()['armed']:raise ValueError('Disarm entries before changing the scan universe')
        universe=fo_stock_universe()
        selected=body.get('symbols',[])
        if not isinstance(selected,list) or any(not isinstance(x,str) for x in selected):raise ValueError('symbols must be a list of stock symbols')
        if selected:
            selected=list(dict.fromkeys(x.strip().upper() for x in selected if x.strip()))
            if not 1<=len(selected)<=60 or any(x not in universe for x in selected):raise ValueError('Choose 1–60 current F&O stocks; indices are excluded')
            mode='Custom watchlist'
        else:
            selected=list(universe);mode='Full F&O universe'
        if not selected:raise ValueError('No current F&O stocks found. Connect Kite and refresh instruments.')
        self.a.symbols=tuple(selected)
        self.put('stock_universe',selected)
        self.put('stock_universe_info',dict(mode=mode,total_fo=len(universe),selected=len(selected),deep_check=STOCK_FULL_SCAN_DEEP_N,
            refresh_batch=STOCK_FULL_REFRESH_BATCH,updated=now_ist().isoformat()))
        self.signals={k:v for k,v in self.signals.items() if k in selected}
        self.refresh_cursor=0
        self.event('UNIVERSE',f'{mode}: monitoring {len(selected)} stocks; deep option checks top {min(STOCK_FULL_SCAN_DEEP_N,len(selected))}')
        return selected
    def _sync_full_universe_membership(self):
        if not self._full_mode():return
        universe=fo_stock_universe()
        if tuple(universe)==tuple(self.a.symbols):return
        self.a.symbols=tuple(universe);self.put('stock_universe',universe)
        info=self.get('stock_universe_info',{});info.update(mode='Full F&O universe',total_fo=len(universe),selected=len(universe),
            deep_check=STOCK_FULL_SCAN_DEEP_N,refresh_batch=STOCK_FULL_REFRESH_BATCH,updated=now_ist().isoformat())
        self.put('stock_universe_info',info);self.signals={k:v for k,v in self.signals.items() if k in universe}
        self.event('UNIVERSE',f'F&O membership refreshed: {len(universe)} stocks')
    @staticmethod
    def _pattern_score(s):
        pf=s.get('pattern_features') if isinstance(s.get('pattern_features'),dict) else {}
        if not pf:return 50.0
        direction=1. if s.get('direction')=='UP' else -1.
        def val(k,default=0.):
            try:
                x=float(pf.get(k,default));return x if math.isfinite(x) else default
            except Exception:return default
        def aligned(k,scale):
            return .5+.5*math.tanh(direction*val(k)/max(1e-6,float(scale)))
        volumeq=max(0.,min(1.,(val('volume_accel',1.)-.75)/1.75))
        abnormalq=max(0.,min(1.,(val('abnormal_volume',1.)-.75)/2.50))
        expansionq=max(0.,min(1.,(val('compression_ratio',1.)-.55)/1.45))
        breakout=max(0.,direction*val('breakout_20'),.75*direction*val('breakout_50'))
        breakoutq=max(0.,min(1.,breakout/.75))
        score=(16*aligned('range_pos_20',.75)+10*aligned('range_pos_50',.85)+
               14*aligned('vwap_distance',.45)+12*aligned('opening_range_position',1.0)+
               10*aligned('structure_6',.50)+10*aligned('momentum_accel',.45)+
               8*breakoutq+8*volumeq+5*abnormalq+4*aligned('gap_pct',.70)+3*expansionq)
        return round(max(0.,min(100.,score)),2)
    def _annotate_pattern(self,s):
        s['pattern_score']=self._pattern_score(s)
        ps=float(s['pattern_score'])
        s['pattern_profile']='Strong aligned structure' if ps>=70 else 'Aligned structure' if ps>=58 else 'Mixed structure' if ps>=44 else 'Countertrend / noisy structure'
        return s
    def _apply_cross_sectional_strength(self,screened):
        # Relative momentum is computed from the already-scanned F&O universe; no NIFTY,
        # sector or additional quote request is needed. A bearish stock is rewarded for
        # being unusually weak, while a bullish stock is rewarded for unusual strength.
        values=[]
        for sym,sig in screened.items():
            feats=list(sig.get('features') or [])
            if len(feats)<5 or sig.get('p_up') is None:continue
            try:
                sign=1. if sig.get('direction')=='UP' else -1.
                aligned=sign*(float(feats[2])+0.35*float(feats[4]))
                if math.isfinite(aligned):values.append((aligned,sym))
            except Exception:pass
        values.sort(key=lambda x:x[0])
        n=len(values)
        ranks={sym:(50. if n<=1 else 100.*i/(n-1)) for i,(_,sym) in enumerate(values)}
        max_spread=self.config()['max_spread']
        for sym,sig in screened.items():
            sig['relative_strength']=round(float(ranks.get(sym,50.)),1)
            self._annotate_pattern(sig)
            sig['rank_score']=self.rank_signal(sig,max_spread)
        return screened

    @staticmethod
    def rank_signal(s,max_spread):
        if s.get('p_up') is None:return 0.
        confidence=max(0.,min(1.,float(s.get('confidence') or 0)))
        feats=list(s.get('features') or [])
        def feat(i,default=0.):
            try:return float(feats[i]) if math.isfinite(float(feats[i])) else default
            except Exception:return default
        direction=s.get('direction');regime=s.get('regime')
        align=1. if (direction=='UP' and regime=='UPTREND') or (direction=='DOWN' and regime=='DOWNTREND') else .65 if regime=='RANGE' else .20
        hp=s.get('historical_probability');op=s.get('online_probability')
        agreement=.5 if op is None else (1. if hp is not None and (float(hp)>=.5)==(float(op)>=.5) else 0.)
        fresh=max(0.,min(1.,1-float(s.get('bar_age') or STOCK_FULL_SCAN_BAR_MAX_AGE)/STOCK_FULL_SCAN_BAR_MAX_AGE))
        volumeq=max(0.,min(1.,(feat(10,1.)-.5)/1.5))
        momentum=feat(12,12.5);momq=max(0.,min(1.,((momentum-12.5) if direction=='UP' else (12.5-momentum))/12.5+.5))
        patternq=max(0.,min(1.,float(s.get('pattern_score') if s.get('pattern_score') is not None else 50.)/100.))
        relativeq=max(0.,min(1.,float(s.get('relative_strength') if s.get('relative_strength') is not None else 50.)/100.))
        universe_quality=50*confidence+8*align+6*agreement+7*fresh+5*volumeq+5*momq+14*patternq+5*relativeq
        # Validated success evidence dominates once available, even when its gate rejects
        # the trade; this keeps the table sorted by opportunity quality, not READY status.
        if s.get('success_probability') is not None:
            p=max(0.,min(1.,float(s['success_probability'])))
            ev=float(s.get('expected_r') if s.get('expected_r') is not None else -1.)
            ev_quality=max(0.,min(1.,(ev+.25)/1.25))
            spread=max(0.,min(1.,1-float(s.get('spread') or max_spread)/max(.01,max_spread)))
            return round(72*p+16*ev_quality+7*spread+5*universe_quality/100,2)
        # Deep fallback while the success models learn: quote quality supplements the
        # stronger, multi-factor universe score instead of replacing it.
        if s.get('contract'):
            spread=max(0.,min(1.,1-float(s.get('spread') or max_spread)/max(.01,max_spread)))
            depth=min(1.,float(s.get('depth_qty') or 0)/max(1,float(s.get('qty') or 1))/3)
            return round(.82*universe_quality+12*spread+6*depth,2)
        return round(universe_quality,2)
    def _success_vector(self,s):
        feats=list(s.get('features') or [])
        def fidx(i,default=0.):
            try:return float(feats[i]) if math.isfinite(float(feats[i])) else default
            except Exception:return default
        conf=max(0.,min(1.,float(s.get('confidence') or 0)))
        hp=s.get('historical_probability');op=s.get('online_probability')
        hs=abs(float(hp)-.5)*2 if hp is not None else conf
        os=abs(float(op)-.5)*2 if op is not None else 0.
        agree=1. if hp is not None and op is not None and (float(hp)>=.5)==(float(op)>=.5) else 0.5 if op is None else 0.
        direction=s.get('direction');reg=s.get('regime')
        align=1. if (direction=='UP' and reg=='UPTREND') or (direction=='DOWN' and reg=='DOWNTREND') else .5 if reg=='RANGE' else 0.
        current_cfg=self.config();entry_cfg=s.get('risk_config_at_entry') if isinstance(s.get('risk_config_at_entry'),dict) else {}
        feature_cfg={**current_cfg,**entry_cfg}
        spread=float(s.get('spread') or 99);max_spread=max(.01,float(feature_cfg.get('max_spread') or 1.))
        spreadq=max(0.,min(1.,1-spread/max_spread))
        qty=max(1.,float(s.get('qty') or 1));depth=max(0.,min(5.,float(s.get('depth_qty') or 0)/qty))/5
        delta=abs(float(s.get('option_delta') or .5));deltaq=max(0.,1-abs(delta-.5)/.2)
        vol=np.log1p(max(0.,float(s.get('option_volume') or 0)))/15.
        oi=np.log1p(max(0.,float(s.get('option_oi') or 0)))/18.
        dte=max(0.,min(60.,float(s.get('dte') or 0)))/60.
        fresh=max(0.,min(1.,1-float(s.get('bar_age') or 15)/15.))
        reward=float(feature_cfg.get('reward_r') or 1.8);stop=float(feature_cfg.get('stop_pct') or 12)/40.
        planned=max(.01,(float(s.get('entry') or 0)-float(s.get('stop') or 0))*max(1.,float(s.get('qty') or 1)))
        cost_r=(2*float(feature_cfg.get('fee_per_order') or 0))/planned
        return [conf,hs,os,agree,fidx(11),fidx(12),fidx(8),fidx(9),fidx(10),align,spreadq,depth,deltaq,vol,oi,dte,fresh,reward,stop,cost_r]
    def _historical_setup_vector_from_features(self,f,p,reward=None,stop_barrier=None):
        direction=1. if float(p)>=.5 else -1.
        conf=max(float(p),1-float(p))
        reward=float(reward if reward is not None else self.config().get('reward_r',1.8))
        if stop_barrier is None:
            # This is an UNDERLYING-stock barrier, not an option-premium stop. It is
            # deliberately volatility-scaled so historical candles can provide a
            # useful setup prior without fabricating option-chain history.
            vol=max(0.,float(f.get('volatility_10') or 0));rng=max(0.,float(f.get('range_20') or 0))
            stop_barrier=max(.20,min(1.75,.55*vol+.35*rng))
        return [
            conf,
            direction*float(f.get('ret_1') or 0),direction*float(f.get('ret_3') or 0),
            direction*float(f.get('ret_5') or 0),direction*float(f.get('ret_20') or 0),
            direction*float(f.get('ema_gap_9_20') or 0),direction*float(f.get('ema_gap_20_50') or 0),
            direction*(float(f.get('rsi14') or 50)-50),
            direction*(float(f.get('trend_score') or 12.5)-12.5),
            direction*(float(f.get('momentum_score') or 12.5)-12.5),
            float(f.get('volatility_10') or 0),float(f.get('range_20') or 0),float(f.get('volume_ratio') or 0),
            float(f.get('time_sin') or 0),float(f.get('time_cos') or 0),float(stop_barrier),reward,
            direction*float(f.get('range_pos_20') or 0),direction*float(f.get('range_pos_50') or 0),
            direction*float(f.get('breakout_20') or 0),direction*float(f.get('breakout_50') or 0),
            direction*float(f.get('vwap_distance') or 0),direction*float(f.get('opening_range_position') or 0),
            direction*float(f.get('gap_pct') or 0),float(f.get('volume_accel') or 0),
            float(f.get('compression_ratio') or 0),direction*float(f.get('momentum_accel') or 0),
            direction*float(f.get('day_position') or 0),direction*float(f.get('structure_6') or 0),
            float(f.get('abnormal_volume') or 0)
        ]
    def _historical_setup_vector(self,s):
        vals=list(s.get('features') or [])
        f={name:(float(vals[i]) if i<len(vals) and math.isfinite(float(vals[i])) else 0.) for i,name in enumerate(STOCK_DIRECTION_FEATURES)}
        if isinstance(s.get('pattern_features'),dict):f.update({k:float(s['pattern_features'].get(k) or 0.) for k in STOCK_PATTERN_FEATURES})
        p=s.get('p_up')
        if p is None:return None
        return self._historical_setup_vector_from_features(f,float(p))
    @staticmethod
    def _historical_barrier_outcome(rows,i,direction,stop_pct,target_pct,horizon_bars):
        entry=float(rows[i]['close'] or 0)
        if entry<=0:return None
        stop=float(stop_pct)/100.;target=float(target_pct)/100.;end=min(len(rows)-1,i+int(horizon_bars))
        for j in range(i+1,end+1):
            hi=float(rows[j]['high'] or rows[j]['close'] or 0);lo=float(rows[j]['low'] or rows[j]['close'] or 0)
            if direction>0:
                fav=(hi/entry)-1;adv=1-(lo/entry)
            else:
                fav=1-(lo/entry);adv=(hi/entry)-1
            hit_target=fav>=target;hit_stop=adv>=stop
            # If both barriers occur inside the same five-minute candle, intrabar order
            # is unknowable from OHLC. Count it conservatively as a failure.
            if hit_target and hit_stop:return 0
            if hit_stop:return 0
            if hit_target:return 1
        return None
    def _live_execution_priority_reason(self):
        """Heavy history work yields whenever any automated live desk may need Kite."""
        try:
            cfg=self.config()
            if cfg.get('mode')=='live' and cfg.get('armed'):return 'Stock AI live entries armed'
            if any(p.get('mode')=='live' for p in self.active()):return 'Stock AI live position active'
            if self.rows("SELECT id FROM desk_orders WHERE mode='live' AND status NOT IN ('COMPLETE','CANCELLED','REJECTED') LIMIT 1"):
                return 'Stock AI live order unresolved'
        except Exception:
            return 'Stock AI live-state check unavailable'
        return None
    def _history_archive_coverage(self):
        hb=self.history_backfill_status or {}
        total=max(0,int(hb.get('total') or len(self.a.symbols) or 0))
        complete=max(0,int(hb.get('archive_complete_symbols') or (total if hb.get('status')=='COMPLETE' else 0)))
        ratio=(complete/total) if total else 0.0
        return dict(complete=complete,total=total,ratio=ratio,ready=bool(total and ratio>=STOCK_HIST_MIN_ARCHIVE_COVERAGE),
            required_ratio=STOCK_HIST_MIN_ARCHIVE_COVERAGE)
    def _ensure_deep_history_backfill(self,force=False):
        """Start/resume the one-year archive only when live execution has no priority."""
        if not self.a.connected() or not self.a.symbols:return False
        universe=tuple(self.a.symbols)
        priority=self._live_execution_priority_reason()
        if priority:
            prior=self.history_backfill_status or {}
            self.history_backfill_status={**prior,'status':'PAUSED · LIVE PRIORITY','pause_reason':priority,
                'target_days':STOCK_HISTORY_BACKFILL_DAYS,'total':len(universe),'updated':now_ist().isoformat()}
            self.history_backfill_last_attempt=0.
            return False
        if self.history_backfill_thread and self.history_backfill_thread.is_alive():return False
        if not force and self.history_backfill_status.get('status')=='COMPLETE' and self.history_backfill_universe==universe:return False
        now=time.monotonic()
        if not force and now-self.history_backfill_last_attempt<STOCK_HISTORY_BACKFILL_RETRY_SECONDS:return False
        self.history_backfill_last_attempt=now;self.history_backfill_universe=universe
        def job():
            total=len(universe);added_total=0;errors=[];archive_complete=0;full_year=0;max_available=0;max_available_names=[]
            self.history_backfill_status=dict(status='BACKFILLING',target_days=STOCK_HISTORY_BACKFILL_DAYS,completed=0,total=total,
                archive_complete_symbols=0,archive_full_year_symbols=0,archive_max_available_symbols=0,coverage_ratio=0.0,
                required_coverage=STOCK_HIST_MIN_ARCHIVE_COVERAGE,rows_added=0,started=now_ist().isoformat(),symbol=None,pause_reason=None)
            for i,symbol in enumerate(universe):
                if self.stop_event.is_set():break
                if not self.a.connected():
                    self.history_backfill_status.update(status='PAUSED · KITE DISCONNECTED',completed=i,symbol=symbol);self.history_backfill_last_attempt=0.;return
                priority=self._live_execution_priority_reason()
                if priority:
                    self.history_backfill_status.update(status='PAUSED · LIVE PRIORITY',pause_reason=priority,completed=i,symbol=symbol,
                        archive_complete_symbols=archive_complete,coverage_ratio=round(archive_complete/max(1,total),4),updated=now_ist().isoformat())
                    self.history_backfill_last_attempt=0.;return
                self.history_backfill_status.update(symbol=symbol,completed=i,pause_reason=None)
                def chunk_progress(info):
                    self.history_backfill_status.update(symbol=symbol,current_chunk=info.get('chunks'),chunk_start=info.get('start'),chunk_end=info.get('end'),
                        rows_added=added_total+int(info.get('rows_added') or 0))
                try:
                    r=self.a.backfill_history(symbol,STOCK_HISTORY_BACKFILL_DAYS,chunk_progress,self._live_execution_priority_reason)
                    added_total+=int(r.get('rows_added') or 0)
                    if r.get('status')=='PAUSED_LIVE_PRIORITY':
                        self.history_backfill_status.update(status='PAUSED · LIVE PRIORITY',pause_reason=r.get('pause_reason') or 'Live execution priority',
                            completed=i,symbol=symbol,rows_added=added_total,archive_complete_symbols=archive_complete,
                            coverage_ratio=round(archive_complete/max(1,total),4),updated=now_ist().isoformat())
                        self.history_backfill_last_attempt=0.;return
                    if r.get('status')=='COMPLETE':
                        archive_complete+=1;full_year+=1
                    elif r.get('status')=='MAX_AVAILABLE':
                        # No older candles exist for the current instrument. This is a
                        # resolved archive, not an error; use all genuine available data.
                        archive_complete+=1;max_available+=1;max_available_names.append(symbol)
                    else:
                        errors.append(symbol+': '+str(r.get('error') or r.get('status')))
                except Exception as e:
                    errors.append(symbol+': '+str(e))
                self.history_backfill_status.update(completed=i+1,rows_added=added_total,last_symbol=symbol,error_count=len(errors),
                    archive_complete_symbols=archive_complete,archive_full_year_symbols=full_year,archive_max_available_symbols=max_available,
                    max_available_symbols=max_available_names[:20],coverage_ratio=round(archive_complete/max(1,total),4))
            complete=(not self.stop_event.is_set() and self.a.connected() and not errors and archive_complete==total)
            self.history_backfill_status.update(status='COMPLETE' if complete else 'PARTIAL · WILL RETRY',
                completed=total if complete else self.history_backfill_status.get('completed',0),total=total,rows_added=added_total,
                archive_complete_symbols=archive_complete,archive_full_year_symbols=full_year,archive_max_available_symbols=max_available,
                max_available_symbols=max_available_names[:20],coverage_ratio=round(archive_complete/max(1,total),4),
                finished=now_ist().isoformat(),errors=errors[:8],pause_reason=None)
            self.event('HISTORY_BACKFILL',f"{self.history_backfill_status['status']} · {archive_complete}/{total} resolved ({full_year} full-year + {max_available} max-available) · added {added_total} rows")
            if added_total>0 and not self.train_lock.locked() and not self._live_execution_priority_reason():self.train()
            if self._history_archive_coverage()['ready'] and not self._live_execution_priority_reason():self._ensure_historical_success_training(force=True)
        self.history_backfill_thread=threading.Thread(target=job,daemon=True,name='stock-ai-deep-history')
        self.history_backfill_thread.start();return True
    def _historical_success_signature(self):
        cfg=self.config();reward=float(cfg.get('reward_r') or 1.8);horizon=max(3,min(24,int(float(cfg.get('max_hold') or 90)//5)))
        return f"feature_v={STOCK_HIST_SUCCESS_FEATURE_VERSION}|reward={reward:.4f}|horizon_bars={horizon}",reward,horizon
    def historical_success_model(self,force=False):
        now=time.monotonic()
        if not force and self.hist_success_model_cache is not None and now-self.hist_success_model_cache_at<30:return self.hist_success_model_cache
        m=self.get('historical_setup_success_model')
        self.hist_success_model_cache=m;self.hist_success_model_cache_at=now
        return m
    def train_historical_success_model(self,force=False):
        if not self.hist_success_lock.acquire(False):return self.historical_success_model()
        try:
            direction_model=self.model()
            if not direction_model or not direction_model.get('mu') or not direction_model.get('validation_start'):
                m=dict(status='WAITING FOR DIRECTION MODEL',valid=False,samples=0,required=STOCK_HIST_SUCCESS_MIN_SAMPLES,
                    features=STOCK_HIST_SUCCESS_FEATURES,updated=now_ist().isoformat(),
                    note='Train the stock directional model first; historical setup learning uses its unseen holdout period.')
                self.put('historical_setup_success_model',m);self.hist_success_model_cache=m;self.hist_success_model_cache_at=time.monotonic();return m
            prior=self.historical_success_model();dm_id=direction_model.get('id')
            setup_signature,reward,horizon=self._historical_success_signature()
            if not force and prior and prior.get('direction_model_id')==dm_id and prior.get('setup_signature')==setup_signature and prior.get('valid'):
                return prior
            try:
                holdout_start=datetime.fromisoformat(str(direction_model['validation_start']).replace('Z','+00:00'))
                if holdout_start.tzinfo is None:holdout_start=holdout_start.replace(tzinfo=IST)
                else:holdout_start=holdout_start.astimezone(IST)
            except Exception:
                holdout_start=None
            if holdout_start is None:
                raise ValueError('Directional model validation_start is missing or invalid')
            covered={str(r.get('symbol')) for r in (direction_model.get('coverage') or []) if int(r.get('bars') or 0)>=300}
            train_symbols=[sym for sym in self.a.symbols if not covered or sym in covered]
            samples=[];per_symbol={};coverage=[]
            for n,symbol in enumerate(train_symbols):
                rows=self.a.bars(symbol,20000)
                if len(rows)<240:continue
                arr={k:np.asarray([float(r[k] or 0) for r in rows],float) for k in ('close','high','low','volume')};arr['ts']=[str(r['ts']) for r in rows]
                made=[]
                eligible=[]
                for i in range(200,len(rows)-horizon,3):
                    try:
                        t=datetime.fromisoformat(str(rows[i]['ts']).replace('Z','+00:00'))
                        if t.tzinfo is None:t=t.replace(tzinfo=IST)
                        else:t=t.astimezone(IST)
                    except Exception:continue
                    if t>=holdout_start:eligible.append((i,t))
                max_eval=STOCK_HIST_SUCCESS_PER_SYMBOL*5
                if len(eligible)>max_eval:
                    keep=np.linspace(0,len(eligible)-1,max_eval,dtype=int);eligible=[eligible[int(k)] for k in keep]
                for i,t in eligible:
                    f=self.a.features(rows,i,arr)
                    extra=getattr(self.a,'extra_features',None)
                    if callable(extra):f.update(extra(rows,i,arr) or {})
                    x=[f.get(k,0) for k in STOCK_DIRECTION_FEATURES]
                    if len(x)!=len(direction_model.get('mu') or []) or not all(math.isfinite(float(v)) for v in x):continue
                    p=_stock_direction_predict(direction_model,x);conf=max(p,1-p)
                    if conf<.55:continue
                    vol=max(0.,float(f.get('volatility_10') or 0));rng=max(0.,float(f.get('range_20') or 0))
                    stop_barrier=max(.20,min(1.75,.55*vol+.35*rng));target_barrier=stop_barrier*reward
                    direction=1 if p>=.5 else -1
                    label=self._historical_barrier_outcome(rows,i,direction,stop_barrier,target_barrier,horizon)
                    if label is None:continue
                    hv=self._historical_setup_vector_from_features(f,p,reward,stop_barrier)
                    if all(math.isfinite(float(v)) for v in hv):
                        end_t=t+timedelta(minutes=5*horizon);made.append((t.timestamp(),end_t.timestamp(),hv,int(label),symbol))
                if len(made)>STOCK_HIST_SUCCESS_PER_SYMBOL:
                    # Keep each stock balanced while spreading its samples across the whole
                    # available holdout instead of retaining only the most recent tail.
                    keep=np.linspace(0,len(made)-1,STOCK_HIST_SUCCESS_PER_SYMBOL,dtype=int)
                    made=[made[int(k)] for k in keep]
                samples.extend(made);per_symbol[symbol]=len(made);coverage.append(dict(symbol=symbol,samples=len(made),bars=len(rows)))
                if n%12==0:time.sleep(.01)
            samples.sort(key=lambda r:r[0])
            if len(samples)>STOCK_HIST_SUCCESS_MAX_SAMPLES:
                # Deterministic evenly-spaced downsample preserves chronology and coverage.
                idx=np.linspace(0,len(samples)-1,STOCK_HIST_SUCCESS_MAX_SAMPLES,dtype=int);samples=[samples[int(i)] for i in idx]
            if len(samples)<STOCK_HIST_SUCCESS_MIN_SAMPLES or len({r[3] for r in samples})<2:
                m=dict(status='LEARNING',valid=False,samples=len(samples),required=STOCK_HIST_SUCCESS_MIN_SAMPLES,
                    features=STOCK_HIST_SUCCESS_FEATURES,feature_version=STOCK_HIST_SUCCESS_FEATURE_VERSION,direction_model_id=dm_id,setup_signature=setup_signature,holdout_start=holdout_start.isoformat(),direction_coverage_symbols=len(train_symbols),
                    per_symbol=per_symbol,updated=now_ist().isoformat(),
                    note='Historical stock setup samples are still building. Full-F&O recent history is collected progressively.')
            else:
                split=int(len(samples)*.8);cut=samples[split][0]
                train=[r for r in samples[:split] if r[1]<cut];val=[r for r in samples[split:] if r[0]>=cut]
                if len(train)<200 or len(val)<60 or len({r[3] for r in train})<2 or len({r[3] for r in val})<2:
                    m=dict(status='LEARNING',valid=False,samples=len(samples),required=STOCK_HIST_SUCCESS_MIN_SAMPLES,
                        train_samples=len(train),validation_samples=len(val),features=STOCK_HIST_SUCCESS_FEATURES,feature_version=STOCK_HIST_SUCCESS_FEATURE_VERSION,direction_model_id=dm_id,setup_signature=setup_signature,
                        holdout_start=holdout_start.isoformat(),direction_coverage_symbols=len(train_symbols),per_symbol=per_symbol,updated=now_ist().isoformat(),
                        note='Need more chronological winners and failures for historical setup validation.')
                else:
                    X=[r[2] for r in train];Y=[r[3] for r in train];Xv=[r[2] for r in val];Yv=np.asarray([r[3] for r in val],int)
                    m=_stock_success_fit(X,Y);pv=np.asarray(_stock_success_predict(m,Xv),float)
                    aucv=_stock_success_auc(Yv,pv);acc=float(((pv>=.5)==Yv).mean()*100);baseline=float(max(Yv.mean(),1-Yv.mean())*100);brier=float(np.mean((pv-Yv)**2))
                    valid=aucv is not None and aucv>=STOCK_HIST_SUCCESS_MIN_AUC and acc>=baseline-2 and brier<=STOCK_HIST_SUCCESS_MAX_BRIER
                    m.update(status='VALIDATED' if valid else 'LEARNING',valid=bool(valid),samples=len(samples),train_samples=len(train),validation_samples=len(val),
                        accuracy=acc,auc=aucv,brier=brier,baseline=baseline,features=STOCK_HIST_SUCCESS_FEATURES,feature_version=STOCK_HIST_SUCCESS_FEATURE_VERSION,direction_model_id=dm_id,setup_signature=setup_signature,
                        holdout_start=holdout_start.isoformat(),direction_coverage_symbols=len(train_symbols),horizon_minutes=horizon*5,reward_r=reward,
                        label_definition='Underlying target-before-stop using volatility-scaled barriers on the directional model holdout; not an historical option-P&L backtest.',
                        per_symbol=per_symbol,coverage=coverage,updated=now_ist().isoformat())
            self.put('historical_setup_success_model',m);self.hist_success_model_cache=m;self.hist_success_model_cache_at=time.monotonic()
            self.event('HIST_SUCCESS_MODEL',f"{m.get('status')} samples={m.get('samples',0)} auc={m.get('auc')}")
            return m
        except Exception as e:
            m=dict(status='ERROR',valid=False,samples=0,required=STOCK_HIST_SUCCESS_MIN_SAMPLES,features=STOCK_HIST_SUCCESS_FEATURES,
                error=str(e),updated=now_ist().isoformat())
            self.put('historical_setup_success_model',m);self.hist_success_model_cache=m;self.hist_success_model_cache_at=time.monotonic()
            self.event('ERROR','Historical success model: '+str(e));return m
        finally:self.hist_success_lock.release()
    def _ensure_historical_success_training(self,force=False):
        # Historical-success fitting is background research; live execution always wins.
        if self._live_execution_priority_reason():return False
        direction_model=self.model()
        if not direction_model:return False
        prior=self.historical_success_model();setup_signature,_,_=self._historical_success_signature()
        same=prior and prior.get('direction_model_id')==direction_model.get('id') and prior.get('setup_signature')==setup_signature
        if not force and same and prior.get('valid'):return False
        if self.hist_success_thread and self.hist_success_thread.is_alive():return False
        now=time.monotonic()
        if not force and now-self.hist_success_last_attempt<STOCK_HIST_SUCCESS_RETRY_SECONDS:return False
        self.hist_success_last_attempt=now
        def job():self.train_historical_success_model(force=force)
        self.hist_success_thread=threading.Thread(target=job,daemon=True,name='stock-ai-historical-success')
        self.hist_success_thread.start();return True
    def success_model(self,force=False):
        now=time.monotonic()
        if not force and self.success_model_cache is not None and now-self.success_model_cache_at<30:return self.success_model_cache
        m=self.get('trade_success_model')
        self.success_model_cache=m;self.success_model_cache_at=now
        return m
    def train_success_model(self,force=False):
        if not self.success_lock.acquire(False):return self.success_model()
        try:
            rows=self.rows("SELECT id,exit_ts,pnl,fees,setup FROM desk_positions WHERE status='CLOSED' AND mode='paper' AND setup IS NOT NULL ORDER BY exit_ts,id")
            stamp_key=rows[-1]['exit_ts'] if rows else None;prior=self.success_model()
            if not force and prior and prior.get('last_exit_ts')==stamp_key and prior.get('feature_version')==STOCK_SUCCESS_FEATURE_VERSION:return prior
            X=[];Y=[]
            for r in rows:
                try:
                    s=json.loads(r['setup'] or '{}')
                    if s.get('decision')!='READY':continue
                    x=self._success_vector(s)
                    if len(x)==len(STOCK_SUCCESS_FEATURES) and all(math.isfinite(float(v)) for v in x):
                        X.append(x);Y.append(1 if float(r['pnl'] or 0)-float(r['fees'] or 0)>0 else 0)
                except Exception:continue
            if len(X)<STOCK_SUCCESS_MIN_SAMPLES or len(set(Y))<2:
                m=dict(status='LEARNING',valid=False,samples=len(X),required=STOCK_SUCCESS_MIN_SAMPLES,features=STOCK_SUCCESS_FEATURES,feature_version=STOCK_SUCCESS_FEATURE_VERSION,last_exit_ts=stamp_key,updated=now_ist().isoformat())
                self.put('trade_success_model',m);self.success_model_cache=m;self.success_model_cache_at=time.monotonic();return m
            split=max(25,int(len(X)*.8));split=min(split,len(X)-10)
            Xt,Yt=X[:split],Y[:split];Xv,Yv=X[split:],Y[split:]
            if len(set(Yt))<2 or len(set(Yv))<2:
                m=dict(status='LEARNING',valid=False,samples=len(X),required=STOCK_SUCCESS_MIN_SAMPLES,features=STOCK_SUCCESS_FEATURES,feature_version=STOCK_SUCCESS_FEATURE_VERSION,last_exit_ts=stamp_key,updated=now_ist().isoformat(),note='Need wins and losses in both chronological train and validation sets')
            else:
                m=_stock_success_fit(Xt,Yt);pv=np.asarray(_stock_success_predict(m,Xv),float);yv=np.asarray(Yv,int)
                aucv=_stock_success_auc(yv,pv);acc=float(((pv>=.5)==yv).mean()*100);baseline=float(max(yv.mean(),1-yv.mean())*100);brier=float(np.mean((pv-yv)**2))
                valid=aucv is not None and aucv>=STOCK_SUCCESS_MIN_AUC and acc>=baseline-2 and brier<=STOCK_SUCCESS_MAX_BRIER
                m.update(status='VALIDATED' if valid else 'LEARNING',valid=bool(valid),samples=len(X),train_samples=len(Xt),validation_samples=len(Xv),accuracy=acc,auc=aucv,brier=brier,baseline=baseline,features=STOCK_SUCCESS_FEATURES,feature_version=STOCK_SUCCESS_FEATURE_VERSION,last_exit_ts=stamp_key,updated=now_ist().isoformat())
            self.put('trade_success_model',m);self.success_model_cache=m;self.success_model_cache_at=time.monotonic()
            self.event('SUCCESS_MODEL',f"{m.get('status')} samples={m.get('samples',0)} auc={m.get('auc')}")
            return m
        finally:self.success_lock.release()
    def _live_payoff_profile(self):
        """Conservative realized payoff profile for the live success classifier.

        The classifier label is net profitable vs non-profitable, not target-vs-stop.
        Therefore its probability must not be multiplied by the configured reward_r as
        if every winner hit the full target. We shrink empirical paper-trade R outcomes
        toward the planned payoff until the sample grows.
        """
        cfg=self.config();planned_win=float(cfg.get('reward_r') or 1.8)
        rows=self.rows("SELECT pnl,fees,risk FROM desk_positions WHERE status='CLOSED' AND mode='paper' AND risk>0 ORDER BY exit_ts DESC LIMIT 240")
        vals=[]
        for r in rows:
            try:
                rv=(float(r.get('pnl') or 0)-float(r.get('fees') or 0))/max(.01,float(r.get('risk') or 0))
                if math.isfinite(rv):vals.append(max(-4.,min(5.,rv)))
            except Exception:pass
        wins=[v for v in vals if v>0];losses=[-v for v in vals if v<=0]
        empirical_win=float(np.median(wins)) if wins else planned_win
        empirical_loss=float(np.median(losses)) if losses else 1.0
        # A profitable timed/partial exit can be much smaller than the planned target;
        # never let historical winners imply more than the configured full target.
        empirical_win=max(.05,min(planned_win,empirical_win))
        # Be conservative on downside: observed small losses do not reduce the assumed
        # 1R loss, while slippage/gaps larger than 1R remain represented.
        empirical_loss=max(1.0,min(3.0,empirical_loss))
        weight=min(.75,len(vals)/160.0)
        win_r=(1-weight)*planned_win+weight*empirical_win
        loss_r=(1-weight)*1.0+weight*empirical_loss
        return dict(samples=len(vals),wins=len(wins),losses=len(losses),win_r=round(win_r,4),loss_r=round(loss_r,4),shrink_weight=round(weight,3))
    def _apply_success_intelligence(self,s):
        if s.get('decision')!='READY':return s
        live_model=self.success_model() or self.train_success_model()
        hist_model=self.historical_success_model()
        archive=self._history_archive_coverage()
        live_valid=bool(live_model and live_model.get('valid') and live_model.get('w') and live_model.get('brier') is not None and float(live_model.get('brier'))<=STOCK_SUCCESS_MAX_BRIER)
        hist_model_valid=bool(hist_model and hist_model.get('valid') and hist_model.get('w') and hist_model.get('brier') is not None and float(hist_model.get('brier'))<=STOCK_HIST_SUCCESS_MAX_BRIER)
        hist_valid=bool(hist_model_valid and archive.get('ready'))
        s['live_success_model_valid']=live_valid;s['historical_success_model_valid']=hist_valid
        s['historical_success_model_trained']=hist_model_valid
        s['historical_archive_coverage']=round(float(archive.get('ratio') or 0),4)
        s['historical_archive_ready']=bool(archive.get('ready'))
        cfg=self.config();planned=max(.01,(float(s['entry'])-float(s['stop']))*max(1,int(s['qty'])))
        exit_slippage=float(s.get('entry') or 0)*max(1,int(s.get('qty') or 1))*float(cfg.get('slippage_bps') or 0)/10000
        cost_r=(2*float(cfg.get('fee_per_order') or 0)+exit_slippage)/planned
        s['estimated_cost_r']=round(cost_r,4)
        hp=None;lp=None;raw_hist=None;raw_live=None;hist_weight=0.;live_weight=0.
        hv=self._historical_setup_vector(s)
        if hist_valid and hv:
            raw_hist=float(_stock_success_predict(hist_model,hv))
            # Historical candles do not contain old option books. Treat the historical
            # setup model as a conservative prior and shrink it toward 50%.
            h_samples=float(hist_model.get('samples') or 0);h_auc=float(hist_model.get('auc') or .5)
            coverage=float(archive.get('ratio') or 0)
            coverage_factor=max(.50,min(1.,coverage))
            h_shrink=min(.78,max(.30,.35+min(h_samples,6000)/6000*.25+max(0.,h_auc-.5)*1.2))*coverage_factor
            hp=.5+(raw_hist-.5)*h_shrink
            s['historical_setup_probability']=round(max(0.,min(1.,hp)),6)
            s['historical_setup_probability_raw']=round(raw_hist,6)
        else:
            s['historical_setup_probability']=None
            if hist_model_valid and not archive.get('ready'):
                s['historical_setup_status']=f"WAITING FOR HISTORY COVERAGE · {float(archive.get('ratio') or 0)*100:.0f}% / {float(archive.get('required_ratio') or 0)*100:.0f}%"
            elif not hist_model_valid:
                s['historical_setup_status']='LEARNING'
        if live_valid:
            raw_live=float(_stock_success_predict(live_model,self._success_vector(s)))
            l_samples=float(live_model.get('samples') or 0)
            l_shrink=min(1.,max(.25,(l_samples-STOCK_SUCCESS_MIN_SAMPLES)/160.0+.25))
            lp=.5+(raw_live-.5)*l_shrink
            s['live_success_probability']=round(max(0.,min(1.,lp)),6)
            s['live_success_probability_raw']=round(raw_live,6)
        else:s['live_success_probability']=None
        if hp is not None and lp is not None:
            # Forward executable option outcomes gain authority as evidence accumulates.
            live_weight=min(.75,max(.25,.25+(float(live_model.get('samples') or 0)-STOCK_SUCCESS_MIN_SAMPLES)/460*.50));hist_weight=1-live_weight
            p=hist_weight*hp+live_weight*lp
            s['success_blend']=dict(historical_weight=round(hist_weight,3),live_weight=round(live_weight,3))
            s['selection_basis']=f"Historical setup + live option-success ML ({int(round((1-live_weight)*100))}/{int(round(live_weight*100))})"
        elif hp is not None:
            p=hp;hist_weight=1.0;live_weight=0.0;s['success_blend']=dict(historical_weight=1.0,live_weight=0.0)
            s['selection_basis']='Historical setup bootstrap · live option-success ML still learning'
        elif lp is not None:
            p=lp;hist_weight=0.0;live_weight=1.0;s['success_blend']=dict(historical_weight=0.0,live_weight=1.0)
            s['selection_basis']='Validated live option-success ML'
        else:
            p=None;s['success_blend']=None
            s['selection_basis']='Directional-confidence fallback while success models learn'
        s['success_model_valid']=p is not None
        payoff=self._live_payoff_profile() if lp is not None else None
        if p is not None:
            s['success_probability']=round(max(0.,min(1.,p)),6)
            hist_ev=(hp*float(cfg['reward_r'])-(1-hp)-cost_r) if hp is not None else 0.
            live_ev=(lp*float(payoff['win_r'])-(1-lp)*float(payoff['loss_r'])) if lp is not None and payoff else 0.
            s['expected_r']=round(hist_weight*hist_ev+live_weight*live_ev,4)
            s['expected_r_components']=dict(historical=round(hist_ev,4) if hp is not None else None,
                live=round(live_ev,4) if lp is not None else None,historical_weight=round(hist_weight,3),live_weight=round(live_weight,3))
            s['live_payoff_profile']=payoff
        else:
            s['success_probability']=None;s['expected_r']=None;s['live_payoff_profile']=None
        s['quality_gate']=dict(min_success_prob=float(cfg['min_success_prob']),min_expected_r=float(cfg['min_expected_r']),
            fallback_confidence=float(cfg['fallback_confidence']))
        # Ranking is informational; READY is now reserved for candidates that also pass
        # the learned-probability / expected-value gate. This prevents lower-ranked,
        # negative-quality candidates from being executed merely because capacity remains.
        if p is not None and float(s['success_probability'])<float(cfg['min_success_prob']):
            s.update(decision='WAIT',reason=f"Success gate failed · {float(s['success_probability'])*100:.1f}% < {float(cfg['min_success_prob'])*100:.1f}%")
        elif p is not None and float(s['expected_r'])<float(cfg['min_expected_r']):
            s.update(decision='WAIT',reason=f"Expected-value gate failed · {float(s['expected_r']):.2f}R < {float(cfg['min_expected_r']):.2f}R")
        elif p is None and float(s.get('confidence') or 0)<max(float(cfg['confidence']),float(cfg['fallback_confidence'])):
            s.update(decision='WAIT',reason=f"Success ML learning · fallback direction confidence {float(s.get('confidence') or 0)*100:.1f}% < {max(float(cfg['confidence']),float(cfg['fallback_confidence']))*100:.1f}%")
        else:
            s['quality_gate_passed']=True
            if p is None:s['reason']='READY · success ML learning; stricter direction fallback + execution gates passed'
            else:s['reason']='READY · direction, liquidity, success probability and expected value passed'
        s['quality_gate_passed']=s.get('decision')=='READY'
        s['rank_score']=self.rank_signal(s,cfg['max_spread'])
        return s
    def _screen_direction(self,symbol):
        s=self.direction_snapshot(symbol,refresh=False,record_observation=True,max_bar_age=STOCK_FULL_SCAN_BAR_MAX_AGE)
        self._annotate_pattern(s)
        s['reason']=s.get('reason','').replace('index candles','stock candles')
        if self.banned is not None and symbol in self.banned:s.update(decision='WAIT',reason='Stock in F&O ban list; new entries excluded')
        if self.config()['mode']=='live' and self.banned is None:s.update(decision='WAIT',reason='Daily F&O ban list unavailable; live entries paused')
        if s.get('decision')=='CANDIDATE':s['reason']='Full-universe ML direction qualified · awaiting deep option check'
        s['scan_stage']='UNIVERSE';s['rank_score']=self.rank_signal(s,self.config()['max_spread'])
        return s
    def inspect(self,symbol):
        s=super().inspect(symbol)
        self._annotate_pattern(s)
        s['reason']=s.get('reason','').replace('index candles','stock candles')
        if self.banned is not None and symbol in self.banned:s.update(decision='WAIT',reason='Stock in F&O ban list; new entries excluded')
        if self.config()['mode']=='live' and self.banned is None:s.update(decision='WAIT',reason='Daily F&O ban list unavailable; live entries paused')
        s['scan_stage']='DEEP'
        if s.get('decision')=='READY':
            lot=max(1,int(s.get('lot') or 1));cfg=self.config()
            s['option_volume_lots']=round(float(s.get('option_volume') or 0)/lot,1)
            s['option_oi_lots']=round(float(s.get('option_oi') or 0)/lot,1)
            s['depth_lots']=round(float(s.get('depth_qty') or 0)/lot,2)
            risk_per_unit=max(0.,float(s.get('entry') or 0)-float(s.get('stop') or 0))
            noise_floor=max(2*float(s.get('tick') or .05),2*float(s.get('entry') or 0)*float(cfg.get('slippage_bps') or 0)/10000)
            if s.get('dte') is None or int(s.get('dte') or 0)<MIN_DAYS_TO_EXPIRY:
                s.update(decision='WAIT',reason=f'Expiry has less than {MIN_DAYS_TO_EXPIRY} days remaining')
            elif s['option_volume_lots']<10 or s['option_oi_lots']<20:
                s.update(decision='WAIT',reason='Option liquidity fell below 10 traded lots / 20 OI lots')
            elif s['depth_lots']<1:
                s.update(decision='WAIT',reason='Visible ask depth is below one lot')
            elif risk_per_unit<noise_floor:
                s.update(decision='WAIT',reason='Configured stop is too small versus tick/slippage noise')
            else:self._apply_success_intelligence(s)
        if s.get('decision')!='READY' and s.get('rank_score') is None:s['rank_score']=self.rank_signal(s,self.config()['max_spread'])
        return s
    def _refresh_batch(self,names,screened):
        if not names:return []
        # Stale/missing symbols get first claim on the refresh budget; the rotating
        # cursor then guarantees the rest of the universe is revisited continuously.
        stale=[s for s in names if screened.get(s,{}).get('bar_age') is None or screened.get(s,{}).get('bar_age',999)>STOCK_FULL_SCAN_BAR_MAX_AGE]
        chosen=[]
        for s in stale:
            if s not in chosen:chosen.append(s)
            if len(chosen)>=STOCK_FULL_REFRESH_BATCH:break
        if len(chosen)<STOCK_FULL_REFRESH_BATCH:
            total=len(names);start=self.refresh_cursor%total
            for step in range(total):
                s=names[(start+step)%total]
                if s not in chosen:chosen.append(s)
                if len(chosen)>=STOCK_FULL_REFRESH_BATCH:break
            self.refresh_cursor=(start+STOCK_FULL_REFRESH_BATCH)%total
        # Always keep owned positions fresh even if they fall outside this cycle's batch.
        for p in self.active():
            if p['symbol'] in names and p['symbol'] not in chosen:chosen.append(p['symbol'])
        return chosen
    def enter(self,s):
        # Reprice/revalidate immediately before the order.  A high universe rank never
        # permits a stale option fill or bypasses the normal desk/shared risk gates.
        # This method is called by the dedicated execution thread, never by the broad
        # universe scanner, so slow scanning cannot hold up broker execution/supervision.
        if s.get('decision')!='READY' or self.block() or self.get('close_requested'):return
        fresh=self.inspect(s['symbol']);fresh['relative_strength']=s.get('relative_strength',50.)
        fresh['rank_score']=self.rank_signal(fresh,self.config()['max_spread']);self.signals[s['symbol']]=fresh
        if fresh.get('decision')!='READY' or self.get('close_requested'):return
        return super().enter(fresh)
    def _publish_execution_candidates(self,ranked):
        ready=[dict(s) for s in ranked if s.get('decision')=='READY' and s.get('symbol') in self.a.symbols]
        with self.candidate_lock:
            self.execution_generation+=1
            self.execution_candidates=ready
            self.execution_status=dict(status='CANDIDATES READY' if ready else 'NO READY CANDIDATE',generation=self.execution_generation,
                count=len(ready),best=ready[0]['symbol'] if ready else None,
                best_success_probability=ready[0].get('success_probability') if ready else None,best_expected_r=ready[0].get('expected_r') if ready else None,
                updated=now_ist().isoformat())
        if ready:self.execution_event.set()
    def execute_candidates(self):
        # Entry execution is serialized independently of both the scanner and the risk
        # supervisor.  Existing positions/exits never wait for this entry lock.
        if not self.entry_lock.acquire(False):return
        try:
            if not self.a.connected() or not self.a.market_open() or self.get('close_requested'):return
            with self.candidate_lock:
                candidates=[dict(s) for s in self.execution_candidates]
                generation=self.execution_generation
            if not candidates:return
            self.execution_status=dict(status='EXECUTING',generation=generation,count=len(candidates),updated=now_ist().isoformat())
            for seed in candidates:
                if self.get('close_requested') or not self.config()['armed']:break
                if self.block():break
                try:self.enter(seed)
                except Exception as e:self.event('ERROR','Stock execution: '+str(e),seed.get('symbol',''))
                # Re-evaluate limits after each admitted attempt.  If more positions are
                # allowed, the next-ranked READY candidate may be considered.
                if len(self.active())>=self.config()['max_positions']:break
            self.execution_status=dict(status='IDLE',generation=generation,count=len(candidates),updated=now_ist().isoformat())
        finally:self.entry_lock.release()
    def cycle(self):
        if not self.lock.acquire(False):return
        try:
            self.last_cycle=now_ist().isoformat()
            if not self.a.connected():return
            if not self.a.symbols:self.select_universe()
            else:self._sync_full_universe_membership()
            # First successful Kite connection immediately starts/resumes the deep
            # historical archive in a separate thread. Normal scan/execution continues.
            self._ensure_deep_history_backfill()
            if time.monotonic()-self.ban_checked>1800:
                self.banned,self.ban_error=get_fo_ban_list();self.ban_checked=time.monotonic()
            self.supervise()
            names=list(dict.fromkeys(list(self.a.symbols)+[p['symbol'] for p in self.active()]))
            if not self.a.market_open():
                self.scan_status=dict(status='MARKET CLOSED',phase='Full F&O scan resumes during market hours',completed=0,total=len(names),
                    deep_completed=0,deep_total=0,finished=now_ist().isoformat())
                self.settle_observations();self.maybe_train();self._ensure_historical_success_training();return
            self.scan_status=dict(status='SCANNING FULL F&O',phase='ML universe pass',completed=0,total=len(names),
                deep_completed=0,deep_total=0,started=now_ist().isoformat())
            screened={}
            # Cheap pass: no option chain and no per-symbol history request.  This lets
            # every F&O stock compete on the same model probability each cycle.
            for i,symbol in enumerate(names):
                try:screened[symbol]=self._screen_direction(symbol)
                except Exception as e:screened[symbol]=dict(symbol=symbol,ts=now_ist().isoformat(),decision='WAIT',reason=str(e),rank_score=0,scan_stage='UNIVERSE')
                if i%25==0:self.scan_status.update(completed=i+1,symbol=symbol)
            # Rotate recent-history refreshes so the whole universe stays within the
            # freshness window without a burst of ~200 historical-data requests.
            refresh_names=self._refresh_batch(names,screened)
            self.scan_status.update(phase='Refreshing market history',refreshing=len(refresh_names),completed=len(names))
            for j,symbol in enumerate(refresh_names):
                try:
                    self.a.refresh(symbol);screened[symbol]=self._screen_direction(symbol)
                except Exception as e:
                    prior=screened.get(symbol,dict(symbol=symbol));prior.update(decision='WAIT',reason='History refresh: '+str(e),rank_score=0,scan_stage='UNIVERSE');screened[symbol]=prior
                self.supervise()
                if j+1<len(refresh_names):time.sleep(STOCK_HISTORY_PACE_SECONDS)
            # Cross-sectional directional strength is local math on the completed universe pass.
            # It adds no broker request and helps a new stock compete with the entire F&O list.
            self._apply_cross_sectional_strength(screened)
            # Deep checks are deliberately limited to the strongest model candidates.
            directional=[s for s in screened.values() if s.get('decision')=='CANDIDATE']
            directional.sort(key=lambda s:(float(s.get('rank_score') or 0),float(s.get('confidence') or 0)),reverse=True)
            shortlist=directional[:min(STOCK_FULL_SCAN_DEEP_N,len(directional))]
            self.signals=dict(screened)
            self.scan_status.update(phase='Deep option checks',deep_total=len(shortlist),deep_completed=0,
                directional_candidates=len(directional),fresh_universe=sum(1 for s in screened.values() if s.get('bar_age') is not None and s.get('bar_age')<=STOCK_FULL_SCAN_BAR_MAX_AGE))
            for i,seed in enumerate(shortlist):
                symbol=seed['symbol']
                try:
                    deep=self.inspect(symbol);deep['relative_strength']=seed.get('relative_strength',50.)
                    deep['rank_score']=self.rank_signal(deep,self.config()['max_spread']);self.signals[symbol]=deep
                except Exception as e:self.signals[symbol]=dict(seed,decision='WAIT',reason='Deep check: '+str(e),scan_stage='DEEP')
                self.scan_status.update(deep_completed=i+1,symbol=symbol)
                self.supervise()
            ranked=sorted(self.signals.values(),key=lambda s:(s.get('decision')=='READY',s.get('success_probability') is not None,float(s.get('success_probability') or 0),float(s.get('expected_r') or -99),float(s.get('rank_score') or 0)),reverse=True)
            # Scanner only publishes the ranked READY list.  A dedicated execution
            # thread performs fresh pre-entry validation and broker submission.
            self._publish_execution_candidates(ranked)
            self.settle_observations();self.maybe_train();self.train_success_model();self._ensure_historical_success_training()
            self.scan_status.update(status='COMPLETE',phase='Complete',completed=len(names),ready=sum(1 for s in self.signals.values() if s.get('decision')=='READY'),finished=now_ist().isoformat())
            self.last_cycle=now_ist().isoformat()
            if self.get('close_requested') and not self.active():self.put('close_requested',False)
        except Exception as e:
            self.scan_status.update(status='ERROR',error=str(e));self.event('ERROR','Stock scan: '+str(e))
        finally:self.lock.release()
    def supervise(self):
        if not self.risk_lock.acquire(False):return
        try:
            self.reconcile();self.verify_positions()
            # Close requests have priority over scanning and new entries.  Cancel live
            # BUYs that are still pending so a manual close cannot be followed by a
            # late entry fill.  Filled quantity remains owned and is managed below.
            if self.get('close_requested'):
                for o in self.rows("SELECT * FROM desk_orders WHERE mode='live' AND side='BUY' AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')"):
                    if o.get('broker_id'):
                        try:self.a.cancel(o['broker_id'])
                        except Exception as e:self.record_trade(o['position_id'],'CANCEL_DEFERRED',dict(reason=str(e)))
            if self.a.market_open():self.manage(close_all=bool(self.get('close_requested')))
        except Exception as e:
            self.put('halt','Position supervision error: '+str(e));self.event('ERROR',str(e))
        finally:self.risk_lock.release()
    def start(self):
        if self.started:return
        self.started=True
        def scan_loop():
            while not self.stop_event.is_set():self.cycle();self.stop_event.wait(30)
        def risk_loop():
            while not self.stop_event.wait(5):
                if self.a.connected():self.supervise()
        def execution_loop():
            while not self.stop_event.is_set():
                self.execution_event.wait(1)
                if self.stop_event.is_set():break
                if not self.execution_event.is_set():continue
                self.execution_event.clear()
                self.execute_candidates()
        threading.Thread(target=scan_loop,daemon=True,name='stock-ai-scan').start()
        threading.Thread(target=risk_loop,daemon=True,name='stock-ai-risk').start()
        threading.Thread(target=execution_loop,daemon=True,name='stock-ai-execution').start()
    def state(self):
        s=super().state()
        s['signals'].sort(key=lambda x:(x.get('decision')=='READY',x.get('success_probability') is not None,float(x.get('success_probability') or 0),float(x.get('expected_r') or -99),float(x.get('rank_score') or 0)),reverse=True)
        for i,row in enumerate(s['signals'],1):row['rank']=i
        sm=self.train_success_model();hm=self.historical_success_model()
        if sm:sm={k:v for k,v in sm.items() if k not in ('mu','sd','w','bias')}
        if hm:hm={k:v for k,v in hm.items() if k not in ('mu','sd','w','bias','per_symbol','coverage')}
        archive=self._history_archive_coverage()
        hist_ready=bool(hm and hm.get('valid') and archive.get('ready') and hm.get('brier') is not None and float(hm.get('brier'))<=STOCK_HIST_SUCCESS_MAX_BRIER)
        live_ready=bool(sm and sm.get('valid') and sm.get('brier') is not None and float(sm.get('brier'))<=STOCK_SUCCESS_MAX_BRIER)
        selector_status='BLENDED' if hist_ready and live_ready else 'HISTORICAL BOOTSTRAP' if hist_ready else 'LIVE OPTION MODEL' if live_ready else 'DIRECTIONAL FALLBACK'
        s.update(build=STOCK_AI_BUILD,universe=self.get('stock_universe_info',{}),scan=dict(self.scan_status),ban_status=self.ban_error or 'Daily list checked',watchlist=list(self.a.symbols),
            full_fo_monitoring=self._full_mode(),deep_check_limit=STOCK_FULL_SCAN_DEEP_N,refresh_batch=STOCK_FULL_REFRESH_BATCH,
            execution=dict(self.execution_status),success_model=sm,live_success_model=sm,historical_success_model=hm,success_selector_status=selector_status,
            historical_archive=archive,history_backfill=dict(self.history_backfill_status))
        return s
    def control(self,action,body):
        # Disarming is immediate even while a broad scan is running.
        if action in ('disarm','close'):
            with self.risk_lock:
                cfg=self.config();cfg['armed']=False;self.put('config',cfg)
                if action=='close':
                    self.put('close_requested',True)
                    # Drop queued entries immediately; the risk thread performs broker
                    # cancellation/reconciliation independently of the scan thread.
                    with self.candidate_lock:self.execution_candidates=[]
                    self.execution_event.clear()
                self.event('CONTROL',action)
            if action=='close' and self.a.connected():self.supervise()
            return cfg
        if not self.lock.acquire(False):raise ValueError('Scan in progress; retry after completion')
        try:
            with self.risk_lock:return super().control(action,body)
        finally:self.lock.release()

STOCK_DESK=StockDeskEngine(StockDeskAdapter())

@app.route('/api/stock-ai/state')
def stock_ai_state():
    if not require_session():return jsonify(error='Connect Kite first'),401
    return jsonify(STOCK_DESK.state())

@app.route('/api/stock-ai/config',methods=['POST'])
def stock_ai_config():
    if not require_session():return jsonify(error='Connect Kite first'),401
    if not STOCK_DESK.lock.acquire(False):return jsonify(error='Scan in progress; retry after completion'),409
    try:
        with STOCK_DESK.risk_lock:return jsonify(ok=True,config=STOCK_DESK.save_config(request.get_json(silent=True) or {}))
    except (ValueError,TypeError) as e:return jsonify(error=str(e)),400
    finally:STOCK_DESK.lock.release()

@app.route('/api/stock-ai/control/<action>',methods=['POST'])
def stock_ai_control(action):
    if not require_session():return jsonify(error='Connect Kite first'),401
    body=request.get_json(silent=True) or {}
    try:
        if action in ('scan','universe','sync'):
            if not STOCK_DESK.train_lock.acquire(False):return jsonify(error='Learning/universe job already running'),409
            def job():
                try:
                    if action in ('universe','sync'):
                        with STOCK_DESK.lock:
                            if action=='universe' or not STOCK_DESK.a.symbols:STOCK_DESK.select_universe(body)
                            if action=='sync':
                                for i,symbol in enumerate(STOCK_DESK.a.symbols):
                                    STOCK_DESK.training=dict(status='SYNCING HISTORY',progress=round(100*i/max(1,len(STOCK_DESK.a.symbols))))
                                    STOCK_DESK.a.refresh(symbol)
                                    if i+1<len(STOCK_DESK.a.symbols):time.sleep(STOCK_HISTORY_PACE_SECONDS)
                                STOCK_DESK.training=dict(status='HISTORY READY',progress=100)
                    STOCK_DESK.cycle()
                    if action=='sync':
                        STOCK_DESK._ensure_deep_history_backfill(force=True)
                        STOCK_DESK._ensure_historical_success_training(force=True)
                except Exception as e:
                    STOCK_DESK.training=dict(status='ERROR',progress=0,error=str(e));STOCK_DESK.event('ERROR',str(e))
                finally:STOCK_DESK.train_lock.release()
            threading.Thread(target=job,daemon=True,name='stock-ai-job').start()
            return jsonify(ok=True,started=True)
        if action=='train':return jsonify(ok=True,started=STOCK_DESK.train())
        return jsonify(ok=True,config=STOCK_DESK.control(action,body))
    except (ValueError,TypeError) as e:return jsonify(error=str(e)),400


# ===========================================================================
from collections import deque
ACCOUNT_EXECUTION_LOCK=threading.RLock()

class SharedAIRisk:
    """Stock Options AI risk budget; manual trades are outside this budget."""
    DEFAULTS=dict(daily_loss=7500.,profit_activation=3000.,profit_giveback=1500.,profit_protection_enabled=True,
        symbol_losses=2,max_trades=10,max_positions=3,one_trade_per_arm=False,same_signal_lock=True,
        symbol_loss_limit_enabled=True,trade_limit_enabled=True,position_limit_enabled=True)
    def __init__(self,engines):
        self.engines=engines;self.owner=LegacySharedRiskStorage();self.lock=globals().get('ACCOUNT_EXECUTION_LOCK') or threading.RLock()
        for name,engine in engines.items():engine.shared_risk=self;engine.desk_name=name
        if not self.owner.get('optional_risk_controls_v1',False):
            cfg=self.config();cfg.pop('loss_streak',None);cfg['one_trade_per_arm']=False
            self.owner.put('shared_risk_config',cfg);self.owner.put('optional_risk_controls_v1',True)
            self.owner.event('SHARED_RISK','Consecutive-loss stop removed; one-entry-per-arm disabled. Optional protection switches available.')
    def config(self):
        cfg={**self.DEFAULTS,**self.owner.get('shared_risk_config',{})};cfg.pop('loss_streak',None);return cfg
    def save(self,body):
        with self.lock:
            if any(e.config()['armed'] or e.active() for e in self.engines.values()):raise ValueError('Disarm all AI desks and close/reconcile positions before changing shared risk settings')
            cfg=self.config()
            bounds=dict(daily_loss=(100,1000000),profit_activation=(100,1000000),profit_giveback=(100,1000000),symbol_losses=(1,20),max_trades=(1,100),max_positions=(1,10))
            for key,value in body.items():
                if key in ('one_trade_per_arm','same_signal_lock','profit_protection_enabled','symbol_loss_limit_enabled','trade_limit_enabled','position_limit_enabled'):
                    if not isinstance(value,bool):raise ValueError('Expected checkbox: '+key)
                    cfg[key]=value
                elif key in bounds:
                    if isinstance(value,bool):raise ValueError('Invalid '+key)
                    v=float(value);lo,hi=bounds[key]
                    if not math.isfinite(v) or not lo<=v<=hi:raise ValueError('Out of range: '+key)
                    if key in ('symbol_losses','max_trades','max_positions'):
                        if not v.is_integer():raise ValueError('Whole number required: '+key)
                        v=int(v)
                    cfg[key]=v
                else:raise ValueError('Unknown shared setting: '+key)
            if cfg['profit_protection_enabled'] and cfg['profit_giveback']>=cfg['profit_activation']:raise ValueError('Giveback must be below profit activation')
            self.owner.put('shared_risk_config',cfg)
            self.owner.event('SHARED_RISK','Shared limits updated; existing daily latches retained')
            return cfg
    def snapshot(self,mode):
        with self.lock:
            cfg=self.config();today=now_ist().date().isoformat();now=time.monotonic()
            key='shared_risk_day:'+mode+':'+today;state=self.owner.get(key,{})
            total=0.;reserved=0.;active=[];closed=[];attempts=0;missing=[];pending=[];desk_totals={};desk_reserved={}
            for name,e in self.engines.items():
                rows=e.rows("SELECT * FROM desk_positions WHERE mode=? AND (ts LIKE ? OR exit_ts LIKE ? OR status IN ('OPEN','ENTRY_PENDING','EXIT_PENDING'))",(mode,today+'%',today+'%'))
                subtotal=0.;reserved_before=reserved
                for p in rows:
                    if p['ts'].startswith(today):attempts+=1
                    if p['status']=='CLOSED' and (p.get('exit_ts') or '').startswith(today):closed.append(dict(p,desk=name))
                    # Retained carry positions are included; dashboard explicitly discloses ledger basis.
                    subtotal+=p['pnl']-p['fees']
                    if p['status'] not in ('OPEN','ENTRY_PENDING','EXIT_PENDING'):continue
                    active.append((name,p))
                    if p['status'] in ('ENTRY_PENDING','EXIT_PENDING') or p.get('conversion'):pending.append(p['id'])
                    health=e.health.get(p['id'],{});bid=health.get('bid');fresh=bid is not None and now-health.get('observed_mono',0)<= (3 if name=='cas' else 15)
                    if p['qty']>0:
                        if not fresh:missing.append(p['contract']);bid=p['entry']
                        direction=int(p.get('direction',1));subtotal+=direction*(bid-p['entry'])*p['qty']
                        reserved+=max(0.,direction*(bid-p['stop']))*p['qty']+e.config()['fee_per_order']
                    if p['status']=='ENTRY_PENDING':
                        # Reserve the unfilled part, including intents not yet submitted.
                        setup=json.loads(p.get('setup') or '{}');wanted=int(setup.get('qty') or 0)
                        reserved+=max(0,wanted-p['qty'])*max(0.,int(p.get('direction',1))*(float(setup.get('entry') or 0)-p['stop']))+e.config()['fee_per_order']
                desk_totals[name]=subtotal;desk_reserved[name]=reserved-reserved_before;total+=subtotal
            closed.sort(key=lambda p:(p['exit_ts'],p['id']))
            running=0.;historical_peak=0.
            for p in closed:
                running+=p['pnl']-p['fees'];historical_peak=max(historical_peak,running)
            peak=max(float(state.get('peak',0)),historical_peak,total if not missing else 0.)
            streak=0
            for p in reversed(closed):
                if p['pnl']-p['fees']>=0:break
                streak+=1
            reason=state.get('reason');liquidate=bool(state.get('liquidate'))
            # Clear only the removed/disabled rule's own old latch. Financial loss
            # limits are evaluated again below and cannot be cleared by these switches.
            if (reason=='Combined AI consecutive-loss limit reached' or
                reason=='Combined AI profit giveback limit reached' and not cfg['profit_protection_enabled'] or
                reason=='Combined AI daily entry-attempt limit reached' and not cfg['trade_limit_enabled']):
                reason=None;liquidate=False
            # Financial stops take precedence over entry-only latches.
            financial=None
            if total<=-cfg['daily_loss']:financial='Combined AI daily loss limit reached'
            elif cfg['profit_protection_enabled'] and not missing and peak>=cfg['profit_activation'] and peak-total>=cfg['profit_giveback']:financial='Combined AI profit giveback limit reached'
            # Also retain each desk's tighter daily stop; no same-day clear-halt bypass.
            for name,e in self.engines.items():
                if desk_totals[name]<=-e.config()['daily_loss']:financial=name+' daily loss limit reached'
            if financial:reason=financial;liquidate=True
            if not reason and cfg['trade_limit_enabled'] and attempts>=cfg['max_trades']:reason='Combined AI daily entry-attempt limit reached'
            new=dict(peak=peak,reason=reason,liquidate=liquidate)
            if new!=state:self.owner.put(key,new)
            blocker=reason
            if not blocker and missing:blocker='Fresh position quotes required: '+', '.join(missing)
            if not blocker and pending:blocker='Unresolved entry/exit order in another or this AI desk'
            if not blocker and any(e.mismatches for e in self.engines.values()):blocker='Broker quantity verification required before new entries'
            if not blocker and cfg['position_limit_enabled'] and len(active)>=cfg['max_positions']:blocker='Combined AI open-position limit reached'
            if not blocker and globals().get('BROKER_GATE') and BROKER_GATE.status()['cooldown_seconds']>0:blocker='Broker rate-limit cooldown; new entries paused'
            floor=max(-cfg['daily_loss'],peak-cfg['profit_giveback']) if cfg['profit_protection_enabled'] and peak>=cfg['profit_activation'] else -cfg['daily_loss']
            return dict(mode=mode,date=today,net=round(total,2),peak=round(peak,2),reserved=round(reserved,2),
                remaining=round(max(0.,total-reserved-floor),2),reason=reason,entry_block=blocker,liquidate=liquidate,
                attempts=attempts,loss_streak=streak,positions=len(active),missing_quotes=missing,closed=closed,config=cfg,desk_reserved=desk_reserved,desk_totals=desk_totals)
    def signal_key(self,e,s):
        if e.desk_name=='cas':
            epoch=s.get('signal_epoch')
            return str(int(float(epoch)//max(1.,e.config()['lookback_seconds']))) if epoch else None
        return str(s['bar_ts']) if s.get('bar_ts') else None
    def admit(self,e,s):
        # Shared account ownership synchronization does not share financial budgets.
        if e.config()['mode']=='live':
            for name in ('STOCK_DESK',):
                other=globals().get(name)
                if not other or other is e:continue
                for position in other.active():
                    if position.get('mode')=='live' and position.get('exchange')==s.get('exchange') and position.get('contract')==s.get('contract'):
                        return 'Broker contract already owned or pending in another desk'

        # Caller holds this lock through the durable position insert. No broker I/O here.
        cfg=e.config();view=self.snapshot(cfg['mode']);limits=view['config']
        if view['entry_block']:return view['entry_block']
        losses=sum(1 for p in view['closed'] if p['symbol']==s['symbol'] and p['pnl']-p['fees']<0)
        if limits['symbol_loss_limit_enabled'] and losses>=limits['symbol_losses']:return 'Daily losing-trade limit for '+s['symbol']
        signal=self.signal_key(e,s)
        if limits['same_signal_lock']:
            if signal is None:return 'Signal timestamp missing; cannot establish fresh entry'
            for other in self.engines.values():
                for row in other.rows('SELECT setup FROM desk_positions WHERE mode=? AND symbol=? AND ts LIKE ?',(cfg['mode'],s['symbol'],view['date']+'%')):
                    prior=json.loads(row['setup'] or '{}')
                    prior_key=prior.get('shared_signal_key') or (str(prior.get('bar_ts')) if prior.get('bar_ts') else None)
                    if prior_key==signal:return 'Signal already used; wait for a fresh signal'
        required=abs(float(s['entry'])-float(s['stop']))*int(s['qty'])+2*cfg['fee_per_order']
        if not math.isfinite(required) or required<=0:return 'Invalid planned entry risk'
        if required>cfg['risk']+.01:return 'Planned loss including estimated fees exceeds per-trade risk'
        floor=-limits['daily_loss']
        if limits['profit_protection_enabled'] and view['peak']>=limits['profit_activation']:floor=max(floor,view['peak']-limits['profit_giveback'])
        if required>view['net']-view['reserved']-floor:return 'Insufficient shared risk remaining for this entry'
        if view['desk_totals'][e.desk_name]-view['desk_reserved'][e.desk_name]-required < -cfg['daily_loss']:return 'Insufficient desk daily risk remaining'
        s['shared_signal_key']=signal;s['shared_risk_at_entry']={k:v for k,v in view.items() if k!='closed'}
        return None


class LegacySharedRiskStorage:
    def __init__(self):
        c=ai_db()
        try:
            c.executescript('CREATE TABLE IF NOT EXISTS desk_meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS desk_events(id INTEGER PRIMARY KEY,ts TEXT,kind TEXT,symbol TEXT,detail TEXT);');c.commit()
        finally:c.close()
    # Keep the existing other-desks' financial latches and settings in their original store.
    def get(self,key,default=None):
        c=ai_db()
        try:r=c.execute('SELECT value FROM desk_meta WHERE key=?',(key,)).fetchone();return json.loads(r[0]) if r else default
        finally:c.close()
    def put(self,key,value):
        c=ai_db()
        try:c.execute('INSERT OR REPLACE INTO desk_meta VALUES(?,?)',(key,json.dumps(value)));c.commit()
        finally:c.close()
    def event(self,kind,detail,symbol=''):
        c=ai_db()
        try:c.execute('INSERT INTO desk_events VALUES(NULL,?,?,?,?)',(now_ist().isoformat(),kind,symbol,detail));c.commit()
        finally:c.close()

SHARED_RISK=SharedAIRisk({'stock':STOCK_DESK})

@app.route('/api/shared-ai-risk',methods=['GET','POST'])
def shared_ai_risk():
    if not require_session():return jsonify(error='Connect Kite first'),401
    try:
        if request.method=='POST':
            body=request.get_json(silent=True)
            if not isinstance(body,dict):raise ValueError('Settings object required')
            SHARED_RISK.save(body)
        states={}
        for mode in ('paper','live'):
            states[mode]=SHARED_RISK.snapshot(mode);states[mode].pop('closed',None)
        return jsonify(config=SHARED_RISK.config(),states=states,broker=BROKER_GATE.status(),scope='Stock Options AI only. Keep one backend worker process.')
    except (ValueError,TypeError) as e:return jsonify(error=str(e)),400


@app.route('/api/order-recovery/<desk>/<oid>',methods=['POST'])
def recover_desk_order(desk,oid):
    if not require_session():return jsonify(error='Connect Kite first'),401
    engine={'stock':STOCK_DESK}.get(desk)
    if engine is None:return jsonify(error='Unknown desk'),404
    body=request.get_json(silent=True)
    if not isinstance(body,dict) or body.get('action') not in ('cancel','verify-not-placed'):return jsonify(error='Valid recovery action required'),400
    lock=getattr(engine,'risk_lock',engine.lock)
    if not lock.acquire(False):return jsonify(error='Order supervision is running; retry shortly'),409
    try:
        if hasattr(engine,'last_verify'):engine.last_verify=0.
        return jsonify(ok=True,**engine.recover_order(oid,body))
    except Exception as e:return jsonify(error=str(e)),409
    finally:lock.release()


@app.route('/api/trade-review/<desk>/<pid>')
def desk_trade_review(desk,pid):
    if not require_session():return jsonify(error='Connect Kite first'),401
    engine={'stock':STOCK_DESK}.get(desk)
    if engine is None:return jsonify(error='Unknown desk'),404
    try:return jsonify(engine.trade_review(pid))
    except ValueError as e:return jsonify(error=str(e)),404


def _review_engine(desk):
    return {'stock':STOCK_DESK}.get(desk)


def _review_filter():
    date=request.args.get('date','')
    if date:
        from datetime import datetime as date_parser
        if date_parser.strptime(date,'%Y-%m-%d').strftime('%Y-%m-%d')!=date:raise ValueError('Use YYYY-MM-DD')
    return date


@app.route('/api/trade-history/<desk>')
def desk_trade_history(desk):
    if not require_session():return jsonify(error='Connect Kite first'),401
    engine=_review_engine(desk)
    if engine is None:return jsonify(error='Unknown desk'),404
    try:
        date=_review_filter();before=int(request.args.get('before','9223372036854775807'))
        rows=engine.rows("SELECT rowid cursor,id,ts,contract,status,mode FROM desk_positions WHERE rowid<? AND (?='' OR substr(ts,1,10)=?) ORDER BY rowid DESC LIMIT 101",(before,date,date))
        return jsonify(trades=rows[:100],next_before=rows[99]['cursor'] if len(rows)>100 else None,recorder_error=engine.recorder_error)
    except ValueError as e:return jsonify(error=str(e)),400


@app.route('/api/trade-timeline/<desk>/<pid>')
def desk_trade_timeline(desk,pid):
    if not require_session():return jsonify(error='Connect Kite first'),401
    engine=_review_engine(desk)
    if engine is None:return jsonify(error='Unknown desk'),404
    try:return jsonify(engine.trade_timeline(pid,max(0,int(request.args.get('after','0')))))
    except ValueError:return jsonify(error='Invalid cursor'),400


def _review_csv_cell(value):
    # Do not allow text supplied by broker / instrument metadata to become Excel formulas.
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):return "'"+value
    return value


@app.route('/api/trade-review-export/<desk>')
def desk_trade_review_export(desk):
    if not require_session():return jsonify(error='Connect Kite first'),401
    engine=_review_engine(desk)
    if engine is None:return jsonify(error='Unknown desk'),404
    try:date=_review_filter()
    except ValueError:return jsonify(error='Use YYYY-MM-DD'),400
    pid=request.args.get('pid','');fmt=request.args.get('format','csv')
    if fmt not in ('csv','json'):return jsonify(error='Use csv or json'),400
    if pid and not engine.rows('SELECT id FROM desk_positions WHERE id=?',(pid,)):return jsonify(error='Trade not found'),404
    # Capture an upper bound, so new entries cannot extend a running export indefinitely.
    upper=engine.rows('SELECT COALESCE(MAX(rowid),0) n FROM desk_positions')[0]['n']
    from flask import Response,stream_with_context
    import csv,io,json as export_json
    def reviews():
        cursor=0
        while True:
            batch=engine.rows("SELECT rowid cursor,id FROM desk_positions WHERE rowid>? AND rowid<=? AND (?='' OR id=?) AND (?='' OR substr(ts,1,10)=?) ORDER BY rowid LIMIT 50",(cursor,upper,pid,pid,date,date))
            if not batch:break
            for row in batch:
                review=engine.trade_review(row['id']);review.pop('timeline',None)
                yield row['id'],review
            cursor=batch[-1]['cursor']
    def events(trade_id):
        after=0
        upper_event=engine.rows('SELECT COALESCE(MAX(id),0) n FROM desk_trade_events WHERE position_id=?',(trade_id,))[0]['n']
        while True:
            batch=engine.rows('SELECT id,ts,kind,payload FROM desk_trade_events WHERE position_id=? AND id>? AND id<=? ORDER BY id LIMIT 500',(trade_id,after,upper_event))
            if not batch:break
            for e in batch:yield dict(id=e['id'],ts=e['ts'],kind=e['kind'],data=export_json.loads(e['payload']))
            after=batch[-1]['id']
    def generate():
        if fmt=='json':
            yield '{"note":"Export of retained local evidence. Active trades can change during export. Dates filter entry day in IST.","trades":['
            first=True
            for trade_id,review in reviews():
                if not first:yield ','
                first=False;yield '{"review":'+export_json.dumps(review,default=str)+',"events":['
                first_event=True
                for event in events(trade_id):
                    if not first_event:yield ','
                    first_event=False;yield export_json.dumps(event,default=str)
                yield ']}'
            yield ']}'
        else:
            buf=io.StringIO();writer=csv.writer(buf)
            def line(values):
                buf.seek(0);buf.truncate(0);writer.writerow([_review_csv_cell(v) for v in values]);return buf.getvalue()
            yield '\ufeff'+line(['trade_id','contract','mode','trade_status','record_time_IST','record_type','expected_direction','net_realized_estimated_fees','observed_bid','observed_ask','signal_direction','exit_reason','explanation','evidence_json'])
            for trade_id,r in reviews():
                p=r['position'];base=[trade_id,p['contract'],p['mode'],p['status']]
                yield line(base+[p['ts'],'REVIEW',r['entry_plan'].get('direction'),r['outcome']['net_realized'],'','','',r.get('recorded_exit_reason') or p.get('exit_reason'),' '.join(r['explanations'])+' '+r['limitations'],export_json.dumps(r,default=str)])
                for e in events(trade_id):
                    data=e['data'];q=data.get('quote') or {};signal=data.get('signal') or {}
                    yield line(base+[e['ts'],e['kind'],r['entry_plan'].get('direction'),'',q.get('bid'),q.get('ask'),signal.get('direction'),data.get('reason') or data.get('decision'),'Recorded observation; does not prove market causation',export_json.dumps(data,default=str)])
    return Response(stream_with_context(generate()),mimetype='application/json' if fmt=='json' else 'text/csv',headers={'Content-Disposition':f'attachment; filename="{desk}-trade-evidence.{fmt}"','Cache-Control':'no-store'})


def start_application_workers():
    if os.environ.get("AI_START_WORKERS","1")!="1":return
    STOCK_DESK.start()

start_application_workers()

if __name__ == "__main__":
    if "PUT_YOUR" in API_KEY or "PUT_YOUR" in API_SECRET:
        print("!! Set KITE_API_KEY and KITE_API_SECRET (env vars, or edit backend.py) before running.")
    print(f"Set your Kite app's Redirect URL to: {REDIRECT_URL}")
    if ALLOW_INSECURE_NEWS:
        print("!! ALLOW_INSECURE_NEWS is on — news headline fetches will skip TLS verification on failure.")
    print("Starting server at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
