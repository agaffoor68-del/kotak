"""Historical candles from Angel One SmartAPI.

This is the module that justifies the whole architecture: Kotak Neo publishes
**no** historical candle endpoint, so charts, indicators, backtests and the
algo engine all read candles from here.

SmartAPI's ``/rest/secure/angelbroking/historical/v1/getCandleData`` takes

    {exchange, symboltoken, interval, fromdate, todate}

where the dates are ``"YYYY-MM-DD HH:MM"`` in **IST** (Asia/Kolkata), not UTC —
getting that wrong silently shifts every candle by 5h30m, so the conversion is
done explicitly here and never left to the caller. The interval vocabulary is
``ONE_MINUTE``/``THREE_MINUTE``/…/``ONE_DAY`` rather than the platform's
``1m``/``1d``, and the frontend never sees Angel's names.

A single call is capped (roughly a year for minute bars), so a long range is
chunked and the pieces are concatenated in order. Results are cached briefly and
also written to the shared ``candles`` table, so a restart does not lose history
and a repeated request costs nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.core.config import settings
from backend.core.database import market_cursor
from backend.marketdata import ticks

log = logging.getLogger("alphatrade.angel.candles")

HISTORICAL_PATH = "/rest/secure/angelbroking/historical/v1/getCandleData"

IST = timezone(timedelta(hours=5, minutes=30))

#: The platform vocabulary -> SmartAPI's interval names.
INTERVAL_MAP: dict[str, str] = {
    "1m": "ONE_MINUTE",
    "3m": "THREE_MINUTE",
    "5m": "FIVE_MINUTE",
    "10m": "TEN_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "60m": "ONE_HOUR",
    "1d": "ONE_DAY",
}

#: Longest span SmartAPI accepts per (interval, call). Kept conservative.
CHUNK_DAYS: dict[str, int] = {
    "1m": 30, "3m": 60, "5m": 100, "10m": 100, "15m": 200,
    "30m": 200, "60m": 400, "1d": 1800,
}

_CACHE_TTL = 20.0
_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


class CandleError(RuntimeError):
    """SmartAPI could not supply candles for the request."""


def supported_intervals() -> list[str]:
    return list(INTERVAL_MAP)


def _to_ist(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(IST)


def _format(moment: datetime) -> str:
    return _to_ist(moment).strftime("%Y-%m-%d %H:%M")


def _parse_stamp(raw: Any) -> float | None:
    """SmartAPI returns ``"2024-01-04T09:15:00+05:30"``; return epoch seconds."""
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.timestamp()
    return None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalise_rows(rows: Any) -> list[dict[str, Any]]:
    """Turn SmartAPI's array-of-arrays into candle dicts, newest last."""
    candles: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return candles
    for row in rows:
        if isinstance(row, dict):
            stamp = _parse_stamp(row.get("time") or row.get("timestamp"))
            values = (row.get("open"), row.get("high"), row.get("low"), row.get("close"), row.get("volume"))
        elif isinstance(row, (list, tuple)) and len(row) >= 6:
            stamp = _parse_stamp(row[0])
            values = (row[1], row[2], row[3], row[4], row[5])
        else:
            continue
        open_, high, low, close, volume = (_number(v) for v in values)
        if stamp is None or None in (open_, high, low, close):
            continue
        candles.append({
            "time": stamp,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume or 0,
            "trades": 0,
        })
    candles.sort(key=lambda candle: candle["time"])
    return candles


def _call_historical(
    call: Callable[..., dict[str, Any]],
    *,
    token: str,
    segment: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    payload = call(
        "historical",
        HISTORICAL_PATH,
        params={
            "exchange": segment,
            "symboltoken": str(token),
            "interval": interval,
            "fromdate": _format(start),
            "todate": _format(end),
        },
        http_method="POST",
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None:
        raise CandleError(f"SmartAPI returned no candle data for {symbol}")
    return normalise_rows(data)


def fetch(
    call: Callable[..., dict[str, Any]],
    token: str,
    exchange_segment: str,
    interval: str,
    *,
    days: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch candles for one instrument, chunking long ranges.

    `call` is the SmartAPI session bridge (see ``AngelSessionManager.call``).
    """
    if interval not in INTERVAL_MAP:
        raise CandleError(f"Unsupported interval '{interval}'. Available: {', '.join(INTERVAL_MAP)}")
    angel_interval = INTERVAL_MAP[interval]

    end = end or datetime.now(tz=IST)
    if start is None:
        span = days if days is not None else settings.angel_history_days_default
        span = max(1, min(int(span), settings.angel_history_days_max))
        start = end - timedelta(days=span)

    symbol = _symbol_for(token, exchange_segment) or str(token)
    chunk = CHUNK_DAYS.get(interval, 30)

    merged: dict[float, dict[str, Any]] = {}
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=chunk), end)
        try:
            rows = _call_historical(
                call, token=str(token), segment=exchange_segment, symbol=symbol,
                interval=angel_interval, start=cursor, end=stop,
            )
        except CandleError:
            raise
        except Exception as error:  # noqa: BLE001 - transport/SDK failure
            raise CandleError(f"SmartAPI historical call failed for {symbol}: {error}") from error
        for candle in rows:
            merged[candle["time"]] = candle
        cursor = stop
        time.sleep(0.2)  # stay inside SmartAPI's rate limit between chunks

    candles = [merged[key] for key in sorted(merged)]
    return candles


def _symbol_for(token: str, exchange_segment: str) -> str | None:
    with market_cursor() as cursor:
        cursor.execute(
            "SELECT trading_symbol FROM instruments WHERE token = ? AND exchange_segment = ?",
            (str(token), exchange_segment),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    from backend.angel.instruments import SEGMENT_EXCHANGE

    return f"{SEGMENT_EXCHANGE.get(exchange_segment, 'NSE')}:{row['trading_symbol']}"


def history(
    call: Callable[..., dict[str, Any]],
    token: str,
    exchange_segment: str,
    interval: str,
    limit: int = 500,
    *,
    days: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Candles for the UI, cached for a few seconds, newest last.

    Returns ``(candles, from_cache)``. The candles are persisted so a restart
    keeps whatever history has already been pulled.
    """
    key = (str(token), exchange_segment, interval)
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1][-limit:], True

    candles = fetch(call, token, exchange_segment, interval, days=days)
    store(token, exchange_segment, interval, candles)
    with _cache_lock:
        _cache[key] = (now, candles)
    return candles[-limit:], False


def incremental(
    call: Callable[..., dict[str, Any]],
    token: str,
    exchange_segment: str,
    interval: str,
    limit: int = 1000,
    *,
    backfill_days: int | None = None,
) -> list[dict[str, Any]]:
    """Return `limit` candles, backfilling from SmartAPI only when short.

    The algo engine and charts call this. If the local store already holds
    enough bars it is used as-is (no network), otherwise the gap is filled.
    """
    local = ticks.history(token, exchange_segment, interval, limit)
    if len(local) >= limit:
        return local
    try:
        candles = fetch(call, token, exchange_segment, interval, days=backfill_days)
    except CandleError as error:
        log.warning("Backfill failed for %s %s: %s", token, interval, error)
        return local
    store(token, exchange_segment, interval, candles)
    return ticks.history(token, exchange_segment, interval, limit)


def store(token: str, exchange_segment: str, interval: str, candles: list[dict[str, Any]]) -> int:
    """Persist SmartAPI candles into the shared candle store."""
    if interval not in ticks.INTERVAL_SECONDS or not candles:
        return 0
    seconds = ticks.INTERVAL_SECONDS[interval]
    rows = [
        (
            str(token), exchange_segment, interval, int(candle["time"] // seconds),
            candle["open"], candle["high"], candle["low"], candle["close"],
            candle.get("volume") or 0, time.time(),
        )
        for candle in candles
    ]
    return ticks.upsert_candles(token, exchange_segment, interval, rows)


def live_candle(
    call: Callable[..., dict[str, Any]],
    token: str,
    exchange_segment: str,
    interval: str,
) -> dict[str, Any] | None:
    """The current, still-forming candle — the last row of a small fetch."""
    try:
        candles = fetch(call, token, exchange_segment, interval, days=2)
    except CandleError:
        return None
    return candles[-1] if candles else None


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
