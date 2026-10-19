"""Process-wide sliding-window limiter for Angel One SmartAPI requests."""

from __future__ import annotations

import threading
import time
from collections import deque

from backend.core.config import settings


class RateLimiter:
    """Strict sliding-window limiter; never permits more than ``rate`` calls/sec."""

    def __init__(self, rate: float | None = None, capacity: float | None = None) -> None:
        self.rate = max(1, int(rate if rate is not None else settings.angel_rate_limit_per_second))
        self.window = 1.0
        self.min_spacing = 0.35
        self._calls: deque[float] = deque(maxlen=self.rate)
        self._lock = threading.Lock()

    def wait(self, tokens: float = 1.0) -> None:
        """Wait until both the 3/sec window and minimum spacing are satisfied."""
        if tokens != 1.0:
            raise ValueError("The SmartAPI limiter accepts one request at a time")
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.window:
                    self._calls.popleft()
                spacing_wait = self.min_spacing - (now - self._calls[-1]) if self._calls else 0.0
                window_wait = self.window - (now - self._calls[0]) if len(self._calls) >= self.rate else 0.0
                delay = max(0.0, spacing_wait, window_wait)
                if delay == 0.0:
                    self._calls.append(now)
                    return
            time.sleep(delay)


#: The one limiter every SmartAPI caller shares.
angel_limiter = RateLimiter()
