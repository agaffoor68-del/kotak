"""Background Advance/Decline calculator backed by Angel One quotes."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from backend.core.config import settings
from backend.marketdata import market_hours

from . import instruments
from .quotes import fetch

log = logging.getLogger("alphatrade.angel.breadth")


class MarketBreadthWorker:
    """Refresh a cached breadth snapshot without requiring a dashboard user."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] | None = None
        self._resolver: Callable[[], tuple[str | None, Callable[..., dict[str, Any]] | None]] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error: str | None = None

    def start(self, resolver: Callable[[], tuple[str | None, Callable[..., dict[str, Any]] | None]]) -> None:
        self._resolver = resolver
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="angel-breadth", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._snapshot) if self._snapshot else None

    def refresh_once(self) -> dict[str, Any] | None:
        if not self._resolver:
            return None
        _account_id, call = self._resolver()
        if call is None:
            return None
        universe = instruments.equity_universe("nse_cm", settings.breadth_universe_size)
        if not universe:
            return None
        # LTP mode does not include previous close; OHLC is required for A/D.
        quotes = fetch(call, [(row["token"], row["exchange_segment"]) for row in universe], "ohlc")
        snapshot = {
            "as_of": time.time(),
            "source": "angel_one",
            "universe_size": len(universe),
            "priced": sum(1 for quote in quotes if quote.last is not None),
            # Always publish a complete numeric breadth object so clients never
            # have to infer whether zero means "no data" or "no movers".
            "breadth": {"advances": 0, "declines": 0, "unchanged": 0, "priced": 0, **(market_hours.breadth(quotes) or {})},
            "top_gainers": market_hours.rank(quotes, limit=10, reverse=True),
            "top_losers": market_hours.rank(quotes, limit=10, reverse=False),
            "most_active": market_hours.most_active(quotes, limit=10),
        }
        with self._lock:
            self._snapshot = snapshot
            self.last_error = None
        return snapshot

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as error:  # noqa: BLE001
                self.last_error = str(error)
                log.warning("Market breadth refresh failed: %s", error)
            self._stop.wait(max(15, settings.breadth_refresh_seconds))


market_breadth_worker = MarketBreadthWorker()
