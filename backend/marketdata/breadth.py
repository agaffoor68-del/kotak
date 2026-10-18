"""Background market-breadth snapshots sourced from Angel One quotes."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from backend.angel import instruments as angel_instruments
from backend.angel.quotes import fetch as angel_fetch
from backend.marketdata import market_hours, ticks
from backend.marketdata.quotes import quote_engine

log = logging.getLogger("alphatrade.marketdata.breadth")


class BreadthWorker:
    """Continuously price a cash-equity universe and retain the latest breadth."""

    def __init__(self, universe_size: int = 200, interval_seconds: float = 60.0) -> None:
        self.universe_size = universe_size
        self.interval_seconds = interval_seconds
        self._resolver: Callable[[], tuple[str | None, Callable[..., dict[str, Any]] | None]] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] | None = None
        self._last_error: str | None = None

    def _run_once(self) -> None:
        universe = angel_instruments.equity_universe("nse_cm", self.universe_size)
        if not universe or self._resolver is None:
            return
        _account_id, call = self._resolver()
        if call is None:
            return
        quotes = angel_fetch(call, [(row["token"], row["exchange_segment"]) for row in universe], "ltp")
        for quote in quotes:
            quote_engine.store(quote)
            ticks.record_quote(quote)
        snapshot = {
            "as_of": time.time(),
            "source": "angel_one",
            "universe_size": len(universe),
            "priced": sum(quote.last is not None for quote in quotes),
            "breadth": market_hours.breadth(quotes),
            "top_gainers": market_hours.rank(quotes, limit=10, reverse=True),
            "top_losers": market_hours.rank(quotes, limit=10, reverse=False),
            "most_active": market_hours.most_active(quotes, limit=10),
        }
        with self._lock:
            self._snapshot = snapshot
            self._last_error = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as error:  # noqa: BLE001
                with self._lock:
                    self._last_error = str(error)
                log.warning("Market breadth update failed: %s", error)
            self._stop.wait(self.interval_seconds)

    def start(self, resolver: Callable[[], tuple[str | None, Callable[..., dict[str, Any]] | None]]) -> None:
        self._resolver = resolver
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="market-breadth", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return self._snapshot

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {"updated_at": self._snapshot["as_of"] if self._snapshot else None, "last_error": self._last_error}


breadth_worker = BreadthWorker()
