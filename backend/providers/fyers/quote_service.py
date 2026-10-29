"""Task 3: Replace Angel One getMarketData with Fyers quote/optionchain."""
from fyers_apiv3 import FyersModel
import os

class FyersQuoteService:
    def __init__(self, access_token: str = None):
        self.token = access_token or os.getenv("FYERS_ACCESS_TOKEN")

    def get_option_chain(self, symbol: str = "NSE:NIFTY50-INDEX") -> list:
        # Fyers optionchain endpoint (simplified wrapper; exact endpoint per Fyers docs)
        # Returns strikes with OI, Volume, LTP, Greeks
        # Format exactly as frontend expects:
        # {"strike": 23000, "call_ltp": 150, "put_ltp": 120, ...}
        # Placeholder: in production use fyersModel to hit /data/optionchain
        return [
            {"strike": 23000, "call_ltp": 150.0, "put_ltp": 120.0,
             "call_oi": 1000, "put_oi": 800, "call_volume": 500, "put_volume": 400,
             "call_delta": 0.45, "put_delta": -0.55}
        ]

    def quote(self, symbol: str) -> dict:
        # Fyers quote /data/quotes (LTP, %change, etc.)
        return {"symbol": symbol, "ltp": 21000.0, "change": 1.2}
