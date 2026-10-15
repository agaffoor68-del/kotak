"""Broker-agnostic market data provider interface.

Every market-data surface of the platform (quotes, LTP snapshots, historical
candles and the live tick fan-out) talks to this interface, never to a broker
SDK directly. Swapping FYERS for Angel One — or any future broker — means
writing one module that implements :class:`MarketDataProvider` and selecting it
with ``MARKET_DATA_PROVIDER``; routes, WebSocket plumbing and the UI stay
untouched.

Two rules govern every implementation:

* **Read-only.** A provider may only *read* market data. Order placement,
  modification and cancellation have no place in this package and must never be
  added to it; the broker credentials used here should carry market-data
  permission only.
* **Never invent a price.** A failed call raises a :class:`ProviderError` (or
  one of its subclasses) carrying a message the UI can show verbatim. Cached
  values may be served with their age, but a provider never fabricates a tick.

The instrument keys used across the interface are the platform's own
``(token, exchange_segment)`` pairs — the same ones the ``instruments`` table,
the frontend and the tick store already use — so a provider owns the translation
to its own symbol vocabulary internally.
"""

from __future__ import annotations

import abc
from typing import Any, Callable, Iterable, Protocol

from backend.marketdata.quotes import Quote

#: One instrument in the platform's own vocabulary: ``(token, exchange_segment)``.
InstrumentKey = tuple[str, str]

#: A subscriber queue of normalised quote dicts, as handed out by the fan-out.
SubscriberQueue = Any


class ProviderError(RuntimeError):
    """The provider could not serve the request; ``str()`` is user-facing."""


class ProviderAuthError(ProviderError):
    """Authentication with the broker failed or the session expired.

    Retrying immediately is pointless: the caller (or the UI) must re-login.
    """


class ProviderSymbolError(ProviderError):
    """The requested instrument has no symbol in this provider's vocabulary."""


class FeedHandle(Protocol):
    """The live-feed surface routes and the WebSocket endpoint rely on.

    It mirrors the shape the Kotak and Angel feeds already expose, so
    ``/ws/market``, ``/api/v1/market/feed/*`` and the dashboard work unchanged.
    """

    def subscribe(self, instruments: list[dict[str, Any]]) -> list[dict[str, str]]: ...

    def unsubscribe(self, tokens: list[str]) -> list[str]: ...

    def subscription_tokens(self) -> list[str]: ...

    def bind_loop(self, loop: Any) -> None: ...

    def register(self, channel: SubscriberQueue) -> Callable[[], None]: ...

    def health(self) -> dict[str, Any]: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class MarketDataProvider(abc.ABC):
    """Read-only market data from one broker."""

    #: Registry key, e.g. ``"fyers"``. Must match ``MARKET_DATA_PROVIDER``.
    name: str = "unknown"
    #: Human label for the UI ("FYERS API v3 (read-only)").
    display_name: str = "unknown"

    # ------------------------------------------------------------------ data

    @abc.abstractmethod
    def get_quotes(
        self,
        keys: Iterable[InstrumentKey],
        quote_type: str = "all",
    ) -> list[Quote]:
        """Full snapshot (last, OHLC, change, volume, bid/ask) for many keys.

        Raises :class:`ProviderError` when the broker answered with something
        unusable; returns only real values.
        """

    @abc.abstractmethod
    def get_ltp(self, keys: Iterable[InstrumentKey]) -> list[Quote]:
        """Cheaper last-traded-price snapshot for a wide universe (breadth)."""

    @abc.abstractmethod
    def historical_candles(
        self,
        token: str,
        exchange_segment: str,
        interval: str,
        limit: int = 500,
        days: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """``(candles, from_cache)`` with candles ordered oldest → newest.

        Candle dicts use the platform shape: ``{time, open, high, low, close,
        volume}`` where ``time`` is epoch seconds (UTC-based bucket).
        """

    @abc.abstractmethod
    def websocket_feed(self) -> FeedHandle:
        """The live tick feed: one shared socket with subscribe/fan-out."""

    # ------------------------------------------------------------------ auth

    def login_url(self) -> str | None:
        """Where an operator re-authorises the session, if the broker needs it."""
        return None

    def auth_status(self) -> dict[str, Any]:
        """Secret-free session status: configured/authenticated/expires/login."""
        return {"name": self.name, "display_name": self.display_name}

    def health(self) -> dict[str, Any]:
        """Combined status block for ``/api/v1/market/status``."""
        from backend.marketdata.quotes import quote_engine

        return {
            **self.auth_status(),
            "feed": self.websocket_feed().health(),
            "quotes_cache": quote_engine.health(),
        }
