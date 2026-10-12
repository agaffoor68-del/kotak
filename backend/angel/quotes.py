"""Live quotes from Angel One SmartAPI.

SmartAPI exposes three quote surfaces, all of which we use:

* ``/rest/secure/angelbroking/order/v1/getLTP``            — last traded price only
* ``/rest/secure/angelbroking/market/v1/quote``            — LTP + OHLC + volume
* ``/rest/secure/angelbroking/market/v1/quote`` (FULL)     — adds depth, OI and IV

Every response row is normalised into the platform's :class:`Quote` so the rest
of the code (and the UI) never learns Angel's key names. Values are real or
absent; the module never substitutes a price it did not receive.

SmartAPI caps the number of symbols per call, so batches are chunked; and it
rate-limits, so requests are spaced with a token bucket.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable

from backend.core.config import settings
from backend.marketdata.quotes import Quote, QuoteError

from . import instruments

log = logging.getLogger("alphatrade.angel.quotes")

LTP_PATH = "/rest/secure/angelbroking/order/v1/getLTP"
QUOTE_PATH = "/rest/secure/angelbroking/market/v1/quote"

#: The platform's quote_type -> SmartAPI's mode.
MODE_MAP = {"ltp": "LTP", "ohlc": "OHLC", "all": "FULL", "full": "FULL"}

#: SmartAPI field aliases. Angel is mostly snake_case already, but legacy
#: builds returned camelCase and the depth block nests its own keys.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "last": ("ltp", "last_price", "lastPrice", "last_traded_price"),
    "open": ("open", "opening_price"),
    "high": ("high", "day_high"),
    "low": ("low", "day_low"),
    "previous_close": ("close", "previous_close", "prev_close"),
    "volume": ("volume", "trade_volume", "total_traded_volume"),
    "open_interest": ("opnInterest", "open_interest", "oi"),
    "average_price": ("avgPrice", "average_price"),
    "total_buy_quantity": ("totBuyQuan", "total_buy_quantity"),
    "total_sell_quantity": ("totSellQuan", "total_sell_quantity"),
    "last_traded_quantity": ("last_traded_quantity", "ltq"),
    "upper_circuit": ("upper_circuit", "upper_circuit_limit"),
    "lower_circuit": ("lower_circuit", "lower_circuit_limit"),
    "week52_high": ("52_week_high", "week_52_high"),
    "week52_low": ("52_week_low", "week_52_low"),
    "implied_volatility": ("implied_volatility", "iv"),
    "change_percent": ("percentChange", "pChange", "change_percent"),
}


class _RateLimiter:
    """A minimal token bucket so we never spam SmartAPI past its limit."""

    def __init__(self, per_second: int) -> None:
        self._interval = 1.0 / max(1, per_second)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self._interval


_limiter = _RateLimiter(settings.angel_rate_limit_per_second)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    text = str(value).replace(",", "").strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _depth(row: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    """Best bid/ask and their sizes out of Angel's nested depth block."""
    depth = row.get("depth") or {}
    buy = depth.get("buy") if isinstance(depth, dict) else None
    sell = depth.get("sell") if isinstance(depth, dict) else None
    bid = ask = bid_qty = ask_qty = None
    if isinstance(buy, list) and buy and isinstance(buy[0], dict):
        bid = _number(buy[0].get("price"))
        bid_qty = _number(buy[0].get("quantity"))
    if isinstance(sell, list) and sell and isinstance(sell[0], dict):
        ask = _number(sell[0].get("price"))
        ask_qty = _number(sell[0].get("quantity"))
    return bid, ask, bid_qty, ask_qty


def quote_from_row(row: dict[str, Any], token: str, exchange_segment: str) -> Quote:
    """Map one SmartAPI quote row onto the shared `Quote`."""
    quote = Quote(token=str(token), exchange_segment=exchange_segment)
    for attribute, keys in FIELD_ALIASES.items():
        for key in keys:
            if key in row and row[key] is not None:
                value = _number(row[key])
                if value is not None:
                    setattr(quote, attribute, value)
                    break

    bid, ask, bid_qty, ask_qty = _depth(row)
    quote.bid = bid if bid is not None else quote.bid
    quote.ask = ask if ask is not None else quote.ask
    quote.bid_quantity = bid_qty if bid_qty is not None else quote.bid_quantity
    quote.ask_quantity = ask_qty if ask_qty is not None else quote.ask_quantity

    if quote.last is not None and quote.previous_close:
        quote.change = round(quote.last - quote.previous_close, 4)
        quote.change_percent = round((quote.change / quote.previous_close) * 100, 4)
    if quote.bid is not None and quote.ask is None:
        quote.ask = quote.bid
    if quote.ask is not None and quote.bid is None:
        quote.bid = quote.ask
    quote.updated_at = time.time()
    return quote


def _extract_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        fetched = data.get("fetched")
        if isinstance(fetched, list):
            return [row for row in fetched if isinstance(row, dict)]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def fetch(
    call: Callable[..., dict[str, Any]],
    keys: list[tuple[str, str]],
    quote_type: str = "all",
) -> list[Quote]:
    """Fetch a live snapshot for `(token, exchange_segment)` pairs."""
    if not keys:
        return []
    mode = MODE_MAP.get(str(quote_type).lower(), "FULL")
    resolved = instruments.to_smartapi(keys)
    if not resolved:
        raise QuoteError("None of the requested instruments are in the Angel One master. Sync it first.")

    quotes: list[Quote] = []
    for batch in _chunks(resolved, max(1, settings.angel_max_tokens_per_call)):
        _limiter.wait()
        payload = call(
            "quotes", QUOTE_PATH,
            payload={"mode": mode, "exchangeTokens": _exchange_tokens(batch)},
            http_method="POST",
        )
        rows = _extract_rows(payload)
        if not rows:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise QuoteError(f"SmartAPI returned no quote rows: {message or payload}")
        for row in rows:
            token = row.get("symbolToken") or row.get("symbol_token")
            symbol = str(row.get("tradingSymbol") or row.get("symbol") or "")
            # The response is keyed by symbol, not token; resolve back.
            match = _match(batch, token, symbol)
            if match is None:
                continue
            quote = quote_from_row(row, match[0], match[2])
            quotes.append(quote)
    return quotes


def _exchange_tokens(batch: list[tuple[str, str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for token, _symbol, segment in batch:
        grouped.setdefault(segment, []).append(token)
    return grouped


def _match(batch: list[tuple[str, str, str]], token: Any, symbol: str) -> tuple[str, str, str] | None:
    for entry in batch:
        if token is not None and str(entry[0]) == str(token):
            return entry
    cleaned = symbol.split(":", 1)[1] if ":" in symbol else symbol
    for entry in batch:
        if entry[1].split(":", 1)[1] == cleaned:
            return entry
    return None


def fetch_ltp(
    call: Callable[..., dict[str, Any]],
    keys: list[tuple[str, str]],
) -> list[Quote]:
    """Cheaper LTP-only snapshot, used by wide screens like breadth."""
    return fetch(call, keys, quote_type="ltp")


def health() -> dict[str, Any]:
    """Small, secret-free status block for the health endpoint."""
    return {
        "mode": "SmartAPI quote (LTP/OHLC/FULL)",
        "rate_limit_per_second": settings.angel_rate_limit_per_second,
        "max_tokens_per_call": settings.angel_max_tokens_per_call,
    }
