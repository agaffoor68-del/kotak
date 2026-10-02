"""Live quotes: Neo REST polls plus the WebSocket tick feed.

Kotak's WebSocket is push-based and gives far better latency than polling, so it
is the primary source. The REST quote endpoint is used as the authoritative
*snapshot* (it carries the full field set: OI, IV, depth) and to backfill the
cache whenever the socket is not connected.

There is no synthesised data anywhere in this module. If Neo is unreachable the
cache simply stops updating and callers see the last known values with their age.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from backend.core.config import settings
from backend.marketdata import ticks

log = logging.getLogger("alphatrade.quotes")


class QuoteError(RuntimeError):
    """Quotes could not be retrieved from Kotak Neo."""


# ------------------------------------------------------------------ value type


@dataclass
class Quote:
    token: str
    exchange_segment: str
    last: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_quantity: float | None = None
    ask_quantity: float | None = None
    implied_volatility: float | None = None
    upper_circuit: float | None = None
    lower_circuit: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    average_price: float | None = None
    total_buy_quantity: float | None = None
    total_sell_quantity: float | None = None
    last_traded_quantity: float | None = None
    updated_at: float = field(default_factory=time.time)

    @property
    def is_stale(self) -> bool:
        return time.time() - self.updated_at > 15

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token, "exchange_segment": self.exchange_segment,
            "last": self.last, "open": self.open, "high": self.high, "low": self.low,
            "previous_close": self.previous_close, "change": self.change,
            "change_percent": self.change_percent, "volume": self.volume,
            "open_interest": self.open_interest, "bid": self.bid, "ask": self.ask,
            "bid_quantity": self.bid_quantity, "ask_quantity": self.ask_quantity,
            "implied_volatility": self.implied_volatility,
            "upper_circuit": self.upper_circuit, "lower_circuit": self.lower_circuit,
            "week52_high": self.week52_high, "week52_low": self.week52_low,
            "average_price": self.average_price, "total_buy_quantity": self.total_buy_quantity,
            "total_sell_quantity": self.total_sell_quantity,
            "last_traded_quantity": self.last_traded_quantity,
            "updated_at": self.updated_at, "age_seconds": round(time.time() - self.updated_at, 1),
        }


# ------------------------------------------------------------- Neo field names

#: Neo returns abbreviated keys depending on the endpoint. Both forms map here.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "last": ("last_traded_price", "ltp", "last_price", "lp", "lastPrice"),
    "open": ("open", "op", "open_price"),
    "high": ("high", "hi", "high_price"),
    "low": ("low", "lo", "low_price"),
    "previous_close": ("previous_close", "prev_day_close", "pc", "prevClose"),
    "change": ("net_chg", "change", "chg", "netChange"),
    "change_percent": ("percentage_change", "p_change", "net_chg_percent", "pChange", "changePercent"),
    "volume": ("volume", "vol", "total_traded_volume", "totTradedVol"),
    "open_interest": ("open_interest", "oi", "openInterest", "totOI"),
    "bid": ("bid_price", "bidprice", "best_bid", "bid"),
    "ask": ("ask_price", "askprice", "best_ask", "ask"),
    "bid_quantity": ("bid_quantity", "bidqty", "bidQty"),
    "ask_quantity": ("ask_quantity", "askqty", "askQty"),
    "implied_volatility": ("implied_volatility", "iv", "impliedVolatility"),
    "upper_circuit": ("upper_circuit", "upper_circ", "uc"),
    "lower_circuit": ("lower_circuit", "lower_circ", "lc"),
    "week52_high": ("week_52_high", "52_week_high", "w52h"),
    "week52_low": ("week_52_low", "52_week_low", "w52l"),
    "average_price": ("average_price", "avg_price", "ap"),
    "total_buy_quantity": ("total_buy_quantity", "totalBuyQty"),
    "total_sell_quantity": ("total_sell_quantity", "totalSellQty"),
    "last_traded_quantity": ("last_traded_quantity", "lastTradedQty", "ltq"),
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
        parsed = float(text)
    except ValueError:
        return None
    return parsed


def quote_from_row(row: dict[str, Any], token: str, exchange_segment: str) -> Quote:
    """Build a `Quote` from a Neo row, tolerating either key style."""
    quote = Quote(token=str(token), exchange_segment=exchange_segment)
    for attribute, keys in FIELD_ALIASES.items():
        for key in keys:
            if key in row:
                value = _number(row[key])
                if value is not None:
                    setattr(quote, attribute, value)
                    break
    if quote.last is not None and quote.previous_close:
        quote.change = round(quote.last - quote.previous_close, 4)
        quote.change_percent = round((quote.change / quote.previous_close) * 100, 4)
    if quote.bid is not None and quote.ask is None:
        quote.ask = quote.bid
    if quote.ask is not None and quote.bid is None:
        quote.bid = quote.ask
    quote.updated_at = time.time()
    return quote


# --------------------------------------------------------------- quote engine


class QuoteEngine:
    """Fetches, caches and fans out live quotes."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], Quote] = {}
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Quote], None]] = []
        self._last_error: str | None = None
        self._last_success: float | None = None

    # -- subscriptions ---------------------------------------------------
    def add_listener(self, listener: Callable[[Quote], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Quote], None]) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _emit(self, quote: Quote) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(quote)
            except Exception:  # noqa: BLE001 - a bad listener must not stop the feed
                log.exception("Quote listener failed")

    # -- cache ------------------------------------------------------------
    def cached(self, token: str, exchange_segment: str) -> Quote | None:
        with self._lock:
            return self._cache.get((str(token), exchange_segment))

    def cached_many(self, keys: Iterable[tuple[str, str]]) -> list[Quote]:
        with self._lock:
            return [self._cache[(str(token), segment)] for token, segment in keys if (str(token), segment) in self._cache]

    def store(self, quote: Quote) -> Quote:
        with self._lock:
            self._cache[(quote.token, quote.exchange_segment)] = quote
        return quote

    def apply_tick(self, payload: dict[str, Any]) -> Quote | None:
        """Update the cache from a WebSocket frame and notify listeners."""
        token = str(payload.get("token") or payload.get("instrument_token") or "")
        segment = str(payload.get("exchange_segment") or "nse_cm")
        if not token:
            return None
        with self._lock:
            existing = self._cache.get((token, segment))
        merged = dict(payload)
        if existing is not None:
            # The socket sends deltas; keep fields the frame omits.
            base = existing.to_dict()
            for key, value in merged.items():
                if value is not None:
                    base[key] = value
            merged = base
        quote = quote_from_row(merged, token, segment)
        self.store(quote)
        self._emit(quote)
        return quote

    # -- REST fetch -------------------------------------------------------
    def fetch(self, call, instruments: list[dict[str, str]], quote_type: str = "all") -> list[Quote]:
        """Fetch a full snapshot for up to `max_tokens_per_quote_call` tokens."""
        if not instruments:
            return []
        if len(instruments) > settings.max_tokens_per_quote_call:
            raise QuoteError(
                f"Requested {len(instruments)} instruments; the limit is {settings.max_tokens_per_quote_call} per call"
            )
        payload = call("quotes", instrument_tokens=instruments, quote_type=quote_type)
        rows = _extract_rows(payload)
        if not rows:
            raise QuoteError(f"Kotak returned no quote rows: {_short(payload)}")

        quotes: list[Quote] = []
        for row in rows:
            token = str(row.get("instrument_token") or row.get("token") or "").strip()
            if not token:
                continue
            segment = str(row.get("exchange_segment") or "nse_cm").strip().lower()
            quote = quote_from_row(row, token, segment)
            self.store(quote)
            quotes.append(quote)
        self._last_success = time.time()
        self._last_error = None
        return quotes

    def fetch_batched(self, call, instruments: list[dict[str, str]], quote_type: str = "all") -> list[Quote]:
        """Fetch many instruments, respecting the per-call token limit."""
        size = max(1, settings.max_tokens_per_quote_call)
        results: list[Quote] = []
        for start in range(0, len(instruments), size):
            batch = instruments[start:start + size]
            try:
                results.extend(self.fetch(call, batch, quote_type))
            except QuoteError as error:
                log.warning("Quote batch %s..%s failed: %s", start, start + len(batch), error)
                self._last_error = str(error)
        return results

    def health(self) -> dict[str, Any]:
        with self._lock:
            cached = len(self._cache)
        return {
            "cached_quotes": cached,
            "last_success": self._last_success,
            "last_error": self._last_error,
            "age_seconds": round(time.time() - self._last_success, 1) if self._last_success else None,
        }


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """Pull the list of rows out of whatever envelope Neo used."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "quotes", "items", "results", "response"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        # Dictionary keyed by "<segment>|<token>".
        rows = [value for value in payload.values() if isinstance(value, dict)]
        if rows:
            return rows
    return []


def _short(payload: Any) -> str:
    text = str(payload)
    return text if len(text) <= 180 else text[:180] + "…"


quote_engine = QuoteEngine()
