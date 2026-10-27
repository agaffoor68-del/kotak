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
chunked and the pieces are concatenated in order. Every chunk spends a token
from the process-wide :data:`backend.angel.ratelimit.angel_limiter`, so candle
backfills and the live quote feed together never exceed SmartAPI's 3 req/sec
budget. Results are cached briefly (keyed by the requested *window*, so a short
request cannot satisfy a long one) and also written to the shared ``candles``
table, so a restart does not lose history and a repeated request costs nothing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.core.config import settings
from backend.core.database import app_cursor, market_cursor
from backend.marketdata import ticks
from backend.angel.instruments import SEGMENT_EXCHANGE
from backend.angel.ratelimit import angel_limiter

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
#: Key is (token, exchange_segment, interval, start_bucket, end_bucket) so a short
#: `days=2` request can never satisfy a later `days=500` request from the cache.
_cache: dict[tuple[str, str, str, int, int], tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def _stored_window(token: str, segment: str, interval: str, start: datetime, end: datetime) -> list[dict[str, Any]] | None:
    """Return persisted candles only when the database fully covers the request."""
    interval_seconds = ticks.INTERVAL_SECONDS[interval]
    start_bucket = int(start.timestamp() // interval_seconds)
    end_bucket = int(end.timestamp() // interval_seconds)
    with market_cursor() as cursor:
        cursor.execute(
            """SELECT bucket, open, high, low, close, volume, trades
               FROM candles WHERE token = ? AND exchange_segment = ? AND interval = ?
               AND bucket BETWEEN ? AND ? ORDER BY bucket""",
            (str(token), segment, interval, start_bucket, end_bucket),
        )
        rows = cursor.fetchall()
    if not rows or int(rows[0]["bucket"]) > start_bucket or int(rows[-1]["bucket"]) < end_bucket:
        return None
    return [{"time": int(row["bucket"] * interval_seconds), "open": float(row["open"]),
             "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]),
             "volume": float(row["volume"] or 0), "trades": int(row["trades"] or 0)} for row in rows]


def _range_key(token: str, exchange_segment: str, interval: str, start: datetime, end: datetime) -> tuple[str, str, str, int, int]:
    """Bucket the requested window so a wider request never reads a narrower cache hit.

    The bucket is the candle's own interval, so a request for the same day at the
    same interval hits, while a request that starts earlier (and therefore needs
    more history) does not reuse a short window's result.
    """
    seconds = max(1, ticks.INTERVAL_SECONDS.get(interval, 300))
    return (str(token), exchange_segment, interval, int(start.timestamp() // seconds), int(end.timestamp() // seconds))


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


def format_candles(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce internal candle dicts to the shape the API/UI contract expects.

    The chart, indicators and backtester consume exactly
    ``{time, open, high, low, close, volume}`` — nothing more. Internal rows also
    carry ``trades``, and the store may hand back numpy-ish numeric types; both
    are normalised here so JSON serialisation never surprises a client.
    """
    return [
        {
            "time": int(candle["time"]),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume") or 0),
        }
        for candle in candles
    ]


def _call_historical(
    call: Callable[..., dict[str, Any]],
    *,
    token: str,
    segment: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    is_index: bool = False,
) -> list[dict[str, Any]]:
    # SmartAPI wants a JSON body (not query params) for getCandleData.
    # BSE indices (e.g. SENSEX) need exchange "BSE" and token "99919000".
    # NSE indices need exchange "NSE" and a 999-prefixed token (e.g. 99926000).
    if segment == "bse_cm" or str(token) in ("265", "99919000"):
        exchange = "BSE"
        symboltoken = "99919000"
    elif is_index:
        exchange = "NSE"
        symboltoken = str(token) if str(token).startswith("999") else f"999{token}"
    else:
        exchange = SEGMENT_EXCHANGE.get(segment, "NSE")
        symboltoken = str(token)
    payload = call(
        "historical",
        HISTORICAL_PATH,
        payload={
            "exchange": exchange,
            "symboltoken": symboltoken,
            "interval": interval,
            "fromdate": _format(start),
            "todate": _format(end),
        },
        http_method="POST",
    )
    # SmartAPI wraps the rows in `data`; on failure it may return an empty list,
    # a null, or an error envelope instead. Treat every one of those as "no data
    # for this window" rather than letting a malformed payload reach the parser.
    data = payload.get("data") if isinstance(payload, dict) else payload
    if data is None:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise CandleError(f"SmartAPI returned no candle data for {symbol}: {message or 'empty response'}")
    if isinstance(data, dict):
        # Some responses nest the array under a key; pull the first list found.
        data = next((value for value in data.values() if isinstance(value, list)), [])
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

    symbol, is_index = _symbol_for(token, exchange_segment)
    symbol = symbol or str(token)
    chunk = CHUNK_DAYS.get(interval, 30)

    merged: dict[float, dict[str, Any]] = {}
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=chunk), end)
        # One token per historical call, shared with the quote feed so the whole
        # process stays inside SmartAPI's 3 req/sec budget.
        rows: list[dict[str, Any]] | None = None
        for attempt, backoff in enumerate((0, 5, 10, 20)):
            angel_limiter.wait()
            try:
                rows = _call_historical(
                    call, token=str(token), segment=exchange_segment, symbol=symbol,
                    interval=angel_interval, start=cursor, end=stop, is_index=is_index,
                )
                break
            except CandleError as error:
                text = str(error).lower()
                rate_limited = "403" in text or "rate" in text or "ab1021" in text or "ab1010" in text
                if not rate_limited or attempt == 3:
                    raise
                log.warning("Historical request rate-limited; retrying in %ss (%s/%s)", backoff, attempt + 1, 3)
                time.sleep(backoff)
            except Exception as error:  # noqa: BLE001 - transport/SDK failure
                text = str(error).lower()
                rate_limited = "403" in text or "rate" in text or "ab1021" in text or "ab1010" in text
                if not rate_limited or attempt == 3:
                    raise CandleError(f"SmartAPI historical call failed for {symbol}: {error}") from error
                log.warning("Historical request rate-limited; retrying in %ss (%s/%s)", backoff, attempt + 1, 3)
                time.sleep(backoff)
        if rows is None:
            raise CandleError(f"SmartAPI historical call failed for {symbol}: no response")
        for candle in rows:
            merged[candle["time"]] = candle
        cursor = stop

    candles = [merged[key] for key in sorted(merged)]
    return candles


_LEGACY_INDEX_TOKENS = {
    "26000": "99926000",
    "26009": "99919000",
    "26037": "99926037",
    "26074": "99926074",
    "265": "99919000",
}

def _symbol_for(token: str, exchange_segment: str) -> tuple[str | None, bool]:
    """Return (broker_symbol, is_index) for the token.

    The broker symbol is the ``exch_seg:trading_symbol`` string SmartAPI wants
    in payloads. `is_index` tells callers whether to prefix the token with `999`.
    """
    resolved = _LEGACY_INDEX_TOKENS.get(str(token), str(token))
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT trading_symbol, is_index FROM instruments WHERE token = ? AND exchange_segment = ?",
            (resolved, exchange_segment),
        )
        row = cursor.fetchone()
    if row is None:
        return None, False
    exchange = SEGMENT_EXCHANGE.get(exchange_segment, "NSE")
    return f"{exchange}:{row['trading_symbol']}", bool(row["is_index"])


def _window(
    days: int | None, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime]:
    """Resolve the requested window, clamping the span to the configured maximum."""
    resolved_end = end or datetime.now(tz=IST)
    if start is None:
        span = days if days is not None else settings.angel_history_days_default
        span = max(1, min(int(span), settings.angel_history_days_max))
        resolved_start = resolved_end - timedelta(days=span)
    else:
        resolved_start = start
    return resolved_start, resolved_end


def history(
    call: Callable[..., dict[str, Any]],
    token: str,
    exchange_segment: str,
    interval: str,
    limit: int = 500,
    *,
    days: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Candles for the UI, cached for a few seconds, newest last.

    Returns ``(candles, from_cache)``. The cache key includes the requested
    window, so a short request (e.g. a 2-day live-candle probe) cannot satisfy a
    long one within the TTL. The candles are persisted so a restart keeps
    whatever history has already been pulled.
    """
    window_start, window_end = _window(days, start, end)
    key = _range_key(token, exchange_segment, interval, window_start, window_end)
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1][-limit:], True

    stored = _stored_window(token, exchange_segment, interval, window_start, window_end)
    if stored is not None:
        with _cache_lock:
            _cache[key] = (now, stored)
        return stored[-limit:], True

    candles = fetch(
        call, token, exchange_segment, interval,
        days=days, start=window_start, end=window_end,
    )
    store(token, exchange_segment, interval, candles)
    with _cache_lock:
        _cache[key] = (now, candles)
    return candles[-limit:], False


def backfill(
    call: Callable[..., dict[str, Any]],
    token: str,
    exchange_segment: str,
    interval: str,
    *,
    since: float | None = None,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Fill the gap between the last stored candle and now.

    Called after a WebSocket (or feed) reconnect: the live tape cannot deliver
    bars that formed while the connection was down, so the missing window is
    pulled from SmartAPI's historical endpoint once and merged into the store.
    ``since`` is an epoch second; when omitted the gap is measured from the
    newest candle already persisted. Returns the candles fetched.
    """
    end = datetime.now(tz=IST)
    if since is None:
        stored = ticks.history(token, exchange_segment, interval, limit=1)
        last_ts = stored[-1]["time"] if stored else None
        since = last_ts if last_ts is not None else (end - timedelta(days=days or settings.angel_history_days_default)).timestamp()

    start = datetime.fromtimestamp(float(since), tz=IST)
    if start >= end:
        return []

    try:
        candles = fetch(call, token, exchange_segment, interval, start=start, end=end)
    except CandleError as error:
        log.warning("Backfill failed for %s %s: %s", token, interval, error)
        return []

    # Only persist bars at or after the gap start, so the seam is continuous.
    fresh = [candle for candle in candles if candle["time"] >= start.timestamp()]
    if fresh:
        store(token, exchange_segment, interval, fresh)
        with _cache_lock:
            _cache.clear()
    return fresh


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


async def download_historical_data(
    call: Callable[..., dict[str, Any]],
    symbol_token: str,
    exchange: str,
    interval: str,
    start_date: datetime,
    end_date: datetime,
) -> list[dict[str, Any]]:
    """Download a range in bounded chunks, with DB coverage and rate-limit retries.

    The function is async so callers never busy-wait between SmartAPI requests.
    Existing complete chunks are read from SQLite and do not consume API quota.
    """
    segment = exchange.lower() if exchange.lower() in SEGMENT_EXCHANGE else {
        "NSE": "nse_cm", "NFO": "nse_fo", "BSE": "bse_cm", "BFO": "bse_fo",
    }.get(exchange.upper(), exchange.lower())
    platform_interval = next((key for key, value in INTERVAL_MAP.items() if value == interval), interval)
    if platform_interval not in INTERVAL_MAP:
        raise CandleError(f"Unsupported interval '{interval}'")
    if start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=IST)
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=IST)
    if start_date >= end_date:
        raise CandleError("start_date must be before end_date")

    token = str(symbol_token)
    symbol, is_index = _symbol_for(token, segment)
    chunk_days = CHUNK_DAYS.get(platform_interval, 30)
    merged: dict[float, dict[str, Any]] = {}
    cursor = start_date
    request_number = 0

    while cursor < end_date:
        stop = min(cursor + timedelta(days=chunk_days), end_date)
        stored = _stored_window(token, segment, platform_interval, cursor, stop)
        if stored is not None:
            merged.update({row["time"]: row for row in stored})
            cursor = stop
            continue

        if request_number:
            await asyncio.sleep(0.4)
        rows: list[dict[str, Any]] | None = None
        for attempt, backoff in enumerate((0, 5, 10, 20)):
            if backoff:
                await asyncio.sleep(backoff)
            angel_limiter.wait()
            try:
                rows = _call_historical(
                    call, token=token, segment=segment, symbol=symbol or token,
                    interval=INTERVAL_MAP[platform_interval], start=cursor, end=stop,
                    is_index=is_index,
                )
                break
            except Exception as error:  # noqa: BLE001 - broker SDK errors vary
                text = str(error).lower()
                limited = "403" in text or "429" in text or "rate" in text or "ab1010" in text or "ab1021" in text
                if not limited or attempt == 3:
                    raise CandleError(f"SmartAPI historical call failed: {error}") from error
                log.warning("Historical request rate-limited; retrying in %ss", backoff or 5)
        if rows is None:
            raise CandleError("SmartAPI returned no candle data")
        if rows:
            store(token, segment, platform_interval, rows)
            merged.update({row["time"]: row for row in rows})
        request_number += 1
        cursor = stop

    result = [merged[key] for key in sorted(merged)]
    log.info("Historical download complete: %s/%s %s rows=%s", token, segment, platform_interval, len(result))
    return result


def download_historical_data_sync(
    call: Callable[..., dict[str, Any]], symbol_token: str, exchange: str, interval: str,
    start_date: datetime, end_date: datetime,
) -> list[dict[str, Any]]:
    """Synchronous adapter for FastAPI BackgroundTasks and CLI callers."""
    return asyncio.run(download_historical_data(call, symbol_token, exchange, interval, start_date, end_date))


def fetch_and_save_historical_candles(
    call: Callable[..., dict[str, Any]],
    symbol_token: str,
    exchange: str,
    interval: str = "FIVE_MINUTE",
    days: int = 5,
) -> list[dict[str, Any]]:
    """Fetch Angel history and persist it for backtests.

    ``exchange`` may be an Angel exchange (NSE/NFO/BSE/BFO) or an internal
    segment (nse_cm/nse_fo/bse_cm/bse_fo). The public helper accepts Angel's
    interval names while the storage layer uses the platform's short names.
    """
    segment = exchange.lower() if exchange.lower() in SEGMENT_EXCHANGE else {
        "NSE": "nse_cm", "NFO": "nse_fo", "BSE": "bse_cm", "BFO": "bse_fo",
    }.get(exchange.upper(), exchange.lower())
    platform_interval = next((key for key, value in INTERVAL_MAP.items() if value == interval), interval)
    if platform_interval not in INTERVAL_MAP:
        raise CandleError(f"Unsupported Angel interval '{interval}'")
    candles = fetch(call, str(symbol_token), segment, platform_interval, days=max(1, int(days)))
    stored = store(str(symbol_token), segment, platform_interval, candles)
    log.info("Saved %s historical candles for %s/%s (%s rows)", stored, symbol_token, segment, len(candles))
    return candles


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
    """Return the current candle built from WebSocket ticks; never poll history."""
    del call
    candles = ticks.history(token, exchange_segment, interval, limit=1)
    return candles[-1] if candles else None


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
