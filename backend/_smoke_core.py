"""Smoke test for the core layer: database, security, session, indicators."""

from backend.core.database import initialise, app_cursor
from backend.core.security import (
    hash_password, verify_password, create_access_token, decode_access_token, generate_api_key, verify_api_key,
)
from backend.marketdata import market_hours as mh

initialise()

h = hash_password("s3cret!")
assert verify_password("s3cret!", h) and not verify_password("wrong", h)
token, _ = create_access_token(subject="a@b.c", role="admin", account_id="X1")
assert decode_access_token(token).subject == "a@b.c"
try:
    decode_access_token(token[:-4] + "AAAA")
    raise AssertionError("tampered token accepted")
except ValueError:
    pass
raw_key, key_hash = generate_api_key()
assert verify_api_key(raw_key, key_hash)
print("security: OK")

state = mh.session_state()
print("session:", state["state"], "-", state["reason"])
print("breadth on empty set:", mh.breadth([])["priced"], "priced")

with app_cursor() as cursor:
    tables = [row["name"] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("tables:", len(tables), tables[:8])

from backend.marketdata import ticks
print("intervals:", ticks.supported_intervals())
print("coverage (no data yet):", ticks.coverage("26000", "nse_cm")["has_history"])
