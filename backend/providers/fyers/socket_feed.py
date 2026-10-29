"""Task 5: Replace Angel One WebSocket with fyers_apiv3.FyersDataSocket."""
from fyers_apiv3 import FyersDataSocket
import logging

log = logging.getLogger("fyres.socket")

class FyersSocketFeed:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.socket = FyersDataSocket(access_token=access_token, log_path="")

    def subscribe_watchlist(self, symbols: list):
        # Subscribe to Fyers symbols (e.g., NSE:SBIN-EQ, ATM strikes)
        self.socket.subscribe(symbols, ["LTP", "LTQ", "BidAsk"])

    def on_message(self, message):
        # Update frontend live prices / candlestick via WebSocket push
        log.info("Fyers tick: %s", message)
        # Broadcast to frontend through backend ws module (unmodified endpoint)
        # backend/api/ws.py handles emission; we just feed data here
