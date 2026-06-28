"""
Tiger Brokers Open API - Configuration Setup
Tiger ID: 20160186 | Account: 50353018 | License: TBSG | Env: PROD
"""

from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient
from tigeropen.quote.quote_client import QuoteClient

# ── Credentials (from your tiger_openapi_config.properties) ──────────────────

TIGER_ID    = "20160186"
ACCOUNT     = "50353018"
LICENSE     = "TBSG"

# PK8 format is required by the tigeropen SDK
PRIVATE_KEY = """MIICdgIBADANBgkqhkiG9w0BAQEFAASCAmAwggJcAgEAAoGBAKsNpys9BqQU+GuG
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


def get_config() -> TigerOpenClientConfig:
    """Returns a configured TigerOpenClientConfig ready for use."""
    config = TigerOpenClientConfig(sandbox_debug=False)
    config.tiger_id    = TIGER_ID
    config.account     = ACCOUNT
    config.private_key = PRIVATE_KEY
    config.license     = LICENSE
    return config


def get_trade_client() -> TradeClient:
    """Returns an authenticated TradeClient."""
    return TradeClient(get_config())


def get_quote_client() -> QuoteClient:
    """Returns an authenticated QuoteClient."""
    return QuoteClient(get_config())


# ── Quick connection test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Testing Tiger Brokers API connection...")

    try:
        trade_client = get_trade_client()
        assets = trade_client.get_assets(account=ACCOUNT)
        print("✅ Connection successful!")
        print("Account assets:", assets)
    except Exception as e:
        print("❌ Connection failed:", e)

    try:
        quote_client = get_quote_client()
        quotes = quote_client.get_quote_real_time(['AAPL', 'MSFT'])
        print("✅ Market data working!")
        print("Quotes:", quotes)
    except Exception as e:
        print("❌ Market data failed:", e)
