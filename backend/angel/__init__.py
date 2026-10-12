"""Angel One SmartAPI integration — the platform's market-data layer.

Angel One is the *data* broker: the instrument master, live quotes and
historical candles all come from SmartAPI (Kotak Neo publishes no historical
candles at all). Angel also implements the order surface, so the same package
backs one side of the dual-broker execution path.

Nothing here synthesises prices. When SmartAPI is unreachable the callers get
an error they can surface, not a placeholder number.
"""

from backend.angel.auth import AngelAuthError, angel_session_manager
from backend.angel import candles, feed, instruments, quotes

__all__ = [
    "AngelAuthError",
    "angel_session_manager",
    "candles",
    "feed",
    "instruments",
    "quotes",
]
