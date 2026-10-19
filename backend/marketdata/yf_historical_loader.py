"""Durable historical OHLCV loader for backtests.

yfinance is the normal source for historical NSE/BSE data.  Broker APIs remain
live-data/order-execution integrations.  Angel One is used here only as a
rate-limited last-resort historical fallback when Yahoo has no usable series.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from backend.core.database import app_cursor
from backend.marketdata import ticks

log = logging.getLogger("alphatrade.yfinance")

# Well-known indices cannot be inferred by appending .NS/.BO.
YF_TICKERS: dict[str, str] = {
    "26000": "^NSEI", "NIFTY": "^NSEI", "NIFTY 50": "^NSEI", "^NSEI": "^NSEI",
    "26009": "^NSEBANK", "BANKNIFTY": "^NSEBANK", "NIFTY BANK": "^NSEBANK",
    "265": "^BSESN", "SENSEX": "^BSESN", "^BSESN": "^BSESN",
}


class HistoricalDataError(RuntimeError):
    """Neither yfinance nor the optional broker fallback returned candles."""


def _instrument(symbol: str, exchange_segment: str | None) -> dict[str, Any]:
    """Resolve an internal token or trading symbol to its durable identifier."""
    with app_cursor() as cursor:
        if exchange_segment:
            cursor.execute(
                """SELECT token, exchange_segment, symbol, trading_symbol, name, is_index
                   FROM instruments WHERE token = ? AND exchange_segment = ?""",
                (str(symbol), exchange_segment),
            )
        else:
            cursor.execute(
                """SELECT token, exchange_segment, symbol, trading_symbol, name, is_index
                   FROM instruments
                   WHERE token = ? OR symbol = ? OR trading_symbol = ?
                   ORDER BY is_index DESC LIMIT 1""",
                (str(symbol), str(symbol), str(symbol)),
            )
        row = cursor.fetchone()
    if row:
        return dict(row)
    # Accept a yfinance ticker or a plain NSE/BSE symbol even before a master
    # sync; callers still get a stable key in the common candle table.
    return {
        "token": str(symbol), "exchange_segment": exchange_segment or "nse_cm",
        "symbol": str(symbol), "trading_symbol": str(symbol), "name": str(symbol), "is_index": 0,
    }


def resolve_yf_ticker(symbol: str, exchange_segment: str | None = None) -> tuple[dict[str, Any], str]:
    """Map an internal token/symbol to its Yahoo Finance ticker."""
    instrument = _instrument(symbol, exchange_segment)
    candidates = (instrument["token"], instrument["symbol"], instrument["trading_symbol"], instrument["name"])
    for candidate in candidates:
        key = str(candidate or "").strip().upper()
        if key in YF_TICKERS:
            return instrument, YF_TICKERS[key]

    raw = str(instrument["trading_symbol"] or instrument["symbol"]).strip().upper()
    if raw.startswith("^") or raw.endswith((".NS", ".BO")):
        return instrument, raw
    # Equity master symbols can include an NSE series suffix such as RELIANCE-EQ.
    raw = raw.split("-")[0]
    suffix = ".BO" if str(instrument["exchange_segment"]).lower().startswith("bse") else ".NS"
    return instrument, f"{raw}{suffix}"


def _normalise_frame(frame: Any, interval: str) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    # Recent yfinance versions return a two-level column index even for one
    # ticker. The price field is always the first level in that layout.
    columns = getattr(frame, "columns", None)
    if getattr(columns, "nlevels", 1) > 1:
        frame = frame.copy()
        frame.columns = columns.get_level_values(0)
    rows: list[dict[str, Any]] = []
    seconds = ticks.INTERVAL_SECONDS[interval]
    for stamp, row in frame.iterrows():
        try:
            moment = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            epoch = moment.timestamp()
            open_, high, low, close = (float(row[key]) for key in ("Open", "High", "Low", "Close"))
            volume = float(row.get("Volume", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if any(value != value for value in (open_, high, low, close)):  # NaN
            continue
        rows.append({
            "time": int(epoch // seconds) * seconds, "open": open_, "high": high,
            "low": low, "close": close, "volume": volume, "trades": 0,
        })
    # Yahoo may return overlapping timestamps at range boundaries.
    return list({row["time"]: row for row in rows}.values())


def _download(ticker: str, interval: str, start_date: date | datetime | str, end_date: date | datetime | str) -> list[dict[str, Any]]:
    try:
        import yfinance as yf
    except ImportError as error:  # makes a deployment mistake actionable
        raise HistoricalDataError("yfinance is not installed; install backend requirements") from error
    try:
        frame = yf.download(
            ticker, start=start_date, end=end_date, interval=interval,
            auto_adjust=False, progress=False, actions=False, threads=False,
        )
    except Exception as error:  # network/Yahoo errors are eligible for fallback
        raise HistoricalDataError(f"yfinance request for {ticker} failed: {error}") from error
    return _normalise_frame(frame, interval)


def download_yf_data(
    symbol: str,
    interval: str,
    start_date: date | datetime | str,
    end_date: date | datetime | str,
    *,
    exchange_segment: str | None = None,
    angel_call: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Download and persist OHLCV in the shared ``candles`` schema.

    ``angel_call`` is deliberately injected by the API boundary.  This module
    never creates a broker session and therefore cannot affect live ticks or
    execution.  When supplied it is only used after Yahoo returns no data.
    """
    if interval not in ticks.INTERVAL_SECONDS:
        raise ValueError(f"Unsupported interval {interval!r}")
    instrument, ticker = resolve_yf_ticker(symbol, exchange_segment)
    try:
        candles = _download(ticker, interval, start_date, end_date)
    except HistoricalDataError as error:
        log.warning("yfinance historical download failed for %s (%s): %s", symbol, ticker, error)
        candles = []
    if candles:
        ticks.upsert_candles(instrument["token"], instrument["exchange_segment"], interval, [
            (instrument["token"], instrument["exchange_segment"], interval,
             candle["time"] // ticks.INTERVAL_SECONDS[interval], candle["open"], candle["high"],
             candle["low"], candle["close"], candle["volume"], datetime.now().timestamp())
            for candle in candles
        ])
        return candles

    log.warning("yfinance returned no historical data for %s (%s); trying Angel fallback", symbol, ticker)
    if angel_call is not None:
        from backend.angel import candles as angel_candles
        try:
            start = _as_datetime(start_date)
            end = _as_datetime(end_date)
            fallback = angel_candles.fetch(
                angel_call, instrument["token"], instrument["exchange_segment"], interval, start=start, end=end,
            )
            if fallback:
                angel_candles.store(instrument["token"], instrument["exchange_segment"], interval, fallback)
                return fallback
        except Exception as error:  # loader must report a useful no-history result
            log.warning("Angel historical fallback failed for %s: %s", symbol, error)
    raise HistoricalDataError(f"No historical data available for {symbol} ({ticker})")


def _as_datetime(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)


def default_backtest_window(interval: str) -> tuple[datetime, datetime]:
    """Stay within Yahoo's intraday availability while giving daily tests depth."""
    end = datetime.now(timezone.utc)
    days = 59 if interval in {"1m", "3m", "5m", "15m", "30m", "60m"} else 3650
    return end - timedelta(days=days), end
