"""Smoke test for the Kotak Neo websocket feed: frame normalisation → quote cache.

Runs standalone (like _smoke_core.py) with synthetic HSM frames, exactly matching
the wire keys the vendored neo_api_client emits from HSWebSocketLib's
SCRIP_MAPPING / INDEX_MAPPING. No credentials or network access are needed.
"""

import os
import time

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-local-verification")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "test-encryption-key-for-local-verification")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-local-verification")
os.environ.setdefault("KOTAK_KEEPALIVE", "0")

from backend.marketdata.feed import MarketFeed, normalise_frame, _bare
from backend.marketdata.quotes import quote_engine

# ---------------------------------------------------------------- scrip frame
# Exact keys from SCRIP_MAPPING: ltp/ltq/tbq/tsq/bp/sp/bq/bs/oi/op/c/...
scrip_raw = {
    "tk": "11536", "e": "nse_cm", "ts": "RELIANCE-EQ", "name": "SCRIP",
    "ltp": "2951.4000", "ltq": "25", "tbq": "104320", "tsq": "88210",
    "bp": "2951.3500", "sp": "2951.4500", "bq": "410", "bs": "366",
    "ap": "2948.1200", "oi": "0", "op": "2930.0000", "h": "2959.9000",
    "lo": "2925.1000", "c": "2922.7500", "v": "1847233", "cng": "28.6500",
    "nc": "0.9803", "ucl": "3214.0000", "lcl": "2631.0000",
    "yh": "3217.6000", "yl": "2220.3000",
}
frame = normalise_frame(scrip_raw)
assert frame is not None, "scrip frame was dropped"
assert frame["token"] == "11536" and frame["exchange_segment"] == "nse_cm"
assert frame["last"] == 2951.40, frame["last"]
assert frame["bid"] == 2951.35 and frame["ask"] == 2951.45, (frame.get("bid"), frame.get("ask"))
assert frame["bid_quantity"] == 410 and frame["ask_quantity"] == 366
assert frame["previous_close"] == 2922.75, frame.get("previous_close")
assert frame["change_percent"] == 0.9803, frame.get("change_percent")
assert frame["week52_high"] == 3217.60 and frame["upper_circuit"] == 3214.00
print("scrip normalise: OK", {k: frame[k] for k in ("last", "bid", "ask", "previous_close")})

# --------------------------------------------------------------- index frame
# Exact keys from INDEX_MAPPING: iv = LTP, ic = previous close (no ltp!).
index_raw = {
    "tk": "26000", "e": "nse_cm", "name": "INDEX",
    "iv": "24812.3500", "ic": "24750.1000", "openingPrice": "24760.0000",
    "highPrice": "24830.7000", "lowPrice": "24741.2500",
    "cng": "62.2500", "nc": "0.2515",
}
frame = normalise_frame(index_raw)
assert frame is not None, "index frame was dropped"
assert frame["last"] == 24812.35, frame["last"]
assert frame["previous_close"] == 24750.10, frame.get("previous_close")
assert frame["open"] == 24760.00 and frame["high"] == 24830.70
print("index normalise: OK", {k: frame[k] for k in ("last", "previous_close", "change_percent")})

# --------------------------------------------- frame → quote engine round-trip
frame = normalise_frame(scrip_raw)
quote = quote_engine.apply_tick(frame)
assert quote is not None, "apply_tick returned nothing"
assert quote.last == 2951.40, f"quote.last lost in round-trip: {quote.last}"
assert quote.bid == 2951.35 and quote.ask == 2951.45
assert quote.previous_close == 2922.75
assert quote.change == round(2951.40 - 2922.75, 4), quote.change
assert quote.change_percent == round(((2951.40 - 2922.75) / 2922.75) * 100, 4)
assert quote.volume == 1847233 and quote.open_interest == 0
assert not quote.is_stale
print("quote round-trip: OK", quote.to_dict()["last"], quote.to_dict()["age_seconds"], "s old")

# Deltas: a second frame carrying only ltp must merge, not wipe, the quote.
quote2 = quote_engine.apply_tick(normalise_frame({"tk": "11536", "e": "nse_cm", "ltp": "2952.0000"}))
assert quote2.last == 2952.00, quote2.last
assert quote2.bid == 2951.35, f"delta wiped bid: {quote2.bid}"
print("delta merge: OK")

# ------------------------------------------------------- SDK payload handling
feed = MarketFeed()
feed._on_message({"type": "stock_feed", "data": [scrip_raw]})
assert feed.state.frames_received == 1, feed.state.frames_received
# Non-JSON control strings ("Un-Subscribed Successfully!") must be ignored.
feed._on_message("Un-Subscribed Successfully!")
# Depth frames without an LTP are ignored rather than crashing.
feed._on_message({"type": "stock_feed", "data": [{"tk": "11536", "bp": "2951.35"}]})
assert feed.state.frames_received == 1, feed.state.frames_received
print("payload handling: OK", feed.state.frames_received, "frames")

# ------------------------------------------------- subscription bookkeeping
added = feed.subscribe([
    {"instrument_token": "11536", "exchange_segment": "nse_cm"},
    {"instrument_token": "26000", "exchange_segment": "nse_cm", "is_index": True},
])
assert len(added) == 2
assert feed.subscription_tokens() == ["11536", "26000"] or set(feed.subscription_tokens()) == {"11536", "26000"}
rebuilt = feed._rebuild_subscriptions()
by_token = {item["instrument_token"]: item for item in rebuilt}
assert by_token["11536"]["is_index"] is False
assert by_token["26000"]["is_index"] is True, "index flag lost on rebuild"
assert _bare(rebuilt) == [
    {"instrument_token": "11536", "exchange_segment": "nse_cm"},
    {"instrument_token": "26000", "exchange_segment": "nse_cm"},
]
removed = feed.unsubscribe(["26000"])
assert removed == [{"instrument_token": "26000", "exchange_segment": "nse_cm"}]
assert "26000" not in feed._is_index, "is_index entry leaked"
print("subscription bookkeeping: OK")

health = feed.health()
assert health["status"] == "disconnected" and health["subscribed_instruments"] == 1
print("health: OK", health)

print("kotak websocket feed smoke: ALL OK")