"""Background market-breadth snapshots sourced from Angel One quotes."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from backend.angel import instruments
from backend.angel.quotes import fetch
from backend.core.config import settings
from backend.marketdata import market_hours, master

log = logging.getLogger("alphatrade.marketdata.breadth")


class BreadthWorker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] = {"status": "waiting", "breadth": market_hours.breadth([])}
        self._resolver: Callable[[], tuple[str | None, Callable[..., dict[str, Any]] | None]] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, resolver=None) -> None:
        self._resolver = resolver or self._resolver
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="market-breadth", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def refresh(self) -> None:
        if not self._resolver:
            return
        _account_id, call = self._resolver()
        if call is None:
            raise RuntimeError("No Angel One credentials available for breadth")
        universe = instruments.equity_universe("nse_cm", settings.breadth_universe_size)
        if not universe:
            universe = master.universe("nse_cm", settings.breadth_universe_size)
        if not universe:
            raise RuntimeError("No NSE cash instruments available for breadth")
        quotes = fetch(call, [(item["token"], item["exchange_segment"]) for item in universe], "ltp")
        with self._lock:
            self._snapshot = {
                "status": "ready",
                "as_of": time.time(),
                "source": "angel_one_ltp_universe",
                "universe_size": len(universe),
                "priced": sum(1 for quote in quotes if quote.last is not None),
                "breadth": market_hours.breadth(quotes),
                "top_gainers": market_hours.rank(quotes, limit=10, reverse=True),
                "top_losers": market_hours.rank(quotes, limit=10, reverse=False),
                "most_active": market_hours.most_active(quotes, limit=10),
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as error:  # one failed cycle must not kill the worker
                log.warning("Market breadth refresh failed: %s", error)
                with self._lock:
                    self._snapshot["status"] = "error"
                    self._snapshot["error"] = str(error)
            self._stop.wait(max(15, settings.breadth_refresh_seconds))


breadth_worker = BreadthWorker()
