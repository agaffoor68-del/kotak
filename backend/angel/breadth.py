"""Background Advance/Decline calculator backed by Angel One quotes."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from backend.core.config import settings
from backend.marketdata import market_hours, master, search_index
from backend.marketdata.quotes import quote_engine

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
            return self.snapshot()
        if not market_hours.is_market_open() and self.snapshot():
            log.info("Market is closed; returning the last known breadth snapshot")
            return self.snapshot()
        _account_id, call = self._resolver()
        if call is None:
            return self.snapshot()
        universe = self._universe()
        if not universe:
            # Nothing to scan: keep the last known snapshot rather than
            # publishing a breadth of zeros.
            log.warning("Market breadth has no instruments to scan; keeping the last known snapshot")
            return self.snapshot()
        # LTP mode does not include previous close; OHLC is required for A/D.
        try:
            quotes = fetch(call, [(row["token"], row["exchange_segment"]) for row in universe], "ohlc")
        except Exception:
            log.exception("Market breadth broker quote request failed")
            return self.snapshot()
        if not quotes:
            log.error("Market breadth broker quote request returned no rows")
            return self.snapshot()
        for quote in quotes:
            quote_engine.store(quote)

        # The universe rows already carry the trading symbol and the company
        # name, so the movers are labelled from them instead of asking the
        # master once per quote.
        symbols: dict[tuple[str, str], tuple[str, Any]] = {}
        for row in universe:
            symbol = search_index.clean_symbol(row.get("trading_symbol")) or search_index.clean_symbol(row.get("symbol"))
            symbols[(str(row["token"]), str(row["exchange_segment"]))] = (
                symbol or str(row["token"]),
                row.get("name"),
            )

        def with_symbols(items):
            resolved = []
            for item in items:
                symbol, name = symbols.get((str(item.token), str(item.exchange_segment)), (None, None))
                if not symbol:
                    try:
                        symbol, name = master.instrument_display(item.token, item.exchange_segment)
                    except (KeyError, AttributeError, TypeError, ValueError) as error:
                        log.error(
                            "Unable to map breadth token to symbol: token=%s segment=%s error=%s",
                            item.token, item.exchange_segment, error,
                        )
                        symbol, name = None, None
                resolved.append({**item.to_dict(), "symbol": symbol or str(item.token) or "UNKNOWN", "name": name})
            return resolved

        snapshot = {
            "as_of": time.time(),
            "market_session": market_hours.session_state(),
            "source": "angel_one",
            "universe_size": len(universe),
            "priced": sum(1 for quote in quotes if quote.last is not None),
            # Always publish a complete numeric breadth object so clients never
            # have to infer whether zero means "no data" or "no movers".
            "breadth": {"advances": 0, "declines": 0, "unchanged": 0, "priced": 0, **(market_hours.breadth(quotes) or {})},
            "top_gainers": with_symbols(market_hours.rank(quotes, limit=10, reverse=True)),
            "top_losers": with_symbols(market_hours.rank(quotes, limit=10, reverse=False)),
            "most_active": with_symbols(market_hours.most_active(quotes, limit=10)),
        }
        with self._lock:
            self._snapshot = snapshot
            self.last_error = None
        return snapshot

    def _universe(self) -> list[dict[str, Any]]:
        """The instruments the breadth scan covers, with their symbols.

        An empty universe means the instrument master is empty or stale, so it
        is reloaded once before the scan gives up: without it the dashboard
        would show a breadth of zero.
        """
        rows = instruments.equity_universe("nse_cm", settings.breadth_universe_size)
        if rows:
            return rows
        log.warning(
            "The breadth universe is empty (%d instruments in the master); reloading the instrument master",
            instruments.status()["instruments"],
        )
        try:
            instruments.bootstrap_if_empty()
            rows = instruments.equity_universe("nse_cm", settings.breadth_universe_size)
            if rows:
                return rows
            instruments.sync()
        except Exception:  # noqa: BLE001 - a failed reload must not kill the scan
            log.exception("Could not reload the instrument master for the breadth scan")
            return []
        rows = instruments.equity_universe("nse_cm", settings.breadth_universe_size)
        if not rows:
            log.warning("The breadth universe is still empty after reloading the instrument master")
        return rows

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as error:  # noqa: BLE001
                self.last_error = str(error)
                # The full traceback is kept: the snapshot stays as it was, so
                # the cause has to be readable in the log.
                log.exception("Market breadth refresh failed: %s", error)
            self._stop.wait(max(15, settings.breadth_refresh_seconds))


market_breadth_worker = MarketBreadthWorker()
