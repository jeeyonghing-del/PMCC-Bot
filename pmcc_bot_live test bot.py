"""
PMCC Bot — Poor Man's Covered Call (Multi-Symbol)
Stocks:      NVDA, AVGO
Broker:      Tiger Brokers (Open API)
LEAPS delta: 0.90+ (deep ITM), per-symbol configurable
Short call:  0.25–0.35 delta, 20–35 DTE, per-symbol configurable
Profit take: Close short call at 50% of premium received
Roll short:  When ITM or loss hits 50% of premium received
Ex-div guard: Skip selling short calls that expire on/after a known ex-dividend date
              (relevant for dividend payers like AVGO; NVDA has none configured)
Check freq:  Every 30 minutes during market hours
Telegram:    Combined alert stream, tagged by symbol

SECRETS: All credentials are now read from environment variables (see CONFIG
section below). Set these in Railway's "Variables" tab — do NOT hardcode them
in this file. Required env vars:
  TIGER_ID, TIGER_ACCOUNT, TIGER_LICENSE, TIGER_PRIVATE_KEY,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import time
import logging
import requests
from datetime import datetime, date
import pytz

# ── Tiger Brokers config ───────────────────────────────────────────────────
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.quote.quote_client import QuoteClient
from tigeropen.common.util.order_utils import limit_order

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pmcc_bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — credentials come from environment variables
# ═══════════════════════════════════════════════════════════════════════════════

TIGER_ID     = os.environ["TIGER_ID"]
ACCOUNT      = os.environ["TIGER_ACCOUNT"]
LICENSE      = os.environ["TIGER_LICENSE"]
PRIVATE_KEY  = os.environ["TIGER_PRIVATE_KEY"]   # paste full PEM key as a single env var value

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]

# Set PAPER_MODE=true in Railway/your env to run against Tiger's sandbox
# (paper trading) instead of live. Defaults to paper mode (safer default) —
# you must explicitly set PAPER_MODE=false to trade live.
PAPER_MODE = os.environ.get("PAPER_MODE", "true").strip().lower() == "true"
MODE_TAG   = "🧪 PAPER" if PAPER_MODE else "🔴 LIVE"

CHECK_INTERVAL_SEC  = 30 * 60       # 30 minutes

# ── Per-symbol strategy parameters ───────────────────────────────────────────
# Add/edit symbols here. Each symbol tracks its own LEAPS + short call state
# independently. ex_div_dates is a list of "YYYY-MM-DD" strings — the bot will
# refuse to sell a short call whose expiry falls on/after any of these dates,
# to reduce early-assignment risk around dividend payments. Update this list
# periodically (e.g. quarterly) for dividend payers.
SYMBOLS = {
    "NVDA": {
        "leaps_min_delta": 0.90,
        "leaps_min_dte":   180,
        "short_min_delta": 0.25,
        "short_max_delta": 0.35,
        "short_min_dte":   20,
        "short_max_dte":   35,
        "profit_take_pct": 0.50,
        "max_loss_pct":    0.50,
        "ex_div_dates":    [],   # NVDA pays no dividend currently
    },
    "AVGO": {
        "leaps_min_delta": 0.90,
        "leaps_min_dte":   180,
        "short_min_delta": 0.25,
        "short_max_delta": 0.35,
        "short_min_dte":   20,
        "short_max_dte":   35,
        "profit_take_pct": 0.50,
        "max_loss_pct":    0.50,
        # TODO: update each quarter from AVGO's investor relations / broker calendar
        "ex_div_dates":    ["2026-09-21", "2026-12-21"],
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERTS
# ═══════════════════════════════════════════════════════════════════════════════

def send_telegram(message: str):
    """Send a Telegram alert message."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error(f"Telegram error: {resp.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# TIGER BROKERS CONNECTION
# ═══════════════════════════════════════════════════════════════════════════════

def get_clients():
    """Initialise and return (trade_client, quote_client)."""
    config = TigerOpenClientConfig(sandbox_debug=PAPER_MODE)
    config.tiger_id    = TIGER_ID
    config.account     = ACCOUNT
    config.private_key = PRIVATE_KEY
    config.license     = LICENSE
    return TradeClient(config), QuoteClient(config)


# ═══════════════════════════════════════════════════════════════════════════════
# MARKET HOURS CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def is_market_open() -> bool:
    """Return True if US market is currently open (SGT-aware)."""
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    if now_et.weekday() >= 5:          # Saturday / Sunday
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et <= market_close


# ═══════════════════════════════════════════════════════════════════════════════
# OPTION CHAIN HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def days_to_expiry(expiry_str: str) -> int:
    """Return calendar days until expiry. expiry_str format: 'YYYY-MM-DD'."""
    exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    return (exp - date.today()).days


def expiry_crosses_ex_div(expiry_str: str, ex_div_dates: list) -> bool:
    """
    Return True if this expiry falls on/after any configured ex-dividend date
    that hasn't passed yet — i.e. selling a short call expiring after that
    date carries early-assignment risk around the dividend.
    """
    exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    today = date.today()
    for ex_div_str in ex_div_dates:
        ex_div = datetime.strptime(ex_div_str, "%Y-%m-%d").date()
        if today <= ex_div <= exp:
            return True
    return False


def find_leaps(quote_client, symbol: str, params: dict):
    """
    Scan option chain for a deep ITM LEAPS call:
    - DTE >= params['leaps_min_dte']
    - delta >= params['leaps_min_delta']
    Returns the best contract dict or None.
    """
    log.info(f"[{symbol}] Scanning for LEAPS...")
    try:
        expirations = quote_client.get_option_expirations(symbol)
        candidates = []
        for exp in expirations:
            dte = days_to_expiry(exp)
            if dte < params["leaps_min_dte"]:
                continue
            chain = quote_client.get_option_chain(symbol, exp)
            for contract in chain:
                if contract.get("call_put") != "CALL":
                    continue
                delta = abs(contract.get("delta", 0))
                if delta >= params["leaps_min_delta"]:
                    candidates.append({
                        "symbol":     contract["contract_code"],
                        "expiry":     exp,
                        "strike":     contract["strike"],
                        "delta":      delta,
                        "dte":        dte,
                        "ask":        contract.get("ask", 0),
                        "bid":        contract.get("bid", 0),
                    })
        if not candidates:
            log.warning(f"[{symbol}] No LEAPS candidates found.")
            return None
        best = sorted(candidates, key=lambda x: (x["delta"], x["dte"]), reverse=True)[0]
        log.info(f"[{symbol}] LEAPS found: {best['symbol']} | Strike {best['strike']} | "
                 f"DTE {best['dte']} | Delta {best['delta']:.2f}")
        return best
    except Exception as e:
        log.error(f"[{symbol}] Error finding LEAPS: {e}")
        return None


def find_short_call(quote_client, symbol: str, params: dict):
    """
    Scan option chain for the short call to sell:
    - DTE within params['short_min_dte']..params['short_max_dte']
    - delta within params['short_min_delta']..params['short_max_delta']
    - expiry must NOT cross a configured ex-dividend date
    Returns best contract dict or None.
    """
    log.info(f"[{symbol}] Scanning for short call...")
    try:
        expirations = quote_client.get_option_expirations(symbol)
        candidates = []
        for exp in expirations:
            dte = days_to_expiry(exp)
            if not (params["short_min_dte"] <= dte <= params["short_max_dte"]):
                continue
            if expiry_crosses_ex_div(exp, params["ex_div_dates"]):
                log.info(f"[{symbol}] Skipping expiry {exp} — crosses ex-dividend date")
                continue
            chain = quote_client.get_option_chain(symbol, exp)
            for contract in chain:
                if contract.get("call_put") != "CALL":
                    continue
                delta = abs(contract.get("delta", 0))
                if params["short_min_delta"] <= delta <= params["short_max_delta"]:
                    candidates.append({
                        "symbol":   contract["contract_code"],
                        "expiry":   exp,
                        "strike":   contract["strike"],
                        "delta":    delta,
                        "dte":      dte,
                        "bid":      contract.get("bid", 0),
                        "ask":      contract.get("ask", 0),
                        "mid":      round((contract.get("bid", 0) + contract.get("ask", 0)) / 2, 2),
                    })
        if not candidates:
            log.warning(f"[{symbol}] No short call candidates found.")
            return None
        best = sorted(candidates, key=lambda x: abs(x["delta"] - 0.30))[0]
        log.info(f"[{symbol}] Short call found: {best['symbol']} | Strike {best['strike']} | "
                 f"DTE {best['dte']} | Delta {best['delta']:.2f} | Mid ${best['mid']}")
        return best
    except Exception as e:
        log.error(f"[{symbol}] Error finding short call: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════

def buy_leaps(trade_client, symbol: str, contract: dict):
    """Place a limit buy order for the LEAPS call at mid price."""
    mid = round((contract["bid"] + contract["ask"]) / 2, 2)
    try:
        order = limit_order(
            account=ACCOUNT,
            contract=contract["symbol"],
            action="BUY",
            quantity=1,
            limit_price=mid
        )
        oid = trade_client.place_order(order)
        msg = (f"🟢 <b>[{symbol}] LEAPS Bought [{MODE_TAG}]</b>\n"
               f"Symbol: {contract['symbol']}\n"
               f"Strike: {contract['strike']} | DTE: {contract['dte']}\n"
               f"Delta: {contract['delta']:.2f} | Price: ${mid}\n"
               f"Order ID: {oid}")
        log.info(msg)
        send_telegram(msg)
        return oid
    except Exception as e:
        log.error(f"[{symbol}] Error buying LEAPS: {e}")
        send_telegram(f"❌ [{symbol}] LEAPS buy failed: {e}")
        return None


def sell_short_call(trade_client, symbol: str, contract: dict):
    """Place a limit sell order for the short call at mid price."""
    mid = contract["mid"]
    try:
        order = limit_order(
            account=ACCOUNT,
            contract=contract["symbol"],
            action="SELL",
            quantity=1,
            limit_price=mid
        )
        oid = trade_client.place_order(order)
        msg = (f"📤 <b>[{symbol}] Short Call Sold [{MODE_TAG}]</b>\n"
               f"Symbol: {contract['symbol']}\n"
               f"Strike: {contract['strike']} | DTE: {contract['dte']}\n"
               f"Delta: {contract['delta']:.2f} | Premium: ${mid}\n"
               f"Order ID: {oid}")
        log.info(msg)
        send_telegram(msg)
        return oid, mid
    except Exception as e:
        log.error(f"[{symbol}] Error selling short call: {e}")
        send_telegram(f"❌ [{symbol}] Short call sell failed: {e}")
        return None, None


def close_short_call(trade_client, symbol: str, contract_symbol: str, limit_price: float, reason: str):
    """Buy back the short call to close it."""
    try:
        order = limit_order(
            account=ACCOUNT,
            contract=contract_symbol,
            action="BUY",
            quantity=1,
            limit_price=limit_price
        )
        oid = trade_client.place_order(order)
        msg = (f"🔴 <b>[{symbol}] Short Call Closed [{MODE_TAG}] — {reason}</b>\n"
               f"Symbol: {contract_symbol}\n"
               f"Close Price: ${limit_price}\n"
               f"Order ID: {oid}")
        log.info(msg)
        send_telegram(msg)
        return oid
    except Exception as e:
        log.error(f"[{symbol}] Error closing short call: {e}")
        send_telegram(f"❌ [{symbol}] Close short call failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION MONITORING
# ═══════════════════════════════════════════════════════════════════════════════

def get_current_price(quote_client, contract_symbol: str) -> float:
    """Get current mid price of an option contract."""
    try:
        quotes = quote_client.get_quote_real_time([contract_symbol])
        if quotes:
            q = quotes[0]
            return round((q.get("bid", 0) + q.get("ask", 0)) / 2, 2)
    except Exception as e:
        log.error(f"Error getting price for {contract_symbol}: {e}")
    return 0.0


def monitor_short_call(trade_client, quote_client, symbol: str, params: dict, state: dict):
    """
    Check the short call position and act if:
    - Profit >= profit_take_pct of premium → close for profit, sell new short call
    - Loss >= max_loss_pct OR delta > 0.50 (ITM) → roll to new short call
    state keys: short_symbol, premium_collected, short_delta, short_strike
    """
    if not state.get("short_symbol"):
        return state

    current_price = get_current_price(quote_client, state["short_symbol"])
    if current_price == 0:
        return state

    premium    = state["premium_collected"]
    profit     = premium - current_price
    loss       = current_price - premium
    profit_pct = profit / premium if premium else 0
    loss_pct   = loss / premium if premium else 0

    log.info(f"[{symbol}] Short call monitor | Symbol: {state['short_symbol']} | "
             f"Premium: ${premium} | Current: ${current_price} | "
             f"P&L: ${profit:.2f} ({profit_pct*100:.1f}%)")

    try:
        chain_quote = quote_client.get_quote_real_time([state["short_symbol"]])
        current_delta = abs(chain_quote[0].get("delta", state["short_delta"])) if chain_quote else state["short_delta"]
    except Exception:
        current_delta = state["short_delta"]

    def roll(reason):
        close_short_call(trade_client, symbol, state["short_symbol"], current_price, reason)
        new_short = find_short_call(quote_client, symbol, params)
        if new_short:
            oid, new_premium = sell_short_call(trade_client, symbol, new_short)
            if oid:
                state["short_symbol"]      = new_short["symbol"]
                state["premium_collected"] = new_premium
                state["short_delta"]       = new_short["delta"]
                state["short_strike"]      = new_short["strike"]
            else:
                # Couldn't open a replacement (e.g. ex-div blackout) — clear state
                state["short_symbol"] = None
        else:
            state["short_symbol"] = None

    if profit_pct >= params["profit_take_pct"]:
        log.info(f"[{symbol}] 🎯 Profit target hit — closing short call and rolling")
        roll("Profit Target")
        return state

    if loss_pct >= params["max_loss_pct"] or current_delta > 0.50:
        reason = "ITM (delta > 0.50)" if current_delta > 0.50 else "Max Loss"
        log.info(f"[{symbol}] ⚠️ Rolling short call — {reason}")
        roll(reason)
        return state

    return state


# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP SANITY CHECK — buying power vs. estimated LEAPS cost
# ═══════════════════════════════════════════════════════════════════════════════

def get_buying_power(trade_client) -> float:
    """
    Return available buying power (USD) on the account.
    tigeropen's asset summary field names vary slightly by account type, so
    we try a couple of common attributes before giving up.
    """
    try:
        assets = trade_client.get_assets(account=ACCOUNT)
        if not assets:
            return 0.0
        summary = assets[0].summary if hasattr(assets[0], "summary") else assets[0]
        for attr in ("buying_power", "cash", "available_funds"):
            value = getattr(summary, attr, None)
            if value is not None:
                return float(value)
        log.warning("Could not find a recognizable buying power field on account summary.")
        return 0.0
    except Exception as e:
        log.error(f"Error fetching buying power: {e}")
        return 0.0


def estimate_leaps_cost(quote_client, symbol: str, params: dict) -> float:
    """
    Scan for the LEAPS this symbol would buy and estimate total cost
    (mid price * 100 shares per contract). Returns 0 if none found.
    """
    leaps = find_leaps(quote_client, symbol, params)
    if not leaps:
        return 0.0
    mid = round((leaps["bid"] + leaps["ask"]) / 2, 2)
    return mid * 100


def preflight_buying_power_check(trade_client, quote_client, states: dict) -> bool:
    """
    Before live trading starts, estimate the combined cost of opening a LEAPS
    position in every symbol that doesn't already have one, and compare against
    available buying power. Sends a Telegram alert either way.
    Returns True if it's safe to proceed, False if the bot should halt.
    """
    log.info("Running pre-flight buying power check...")
    buying_power = get_buying_power(trade_client)

    total_estimated_cost = 0.0
    breakdown_lines = []
    for symbol, params in SYMBOLS.items():
        if states[symbol]["leaps_purchased"]:
            breakdown_lines.append(f"{symbol}: already holding LEAPS, $0 needed")
            continue
        cost = estimate_leaps_cost(quote_client, symbol, params)
        total_estimated_cost += cost
        breakdown_lines.append(f"{symbol}: ~${cost:,.2f} (est. LEAPS cost)")

    breakdown = "\n".join(breakdown_lines)
    log.info(f"Buying power: ${buying_power:,.2f} | Estimated LEAPS cost: ${total_estimated_cost:,.2f}")

    if buying_power <= 0:
        msg = ("⚠️ <b>Pre-flight check could not confirm buying power</b>\n"
               "Could not read account buying power from Tiger API — proceeding with caution.\n"
               f"{breakdown}")
        log.warning(msg)
        send_telegram(msg)
        return True  # don't hard-block on an API read issue, but warn loudly

    if total_estimated_cost > buying_power:
        msg = (f"🛑 <b>Pre-flight check FAILED [{MODE_TAG}] — insufficient buying power</b>\n"
               f"Available: ${buying_power:,.2f}\n"
               f"Estimated need: ${total_estimated_cost:,.2f}\n{breakdown}\n"
               "Bot halted before placing any live orders.")
        log.error(msg)
        send_telegram(msg)
        return False

    msg = (f"✅ <b>Pre-flight check passed [{MODE_TAG}]</b>\n"
           f"Available: ${buying_power:,.2f}\n"
           f"Estimated need: ${total_estimated_cost:,.2f}\n{breakdown}")
    log.info(msg)
    send_telegram(msg)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def init_state():
    return {
        "leaps_symbol":      None,
        "short_symbol":      None,
        "premium_collected": 0.0,
        "short_delta":       0.0,
        "short_strike":      0.0,
        "leaps_purchased":   False,
    }


def process_symbol(trade_client, quote_client, symbol: str, params: dict, state: dict) -> dict:
    """Run one check cycle for a single symbol; returns updated state."""
    # Step 1: Buy LEAPS if we don't have one
    if not state["leaps_purchased"]:
        log.info(f"[{symbol}] No LEAPS position — scanning to buy...")
        leaps = find_leaps(quote_client, symbol, params)
        if leaps:
            oid = buy_leaps(trade_client, symbol, leaps)
            if oid:
                state["leaps_symbol"]    = leaps["symbol"]
                state["leaps_purchased"] = True
                log.info(f"[{symbol}] LEAPS purchased — waiting for fill before selling short call...")
                time.sleep(60)

    # Step 2: Sell short call if we have LEAPS but no short call
    if state["leaps_purchased"] and not state["short_symbol"]:
        log.info(f"[{symbol}] No short call — scanning to sell...")
        short = find_short_call(quote_client, symbol, params)
        if short:
            oid, premium = sell_short_call(trade_client, symbol, short)
            if oid:
                state["short_symbol"]      = short["symbol"]
                state["premium_collected"] = premium
                state["short_delta"]       = short["delta"]
                state["short_strike"]      = short["strike"]

    # Step 3: Monitor existing short call
    if state["short_symbol"]:
        state = monitor_short_call(trade_client, quote_client, symbol, params, state)

    return state


def main():
    log.info("=" * 60)
    log.info(f"PMCC Bot starting [{MODE_TAG}] — {', '.join(SYMBOLS.keys())} | Tiger Brokers")
    log.info("=" * 60)
    send_telegram(
        f"🤖 <b>PMCC Bot Started [{MODE_TAG}]</b>\n"
        f"Symbols: {', '.join(SYMBOLS.keys())}\n"
        "Strategy: Poor Man's Covered Call"
    )

    trade_client, quote_client = get_clients()

    # One state dict per symbol
    states = {symbol: init_state() for symbol in SYMBOLS}

    # ── Pre-flight: confirm buying power before placing any live orders ──────
    if not preflight_buying_power_check(trade_client, quote_client, states):
        log.error("Halting startup — insufficient buying power for planned LEAPS positions.")
        return

    while True:
        try:
            if not is_market_open():
                log.info("Market closed — waiting...")
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            log.info(f"--- Checking positions [{datetime.now().strftime('%Y-%m-%d %H:%M')}] ---")

            for symbol, params in SYMBOLS.items():
                try:
                    states[symbol] = process_symbol(
                        trade_client, quote_client, symbol, params, states[symbol]
                    )
                except Exception as e:
                    log.error(f"[{symbol}] Unexpected error processing symbol: {e}")
                    send_telegram(f"⚠️ <b>[{symbol}] Bot Error</b>\n{e}")

        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}")
            send_telegram(f"⚠️ <b>Bot Error</b>\n{e}")

        log.info(f"Sleeping {CHECK_INTERVAL_SEC // 60} minutes until next check...")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
