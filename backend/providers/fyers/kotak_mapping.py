"""Task 6: Kotak Order Mapping — translate Fyers symbol to Kotak instrument_token.
DO NOT modify execution/order_service.py (Kotak Neo)."""
# Fyers -> Kotak lookup table (populated from symbol master CSV / DB)
MAP = {
    "NSE:NIFTY26OCT23100CE": "408065",  # example Kotak token
    "NSE:SBIN-EQ": "2885123",
}

def fyers_to_kotak_token(fyers_symbol: str) -> str:
    # Before sending to Kotak Neo (execution/order_service.py), translate
    return MAP.get(fyers_symbol, fyers_symbol)
