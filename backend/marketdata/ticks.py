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

    # 1. Save raw ticks to ticks table
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
    except Exception as error:
        import traceback
        log.error("Tick write failed (%d rows dropped): %s", len(batch), error)
        traceback.print_exc()
        return 0

    # 2. Real-time candle aggregation: group batch by (token, segment, 5m_bucket)
    try:
        # 5m interval is 300 seconds
        five_min_sec = INTERVAL_SECONDS["5m"]
        candle_buckets: dict[tuple[str, str, int], list[tuple[float, float, float]]] = {}

        for row in batch:
            token, segment, ts, last_price, _, _, _, _, volume, _, _, _, _ = row
            if last_price is None:
                continue
            bucket = int(ts // five_min_sec)
            candle_buckets.setdefault((str(token), segment, bucket), []).append((ts, last_price, volume or 0.0))

        if candle_buckets:
            candle_rows: list[tuple[Any, ...]] = []
            now_ts = time.time()
            for (tok, seg, bkt), tick_list in candle_buckets.items():
                tick_list.sort(key=lambda x: x[0])
                prices = [x[1] for x in tick_list]
                volumes = [x[2] for x in tick_list]
                candle_rows.append((
                    tok, seg, "5m", bkt,
                    prices[0], max(prices), min(prices), prices[-1],
                    volumes[-1] if volumes else 0.0,
                    len(tick_list),
                    now_ts,
                ))

            with market_cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO candles (token, exchange_segment, interval, bucket, open, high, low, close, volume, trades, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(token, exchange_segment, interval, bucket) DO UPDATE SET
                        high   = MAX(candles.high, excluded.high),
                        low    = MIN(candles.low, excluded.low),
                        close  = excluded.close,
                        volume = excluded.volume,
                        trades = candles.trades + excluded.trades,
                        updated_at = excluded.updated_at
                    """,
                    candle_rows,
                )
    except Exception as error:
        import traceback
        log.error("Live 5-minute candle aggregation failed: %s", error)
        traceback.print_exc()

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
    except Exception as error:
        import traceback
        log.error("Candle upsert failed for %s %s: %s", token, interval, error)
        traceback.print_exc()
        return 0
    return len(rows)


def build_candles(token: str, exchange_segment: str, interval: str, *, since: float | None = None) -> list[dict[str, Any]]:
    """Aggregate every recorded tick for one instrument into OHLCV candles with accurate epoch timestamps."""
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"Unsupported interval {interval!r}; expected one of {sorted(INTERVAL_SECONDS)}")

    interval_sec = INTERVAL_SECONDS[interval]
    floor = _bucket(since, interval) * interval_sec if since else 0

    try:
        # First check pre-aggregated candles table
        with market_cursor() as cursor:
            cursor.execute(
                """
                SELECT bucket, open, high, low, close, volume, trades
                FROM candles
                WHERE token = ? AND exchange_segment = ? AND interval = ? AND (bucket * ?) >= ?
                ORDER BY bucket ASC
                """,
                (str(token), exchange_segment, interval, interval_sec, floor),
            )
            stored_rows = cursor.fetchall()
            if stored_rows:
                return [
                    {
                        "time": int(r["bucket"] * interval_sec),  # Convert bucket index to epoch seconds
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["volume"] or 0),
                        "trades": int(r["trades"] or 0),
                    }
                    for r in stored_rows
                ]
    except Exception as error:
        import traceback
        log.warning("Reading pre-aggregated candles failed, falling back to raw ticks: %s", error)
        traceback.print_exc()

    # Fallback to aggregating directly from ticks table
    try:
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
    except Exception as error:
        import traceback
        log.error("Reading ticks failed for %s %s: %s", token, exchange_segment, error)
        traceback.print_exc()
        return []

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
            "time": int(bucket * interval_sec),  # Fix: convert bucket to epoch timestamp in seconds!
            "open": float(prices[0]),
            "high": float(max(prices)),
            "low": float(min(prices)),
            "close": float(prices[-1]),
            "volume": float(volumes[-1] if volumes else 0),
            "trades": len(bucket_ticks),
        })
    return candles


def history(token: str, exchange_segment: str, interval: str, limit: int = 500) -> list[dict[str, Any]]:
    """Recorded candles, newest last. Empty means 'not enough recorded data'."""
    candles = build_candles(token, exchange_segment, interval)
    return candles[-max(1, min(limit, 5000)):]


def stored_history(
    token: str,
    exchange_segment: str,
    interval: str,
    limit: int = 5000,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Read persisted OHLCV bars, including yfinance/Angel backfills.

    This deliberately does not replace :func:`history`: the latter is the
    live-tape view built from ticks.  Backtests need the durable candle store,
    whose rows may predate the process starting.
    """
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"Unsupported interval {interval!r}; expected one of {sorted(INTERVAL_SECONDS)}")
    capped_limit = max(1, min(int(limit), 50_000))
    seconds = INTERVAL_SECONDS[interval]
    with market_cursor() as cursor:
        cursor.execute(
            """
            SELECT bucket, open, high, low, close, volume, trades
            FROM candles
            WHERE token = ? AND exchange_segment = ? AND interval = ?
              AND (? IS NULL OR bucket * ? >= ?)
              AND (? IS NULL OR bucket * ? <= ?)
            ORDER BY bucket DESC
            LIMIT ?
            """
            , (str(token), exchange_segment, interval, start_ts, seconds, start_ts,
               end_ts, seconds, end_ts, capped_limit),
        )
        rows = cursor.fetchall()
    return [
        {
            "time": row["bucket"] * seconds,
            "open": row["open"], "high": row["high"], "low": row["low"],
            "close": row["close"], "volume": row["volume"] or 0,
            "trades": row["trades"] or 0,
        }
        for row in reversed(rows)
    ]


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
