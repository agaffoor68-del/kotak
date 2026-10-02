"""Tick persistence and OHLCV candle construction.

**Why this exists.** Kotak Neo exposes *no* historical candle endpoint — quotes
are live-only. A backtester can therefore only be honest if the platform records
the tape itself. Every incoming quote is written to a `ticks` table, and candles
are aggregated from those ticks on read. The result is real recorded market
data: available for the period the platform has been running, and explicitly
absent before that, rather than invented from a third-party sample.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable

from backend.core.config import settings
from backend.core.database import market_cursor

log = logging.getLogger("alphatrade.ticks")

#: Seconds per supported interval. Keys must match the frontend's timeframe list.
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1d": 86400,
}

_batch_lock = threading.Lock()
_pending: list[tuple[Any, ...]] = []
_last_flush = time.time()
FLUSH_INTERVAL = 1.0
MAX_BATCH = 2000


def record_quote(quote) -> None:
    """Queue one quote for persistence. Cheap: the write is batched."""
    if not settings.record_ticks or quote.last is None:
        return
    row = (
        quote.token, quote.exchange_segment, time.time(),
        quote.last, quote.open, quote.high, quote.low, quote.last,
        quote.volume, quote.open_interest, quote.bid, quote.ask, quote.change_percent,
    )
    with _batch_lock:
        _pending.append(row)
        if len(_pending) >= MAX_BATCH:
            _flush_locked()


def record_quotes(quotes: Iterable[Any]) -> None:
    for quote in quotes:
        record_quote(quote)


def flush() -> int:
    with _batch_lock:
        return _flush_locked()


def _flush_locked() -> int:
    global _last_flush
    if not _pending:
        _last_flush = time.time()
        return 0
    batch, _pending[:] = list(_pending), []
    try:
        with market_cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO ticks (token, exchange_segment, ts, last_price, open, high, low,
                                   close, volume, oi, bid, ask, change_pct)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                batch,
            )
    except Exception as error:  # noqa: BLE001 - recording must never break the feed
        log.warning("Tick write failed (%d rows dropped): %s", len(batch), error)
        return 0
    _last_flush = time.time()
    return len(batch)


def maybe_flush() -> None:
    if time.time() - _last_flush >= FLUSH_INTERVAL:
        flush()


# ------------------------------------------------------------------ candles


def _bucket(ts: float, interval: str) -> int:
    return int(ts // INTERVAL_SECONDS[interval])


def upsert_candles(token: str, exchange_segment: str, interval: str, rows: list[tuple[Any, ...]]) -> int:
    """Aggregate recorded ticks into the `candles` table for one interval."""
    if not rows:
        return 0
    try:
        with market_cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO candles (token, exchange_segment, interval, bucket, open, high, low, close, volume, trades, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,1,?)
                ON CONFLICT(token, exchange_segment, interval, bucket) DO UPDATE SET
                    high   = MAX(candles.high, excluded.high),
                    low    = MIN(candles.low, excluded.low),
                    close  = excluded.close,
                    volume = excluded.volume,
                    trades = candles.trades + 1,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
    except Exception as error:  # noqa: BLE001
        log.warning("Candle upsert failed for %s %s: %s", token, interval, error)
        return 0
    return len(rows)


def build_candles(token: str, exchange_segment: str, interval: str, *, since: float | None = None) -> list[dict[str, Any]]:
    """Aggregate every recorded tick for one instrument into OHLCV candles."""
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"Unsupported interval {interval!r}; expected one of {sorted(INTERVAL_SECONDS)}")

    floor = _bucket(since, interval) * INTERVAL_SECONDS[interval] if since else 0
    with market_cursor() as cursor:
        cursor.execute(
            """
            SELECT ts, last_price, volume
            FROM ticks
            WHERE token = ? AND exchange_segment = ? AND ts >= ?
            ORDER BY ts ASC
            """,
            (str(token), exchange_segment, floor),
        )
        ticks = cursor.fetchall()

    if not ticks:
        return []

    grouped: dict[int, list[Any]] = {}
    for tick in ticks:
        grouped.setdefault(_bucket(tick["ts"], interval), []).append(tick)

    candles: list[dict[str, Any]] = []
    for bucket in sorted(grouped):
        bucket_ticks = grouped[bucket]
        prices = [t["last_price"] for t in bucket_ticks if t["last_price"] is not None]
        if not prices:
            continue
        volumes = [t["volume"] for t in bucket_ticks if t["volume"] is not None]
        candles.append({
            "time": bucket,
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
            "volume": volumes[-1] if volumes else 0,
            "trades": len(bucket_ticks),
        })
    return candles


def history(token: str, exchange_segment: str, interval: str, limit: int = 500) -> list[dict[str, Any]]:
    """Recorded candles, newest last. Empty means 'not enough recorded data'."""
    candles = build_candles(token, exchange_segment, interval)
    return candles[-max(1, min(limit, 5000)):]


def coverage(token: str, exchange_segment: str) -> dict[str, Any]:
    """How much real history exists for an instrument — surfaced in the UI."""
    with market_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS ticks, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM ticks WHERE token = ? AND exchange_segment = ?",
            (str(token), exchange_segment),
        )
        row = cursor.fetchone()
    ticks = int(row["ticks"] or 0)
    first = row["first_ts"]
    last = row["last_ts"]
    return {
        "ticks": ticks,
        "first_tick": first,
        "last_tick": last,
        "span_seconds": round(last - first, 1) if first and last else 0,
        "has_history": ticks > 0,
        "note": (
            "History is recorded live by this platform; Kotak Neo provides no historical candles."
            if ticks else
            "No ticks recorded yet. Start the feed, then history builds from this moment."
        ),
    }


def supported_intervals() -> list[str]:
    return list(INTERVAL_SECONDS)


def purge_old_ticks() -> int:
    """Drop ticks past the retention window; candles are kept."""
    cutoff = time.time() - settings.tick_retention_days * 86400
    try:
        with market_cursor() as cursor:
            cursor.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,))
            return cursor.rowcount
    except Exception as error:  # noqa: BLE001
        log.warning("Tick purge failed: %s", error)
        return 0
