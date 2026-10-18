"""A shared SmartAPI rate limiter.

Angel One enforces a hard cap of **3 requests per second** across the historical,
quote and LTP endpoints. Every module that talks to SmartAPI must share one
limiter, otherwise a candle backfill and the live quote feed can each believe
they are inside the budget while together they exceed it — which surfaces as
opaque ``AB1021``/``AB1010`` errors mid-session.

The limiter is a small token bucket: callers ask for permission with
:meth:`RateLimiter.wait` immediately before issuing the request. Tokens refill
at ``rate`` per second up to ``capacity``, so a short burst (a couple of chunked
historical calls) is allowed while the long-run average stays within the limit.
"""

from __future__ import annotations

import threading
import time

from backend.core.config import settings


class RateLimiter:
    """Token bucket limiting calls to ``rate`` per second."""

    def __init__(self, rate: float | None = None, capacity: float | None = None) -> None:
        self.rate = float(rate if rate is not None else settings.angel_rate_limit_per_second) or 1.0
        #: Allow a small burst without ever exceeding the long-run rate.
        self.capacity = float(capacity if capacity is not None else max(1.0, self.rate))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    def wait(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` may be spent, then spend them."""
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self._tokens < tokens:
                deficit = tokens - self._tokens
                time.sleep(deficit / self.rate)
                now = time.monotonic()
                self._refill(now)
            self._tokens = max(0.0, self._tokens - tokens)


#: The one limiter every SmartAPI caller shares.
angel_limiter = RateLimiter()
