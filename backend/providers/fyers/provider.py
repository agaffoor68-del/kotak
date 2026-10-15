"""FYERS API v3 market-data provider — the platform's default, read-only source.

Implements :class:`backend.providers.base.MarketDataProvider` on top of the
official ``fyers-apiv3`` SDK:

* ``get_quotes`` / ``get_ltp`` → ``GET /data/quotes`` (≤50 symbols per call,
  chunked and rate-limited; FYERS exposes no lighter LTP endpoint in v3, so LTP
  requests the same snapshot and callers simply read fewer fields).
* ``historical_candles`` → ``GET /data/history`` with per-resolution range
  chunking (100 days for minute bars, 366 for daily/weekly/monthly) and
  persistence into the shared candle store, so a restart keeps history.
* ``websocket_feed`` → a supervised FYERS Data Socket (see :mod:`.feed`).

Nothing here places, modifies or cancels orders — there is no order code in
this package by design. Prices are either FYERS' or absent: an unreachable API
raises a :class:`ProviderError` whose message the UI shows verbatim.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta
from typing import Any, Iterable

from backend.core.config import settings
from backend.marketdata import ticks
from backend.marketdata.quotes import Quote, quote_engine
from backend.providers.base import (
    InstrumentKey,
    MarketDataProvider,
    ProviderAuthError,
    ProviderError,
)

from .auth import fyers_auth
from .client import FyersClient
from .symbols import symbol_map

log = logging.getLogger("alphatrade.providers.fyers")

#: Platform interval → FYERS ``resolution`` value.
RESOLUTION_MAP: dict[str, str] = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15",
    "30m": "30", "60m": "60", "1d": "D",
}
#: Longest span FYERS accepts per history request, by resolution class.
CHUNK_DAYS: dict[str, int] = {"D": 366, "1W": 366, "1M": 366}
DEFAULT_CHUNK_DAYS = 100
#: Seconds a fetched candle window stays hot before the broker is asked again.
CANDLE_CACHE_TTL = 20.0

#: FYERS field name(s) → canonical quote attribute, for both REST (``v``) and
#: the Data Socket (SymbolUpdate) payload shapes.
FIELD_MAP: dict[str, tuple[str, ...]] = {
    "last": ("lp", "ltp"),
    "change": ("ch",),
    "change_percent": ("chp",),
    "open": ("open_price",),
    "high": ("high_price",),
    "low": ("low_price",),
    "previous_close": ("prev_close_price",),
    "volume": ("volume", "vol_traded_today"),
    "bid": ("bid", "bid_price"),
    "ask": ("ask", "ask_price"),
    "bid_quantity": ("bid_size",),
    "ask_quantity": ("ask_size",),
    "average_price": ("atp", "avg_trade_price"),
    "open_interest": ("oi", "OI"),
    "total_buy_quantity": ("tot_buy_qty",),
    "total_sell_quantity": ("tot_sell_qty",),
    "last_traded_quantity": ("last_traded_qty",),
    "upper_circuit": ("upper_ckt",),
    "lower_circuit": ("lower_ckt",),
}


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None  # NaN guard
    text = str(value).replace(",", "").strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def build_quote(key: InstrumentKey, values: dict[str, Any]) -> Quote:
    """Map FYERS quote/tick fields onto the platform's :class:`Quote`."""
    token, segment = key
    quote = Quote(token=str(token), exchange_segment=segment)
    for attribute, candidates in FIELD_MAP.items():
        for name in candidates:
            if name in values:
                number = _number(values[name])
                if number is not None:
                    setattr(quote, attribute, number)
                break
    if quote.change is None and quote.last is not None and quote.previous_close:
        quote.change = round(quote.last - quote.previous_close, 4)
    if quote.change_percent is None and quote.change is not None and quote.previous_close:
        quote.change_percent = round((quote.change / quote.previous_close) * 100, 4)
    if quote.bid is not None and quote.ask is None:
        quote.ask = quote.bid
    if quote.ask is not None and quote.bid is None:
        quote.bid = quote.ask
    quote.updated_at = time.time()
    return quote


def history_windows(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Split a date range into FYERS-sized ``(from, to)`` windows."""
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=max(1, chunk_days) - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start:start + size]


class FyersProvider(MarketDataProvider):
    """Read-only market data from FYERS API v3."""

    name = "fyers"
    display_name = "FYERS API v3 (read-only)"

    def __init__(self) -> None:
        self.auth = fyers_auth
        self.client = FyersClient(self.auth.get_token)
        self._cache_lock = threading.Lock()
        self._candle_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}
        self._feed: Any = None
        self._feed_lock = threading.Lock()

    # -- auth surface ------------------------------------------------------

    def login_url(self) -> str | None:
        return "/fyers/login" if self.auth.configured else None

    def auth_status(self) -> dict[str, Any]:
        return {
            **self.auth.status(),
            "display_name": self.display_name,
            "symbols": symbol_map.stats(),
            "client": self.client.health(),
        }

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._candle_cache.clear()
        symbol_map.clear()

    # -- quotes ------------------------------------------------------------

    def _fetch_quotes(self, keys: list[InstrumentKey], what: str) -> list[Quote]:
        resolved, unmapped = symbol_map.resolve(list(keys))
        if not resolved:
            from .symbols import require_mapped

            require_mapped(resolved, unmapped)

        batch_size = max(1, int(settings.fyers_max_symbols_per_call))
        quotes: list[Quote] = []
        missing: list[str] = []
        for batch in _chunks(resolved, batch_size):
            symbols = [symbol for _key, symbol in batch]
            payload = self.client.quotes(symbols)
            rows = payload.get("d")
            rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
            if not rows:
                raise ProviderError(
                    f"FYERS {what} returned no rows for {len(symbols)} symbol(s) "
                    f"({', '.join(symbols[:3])}{'…' if len(symbols) > 3 else ''})."
                )
            for row in rows:
                symbol = str(row.get("n") or row.get("symbol") or "")
                values = row.get("v") if isinstance(row.get("v"), dict) else row
                if not isinstance(values, dict):
                    continue
                key = symbol_map.from_fyers(symbol)
                if key is None:
                    missing.append(symbol or "?")
                    continue
                quotes.append(build_quote(key, values))
            if len(rows) < len(batch):
                returned = {
                    str(row.get("n") or row.get("symbol") or "")
                    for row in rows
                }
                missing.extend(s for s in symbols if s not in returned)
        if missing:
            log.warning(
                "FYERS did not return %d requested symbol(s): %s",
                len(missing), ", ".join(missing[:5]),
            )
        if not quotes:
            raise ProviderError(
                f"FYERS {what} produced no usable quotes for {len(keys)} requested "
                f"instrument(s); missing: {', '.join(missing[:5]) or 'none reported'}."
            )
        return quotes

    def get_quotes(
        self, keys: Iterable[InstrumentKey], quote_type: str = "all",
    ) -> list[Quote]:
        """Full snapshot (last/OHLC/change/volume/bid-ask) for the given keys.

        ``quote_type`` is accepted for interface compatibility; FYERS' v3
        ``/data/quotes`` always returns the full snapshot.
        """
        requested = [(str(token), segment) for token, segment in keys]
        if not requested:
            return []
        try:
            return self._fetch_quotes(requested, what=f"{quote_type} quotes")
        except ProviderAuthError:
            raise
        except ProviderError:
            raise
        except Exception as error:  # noqa: BLE001 - SDK quirks become provider errors
            log.warning("FYERS quotes failed: %s", error)
            raise ProviderError(f"FYERS quotes failed: {type(error).__name__}: {error}") from error

    def get_ltp(self, keys: Iterable[InstrumentKey]) -> list[Quote]:
        """Last-traded-price snapshot (breadth screens); same endpoint, cheaper read."""
        requested = [(str(token), segment) for token, segment in keys]
        if not requested:
            return []
        try:
            return self._fetch_quotes(requested, what="LTP quotes")
        except ProviderAuthError:
            raise
        except ProviderError:
            raise
        except Exception as error:  # noqa: BLE001
            log.warning("FYERS LTP failed: %s", error)
            raise ProviderError(f"FYERS LTP failed: {type(error).__name__}: {error}") from error

    # -- history -----------------------------------------------------------

    def historical_candles(
        self,
        token: str,
        exchange_segment: str,
        interval: str,
        limit: int = 500,
        days: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """``(candles, from_cache)`` — real FYERS OHLCV, persisted locally."""
        if interval not in RESOLUTION_MAP:
            raise ProviderError(
                f"Unsupported interval '{interval}'. "
                f"Available: {', '.join(RESOLUTION_MAP)}"
            )
        symbol = symbol_map.to_fyers(str(token), exchange_segment)
        if not symbol:
            from backend.providers.base import ProviderSymbolError

            raise ProviderSymbolError(
                f"Instrument {token}:{exchange_segment} has no FYERS symbol yet. "
                "Sync the instrument master, then retry."
            )

        key = (str(token), exchange_segment, interval)
        now = time.time()
        with self._cache_lock:
            cached = self._candle_cache.get(key)
            if cached and now - cached[0] < CANDLE_CACHE_TTL:
                return cached[1][-limit:], True

        total_days = int(days or settings.fyers_history_days_default)
        total_days = max(1, min(total_days, int(settings.fyers_history_days_max)))
        resolution = RESOLUTION_MAP[interval]
        chunk_days = CHUNK_DAYS.get(resolution, DEFAULT_CHUNK_DAYS)
        end = date.today()
        start = end - timedelta(days=total_days - 1)

        by_time: dict[int, dict[str, Any]] = {}
        for window_from, window_to in history_windows(start, end, chunk_days):
            payload = {
                "symbol": symbol,
                "resolution": resolution,
                "date_format": "1",
                "range_from": window_from.isoformat(),
                "range_to": window_to.isoformat(),
            }
            data = self.client.history(payload)
            raw = data.get("candles")
            if not isinstance(raw, list):
                continue
            for row in raw:
                if not isinstance(row, (list, tuple)) or len(row) < 6:
                    continue
                stamp = _number(row[0])
                open_, high, low, close = _number(row[1]), _number(row[2]), _number(row[3]), _number(row[4])
                if stamp is None or None in (open_, high, low, close):
                    continue
                by_time[int(stamp)] = {
                    "time": int(stamp),
                    "open": open_, "high": high, "low": low, "close": close,
                    "volume": _number(row[5]) or 0.0,
                }

        candles = [by_time[stamp] for stamp in sorted(by_time)]
        if not candles:
            raise ProviderError(
                f"FYERS history returned no candles for {symbol} over the last "
                f"{total_days} day(s) at {interval} resolution."
            )

        self._persist(str(token), exchange_segment, interval, candles)
        with self._cache_lock:
            self._candle_cache[key] = (time.time(), candles)
        return candles[-limit:], False

    @staticmethod
    def _persist(token: str, exchange_segment: str, interval: str, candles: list[dict[str, Any]]) -> None:
        seconds = ticks.INTERVAL_SECONDS.get(interval)
        if not seconds:
            return
        rows = [
            (
                token, exchange_segment, interval, int(candle["time"] // seconds),
                candle["open"], candle["high"], candle["low"], candle["close"],
                candle.get("volume") or 0, time.time(),
            )
            for candle in candles
        ]
        ticks.upsert_candles(token, exchange_segment, interval, rows)

    # -- live feed ---------------------------------------------------------

    def websocket_feed(self) -> Any:
        """The shared FYERS Data Socket feed (created once per process)."""
        if self._feed is None:
            with self._feed_lock:
                if self._feed is None:
                    from .feed import FyersFeed

                    self._feed = FyersFeed(provider=self)
        return self._feed


#: The provider instance the registry hands out.
fyers_provider = FyersProvider()


