"""
PMCC Bot — Poor Man's Covered Call
Stock:       NVDA
Broker:      Tiger Brokers (Open API)
LEAPS delta: 0.90+ (deep ITM)
Short call:  0.25–0.35 delta, 20–35 DTE
Profit take: Close short call at 50% of premium received
Roll short:  When ITM or loss hits 50% of premium received
Check freq:  Every 30 minutes during market hours
Telegram:    Alerts to +6591265645
"""

import time
import logging
import requests
from datetime import datetime, date
import pytz

# ── Tiger Brokers config (from tiger_config.py) ───────────────────────────────
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.quote.quote_client import QuoteClient
from tigeropen.common.util.order_utils import market_order, limit_order
from tigeropen.common.util.contract_utils import option_contract_by_symbol

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
# CONFIGURATION — edit these values
# ═══════════════════════════════════════════════════════════════════════════════

TIGER_ID     = "20160186"
ACCOUNT      = "50353018"
LICENSE      = "TBSG"
PRIVATE_KEY  = """MIICdgIBADANBgkqhkiG9w0BAQEFAASCAmAwggJcAgEAAoGBAKsNpys9BqQU+GuG
kuzp+QvIOu0VjzqX3konVODAL8QgYDEBykc6OAvGfEklyGFvyAcEQM/euqBBkW6W
72tEfIuzlKcnZsk8Neleuuqspwjy9bAdkvCs9vZ4j2pdQRWvzCSfFhVlAGt3fWuI
OCgHEciuMYYRTD916oOV6FuIUIwPAgMBAAECgYAfgBsp/koLy4TYIGdMU+Y2QkB/
yrmeu7sHAulBnoLtZlzwiXjb1x/dI0deHSQitXgrup/I6CaMPqbuq8MZiPo6XtLb
ATk/gsRrfrjPnWMCAZ0+qE/m+erWNPEiX87ODzSQzpCscJyF4sCefaUYZZjzsRXn
CCY7KDoS9PAdKI7T8QJBANe3l7/k7KEYGH/V2LofKDKO6lVY1bsExh5VEI8OENBROtTOv
+OHq1fgepEHvbJc6h0x3lmc7yFf4lsZ+iGG3hcCQQDK/uVcmWl4ZRoMAUC8R7SI
rIKCUtpRYUUqXiuLcX1Eut2BiOCE3cUJ5zJ175JB15uEN4CPa4l2xZKCPpuXXLTJ
AkBWRxPmqEUMWXrTBlDcgEGvlwGaiSFS36Ht18/7p4CKETMakmalNkoNp7bd8t6o
TAlHC/8GkIIEMzlxfn5QkoSZAkA42Z3+ivBgyV+8EPXCRQqoZDfAq9d8hxNJxEnJ
qaT9hJ/YUS8fxsQR++/D265IRkvFgY29nM5ItxhK5aHJiCsRAkEA0IGgMWxoXWg8
aqrXCjfcFa9S5chpiLXrj5tpERL83C8a0Du/cdFmMGkz9dyd8yD3m+mCLdU7+LKR
f4fOCZUbsQ=="""

# ── Strategy parameters ───────────────────────────────────────────────────────
SYMBOL              = "NVDA"
LEAPS_MIN_DELTA     = 0.90          # Buy LEAPS with delta >= this
LEAPS_MIN_DTE       = 180           # LEAPS must have at least 6 months
SHORT_MIN_DELTA     = 0.25          # Short call delta range
SHORT_MAX_DELTA     = 0.35
SHORT_MIN_DTE       = 20            # Short call DTE range
SHORT_MAX_DTE       = 35
PROFIT_TAKE_PCT     = 0.50          # Close short at 50% profit
MAX_LOSS_PCT        = 0.50          # Roll short if loss hits 50% of premium
CHECK_INTERVAL_SEC  = 30 * 60       # 30 minutes

# ── Telegram ──────────────────────────────────────────────────────────────────
# To get your chat_id:
# 1. Search @userinfobot on Telegram and send /start
# 2. It will reply with your chat ID — paste it below
TELEGRAM_BOT_TOKEN  = "8776442900:AAFcSktanlFW1FvVDK3QngaL1BgFNdJBc80"
TELEGRAM_CHAT_ID    = "873839651"

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERTS
# ═══════════════════════════════════════════════════════════════════════════════

def send_telegram(message: str):
    """Send a Telegram alert message."""
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.warning("Telegram not configured — skipping alert")
        return
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
    config = TigerOpenClientConfig(sandbox_debug=False)
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


def find_leaps(quote_client, symbol: str):
    """
    Scan option chain for a deep ITM LEAPS call:
    - DTE >= LEAPS_MIN_DTE (180 days)
    - delta >= LEAPS_MIN_DELTA (0.90)
    Returns the best contract dict or None.
    """
    log.info(f"Scanning for LEAPS on {symbol}...")
    try:
        expirations = quote_client.get_option_expirations(symbol)
        candidates = []
        for exp in expirations:
            dte = days_to_expiry(exp)
            if dte < LEAPS_MIN_DTE:
                continue
            chain = quote_client.get_option_chain(symbol, exp)
            for contract in chain:
                if contract.get("call_put") != "CALL":
                    continue
                delta = abs(contract.get("delta", 0))
                if delta >= LEAPS_MIN_DELTA:
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
            log.warning("No LEAPS candidates found.")
            return None
        # Pick highest delta (deepest ITM), longest DTE as tiebreaker
        best = sorted(candidates, key=lambda x: (x["delta"], x["dte"]), reverse=True)[0]
        log.info(f"LEAPS found: {best['symbol']} | Strike {best['strike']} | DTE {best['dte']} | Delta {best['delta']:.2f}")
        return best
    except Exception as e:
        log.error(f"Error finding LEAPS: {e}")
        return None


def find_short_call(quote_client, symbol: str):
    """
    Scan option chain for the short call to sell:
    - DTE 20–35 days
    - delta 0.25–0.35
    Returns best contract dict or None.
    """
    log.info(f"Scanning for short call on {symbol}...")
    try:
        expirations = quote_client.get_option_expirations(symbol)
        candidates = []
        for exp in expirations:
            dte = days_to_expiry(exp)
            if not (SHORT_MIN_DTE <= dte <= SHORT_MAX_DTE):
                continue
            chain = quote_client.get_option_chain(symbol, exp)
            for contract in chain:
                if contract.get("call_put") != "CALL":
                    continue
                delta = abs(contract.get("delta", 0))
                if SHORT_MIN_DELTA <= delta <= SHORT_MAX_DELTA:
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
            log.warning("No short call candidates found.")
            return None
        # Pick closest delta to 0.30 (midpoint of 0.25–0.35)
        best = sorted(candidates, key=lambda x: abs(x["delta"] - 0.30))[0]
        log.info(f"Short call found: {best['symbol']} | Strike {best['strike']} | DTE {best['dte']} | Delta {best['delta']:.2f} | Mid ${best['mid']}")
        return best
    except Exception as e:
        log.error(f"Error finding short call: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════

def buy_leaps(trade_client, contract: dict):
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
        msg = (f"🟢 <b>LEAPS Bought</b>\n"
               f"Symbol: {contract['symbol']}\n"
               f"Strike: {contract['strike']} | DTE: {contract['dte']}\n"
               f"Delta: {contract['delta']:.2f} | Price: ${mid}\n"
               f"Order ID: {oid}")
        log.info(msg)
        send_telegram(msg)
        return oid
    except Exception as e:
        log.error(f"Error buying LEAPS: {e}")
        send_telegram(f"❌ LEAPS buy failed: {e}")
        return None


def sell_short_call(trade_client, contract: dict):
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
        msg = (f"📤 <b>Short Call Sold</b>\n"
               f"Symbol: {contract['symbol']}\n"
               f"Strike: {contract['strike']} | DTE: {contract['dte']}\n"
               f"Delta: {contract['delta']:.2f} | Premium: ${mid}\n"
               f"Order ID: {oid}")
        log.info(msg)
        send_telegram(msg)
        return oid, mid   # return order id and premium collected
    except Exception as e:
        log.error(f"Error selling short call: {e}")
        send_telegram(f"❌ Short call sell failed: {e}")
        return None, None


def close_short_call(trade_client, contract_symbol: str, limit_price: float, reason: str):
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
        msg = (f"🔴 <b>Short Call Closed — {reason}</b>\n"
               f"Symbol: {contract_symbol}\n"
               f"Close Price: ${limit_price}\n"
               f"Order ID: {oid}")
        log.info(msg)
        send_telegram(msg)
        return oid
    except Exception as e:
        log.error(f"Error closing short call: {e}")
        send_telegram(f"❌ Close short call failed: {e}")
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


def monitor_short_call(trade_client, quote_client, state: dict):
    """
    Check the short call position and act if:
    - Profit >= 50% of premium → close for profit, sell new short call
    - Loss >= 50% of premium OR delta > 0.50 (ITM) → roll to new short call
    state keys: short_symbol, premium_collected, short_delta, short_strike
    """
    if not state.get("short_symbol"):
        return state

    current_price = get_current_price(quote_client, state["short_symbol"])
    if current_price == 0:
        return state

    premium       = state["premium_collected"]
    profit        = premium - current_price          # positive = profit (we sold, now cheaper)
    loss          = current_price - premium          # positive = loss (now more expensive)
    profit_pct    = profit / premium if premium else 0
    loss_pct      = loss / premium if premium else 0

    log.info(f"Short call monitor | Symbol: {state['short_symbol']} | "
             f"Premium: ${premium} | Current: ${current_price} | "
             f"P&L: ${profit:.2f} ({profit_pct*100:.1f}%)")

    # ── Check delta (ITM risk) ────────────────────────────────────────────────
    try:
        chain_quote = quote_client.get_quote_real_time([state["short_symbol"]])
        current_delta = abs(chain_quote[0].get("delta", state["short_delta"])) if chain_quote else state["short_delta"]
    except Exception:
        current_delta = state["short_delta"]

    # ── Profit take at 50% ───────────────────────────────────────────────────
    if profit_pct >= PROFIT_TAKE_PCT:
        log.info("🎯 Profit target hit — closing short call and rolling")
        close_short_call(trade_client, state["short_symbol"], current_price, "50% Profit Target")
        new_short = find_short_call(quote_client, SYMBOL)
        if new_short:
            oid, new_premium = sell_short_call(trade_client, new_short)
            if oid:
                state["short_symbol"]       = new_short["symbol"]
                state["premium_collected"]  = new_premium
                state["short_delta"]        = new_short["delta"]
                state["short_strike"]       = new_short["strike"]
        return state

    # ── Roll if max loss hit OR ITM (delta > 0.50) ───────────────────────────
    if loss_pct >= MAX_LOSS_PCT or current_delta > 0.50:
        reason = "ITM (delta > 0.50)" if current_delta > 0.50 else "50% Max Loss"
        log.info(f"⚠️ Rolling short call — {reason}")
        close_short_call(trade_client, state["short_symbol"], current_price, reason)
        new_short = find_short_call(quote_client, SYMBOL)
        if new_short:
            oid, new_premium = sell_short_call(trade_client, new_short)
            if oid:
                state["short_symbol"]       = new_short["symbol"]
                state["premium_collected"]  = new_premium
                state["short_delta"]        = new_short["delta"]
                state["short_strike"]       = new_short["strike"]
        return state

    return state


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    log.info("PMCC Bot starting — NVDA | Tiger Brokers")
    log.info("=" * 60)
    send_telegram("🤖 <b>PMCC Bot Started</b>\nStock: NVDA\nStrategy: Poor Man's Covered Call")

    trade_client, quote_client = get_clients()

    # State tracks the current positions
    state = {
        "leaps_symbol":       None,   # LEAPS contract we own
        "short_symbol":       None,   # Short call we've sold
        "premium_collected":  0.0,    # Premium received for current short call
        "short_delta":        0.0,
        "short_strike":       0.0,
        "leaps_purchased":    False,
    }

    while True:
        try:
            if not is_market_open():
                log.info("Market closed — waiting...")
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            log.info(f"--- Checking positions [{datetime.now().strftime('%Y-%m-%d %H:%M')}] ---")

            # ── Step 1: Buy LEAPS if we don't have one ───────────────────────
            if not state["leaps_purchased"]:
                log.info("No LEAPS position — scanning to buy...")
                leaps = find_leaps(quote_client, SYMBOL)
                if leaps:
                    oid = buy_leaps(trade_client, leaps)
                    if oid:
                        state["leaps_symbol"]    = leaps["symbol"]
                        state["leaps_purchased"] = True
                        log.info("LEAPS purchased — waiting for fill before selling short call...")
                        time.sleep(60)   # Give order time to fill

            # ── Step 2: Sell short call if we have LEAPS but no short call ───
            if state["leaps_purchased"] and not state["short_symbol"]:
                log.info("No short call — scanning to sell...")
                short = find_short_call(quote_client, SYMBOL)
                if short:
                    oid, premium = sell_short_call(trade_client, short)
                    if oid:
                        state["short_symbol"]      = short["symbol"]
                        state["premium_collected"] = premium
                        state["short_delta"]       = short["delta"]
                        state["short_strike"]      = short["strike"]

            # ── Step 3: Monitor existing short call ──────────────────────────
            if state["short_symbol"]:
                state = monitor_short_call(trade_client, quote_client, state)

        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}")
            send_telegram(f"⚠️ <b>Bot Error</b>\n{e}")

        log.info(f"Sleeping {CHECK_INTERVAL_SEC // 60} minutes until next check...")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
