"""Angel One live market-data feed.

The platform's WebSocket fan-out (`/ws/market` and the dashboard) is fed by
this class. It supervises a background thread that, every ``ANGEL_*`` poll
interval, pulls a FULL snapshot for the subscribed instruments from SmartAPI,
normalises each row, updates the shared quote cache, records ticks and
publishes to the same subscriber queues the Kotak feed used.

Polling rather than Angel's binary WebSocket is deliberate: SmartAPI's socket
protocol needs a separate binary decoder, its feed token and a
``ANGEL_WS_URL`` deployment concern, whereas the REST quote path is the same
data with a second or two of latency — acceptable for a platform whose candles
now come from Angel's historical endpoint anyway. The subscribe/unsubscribe
and health surface are identical, so the routes and UI did not have to change.

Nothing is synthesised: a failed poll leaves the cache untouched and records
the error, exactly as the Kotak feed did.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from backend.core.config import settings
from backend.marketdata import ticks
from backend.marketdata.quotes import quote_engine

from . import quotes as angel_quotes

log = logging.getLogger("alphatrade.angel.feed")

#: How often the subscription is re-priced. SmartAPI's rate limit is the real
#: constraint, so the interval scales with the watchlist size.
MIN_POLL_SECONDS = 1.0


def _poll_interval(subscriptions: int) -> float:
    calls = max(1, (subscriptions + settings.angel_max_tokens_per_call - 1) // settings.angel_max_tokens_per_call)
    floor = calls / max(1, settings.angel_rate_limit_per_second)
    return max(MIN_POLL_SECONDS, floor)


class FeedState:
    """Observable connection state for the dashboard, Kotak-compatible shape."""

    def __init__(self) -> None:
        self.status = "disconnected"
        self.account_id: str | None = None
        self.connected_at: float | None = None
        self.last_message_at: float | None = None
        self.frames_received = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.subscribed: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "broker": "angel",
            "status": self.status,
            "account_id": self.account_id,
            "connected_at": self.connected_at,
            "last_message_at": self.last_message_at,
            "seconds_since_message": round(time.time() - self.last_message_at, 1) if self.last_message_at else None,
            "frames_received": self.frames_received,
            "reconnects": self.reconnects,
            "subscribed_instruments": self.subscribed,
            "last_error": self.last_error,
        }


class AngelFeed:
    """Polls SmartAPI for the watchlist and publishes normalised quotes."""

    def __init__(self) -> None:
        self.state = FeedState()
        self._lock = threading.RLock()
        self._subscriptions: dict[str, dict[str, str]] = {}  # token -> {exchange_segment}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._call: Callable[..., dict[str, Any]] | None = None
        self._resolver: Callable[[], tuple[str | None, Callable[..., dict[str, Any]] | None]] | None = None

    # -- subscription bookkeeping ---------------------------------------
    def subscribe(self, instruments: list[dict[str, str]]) -> list[dict[str, str]]:
        added: list[dict[str, str]] = []
        with self._lock:
            for instrument in instruments:
                token = str(instrument.get("instrument_token") or instrument.get("token") or "").strip()
                if not token or token in self._subscriptions:
                    continue
                segment = str(instrument.get("exchange_segment") or "nse_cm").strip().lower()
                self._subscriptions[token] = {"exchange_segment": segment}
                added.append({"instrument_token": token, "exchange_segment": segment})
        self.state.subscribed = len(self._subscriptions)
        return added

    def unsubscribe(self, tokens: list[str]) -> list[str]:
        removed: list[dict[str, str]] = []
        with self._lock:
            for token in tokens:
                entry = self._subscriptions.pop(str(token), None)
                if entry:
                    removed.append({"instrument_token": str(token), "exchange_segment": entry["exchange_segment"]})
        self.state.subscribed = len(self._subscriptions)
        return removed

    def subscription_tokens(self) -> list[str]:
        with self._lock:
            return list(self._subscriptions)

    def subscription_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(token, entry["exchange_segment"]) for token, entry in self._subscriptions.items()]

    # -- fan-out (same contract as the Kotak feed) ----------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self, channel: asyncio.Queue) -> Callable[[], None]:
        with self._lock:
            self._subscribers.add(channel)

        def unregister() -> None:
            with self._lock:
                self._subscribers.discard(channel)

        return unregister

    def _publish(self, payload: dict[str, Any]) -> None:
        loop, channels = self._loop, list(self._subscribers)
        if loop is None or not channels:
            return
        for channel in channels:
            try:
                loop.call_soon_threadsafe(self._offer, channel, payload)
            except RuntimeError:
                pass  # loop closing

    @staticmethod
    def _offer(channel: asyncio.Queue, payload: dict[str, Any]) -> None:
        try:
            channel.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # a slow client drops ticks rather than stalling the feed

    # -- polling ---------------------------------------------------------
    def _resolve_call(self) -> Callable[..., dict[str, Any]] | None:
        """The account + SmartAPI bridge to poll with, resolved lazily."""
        if self._resolver is None:
            return None
        try:
            return self._resolver()[1]
        except Exception as error:  # noqa: BLE001
            log.debug("Angel feed could not resolve credentials: %s", error)
            return None

    def poll_once(self) -> int:
        """One snapshot of the whole watchlist. Returns rows published."""
        keys = self.subscription_keys()
        if not keys:
            return 0
        call = self._call or self._resolve_call()
        if call is None:
            raise RuntimeError("No Angel One credentials available for the feed")
        published = 0
        for batch_start in range(0, len(keys), settings.angel_max_tokens_per_call):
            batch = keys[batch_start:batch_start + settings.angel_max_tokens_per_call]
            quotes = angel_quotes.fetch(call, batch, quote_type="all")
            for quote in quotes:
                try:
                    quote_engine.store(quote)
                    ticks.record_quote(quote)
                    self._publish(quote.to_dict())
                    published += 1
                except Exception as error:
                    import traceback
                    log.error("Failed to store/record angel quote: %s", error)
                    traceback.print_exc()
        ticks.maybe_flush()
        return published

    # -- lifecycle -------------------------------------------------------
    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                call = self._resolve_call()
                if call is None:
                    self.state.status = "waiting_credentials"
                    self._stop.wait(10)
                    continue
                self._call = call
                self.state.status = "connected"
                self.state.connected_at = self.state.connected_at or time.time()
                self.state.last_error = None

                while not self._stop.is_set():
                    try:
                        count = self.poll_once()
                        if count:
                            self.state.frames_received += count
                            self.state.last_message_at = time.time()
                        ticks.maybe_flush()
                        backoff = 2.0
                    except Exception as error:  # noqa: BLE001 - one bad poll must not kill the feed
                        self.state.last_error = str(error)
                        log.warning("Angel feed poll failed: %s", error)
                    self._stop.wait(_poll_interval(len(self.subscription_keys())))
            except Exception as error:  # noqa: BLE001
                self.state.last_error = str(error)
                log.exception("Angel feed loop error")
            if self._stop.is_set():
                break
            self.state.reconnects += 1
            self.state.status = "reconnecting"
            self._call = None
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30)
        self.state.status = "stopped"

    def start(self, resolver: Callable[[], tuple[str | None, Callable[..., dict[str, Any]] | None]] | None = None) -> None:
        if resolver is not None:
            self._resolver = resolver
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="angel-feed", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        ticks.flush()

    def health(self) -> dict[str, Any]:
        return {**self.state.snapshot(), **angel_quotes.health(), **quote_engine.health()}


angel_feed = AngelFeed()
