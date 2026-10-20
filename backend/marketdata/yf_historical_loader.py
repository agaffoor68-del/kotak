"""Durable historical OHLCV loader for backtests.

yfinance is the normal source for historical NSE/BSE data.  Broker APIs remain
live-data/order-execution integrations.  Angel One is used here only as a
rate-limited last-resort historical fallback when Yahoo has no usable series.

**Token discipline.** The loader never invents a symbol. Whatever token the
caller sends is the token the rows are stored under, so the backtest query —
which looks the data up by that same token — always finds what was written.

**Clock discipline.** Every bar is an epoch second and every date the user
types is an IST date (see :mod:`backend.marketdata.timeutils`). A naive
timestamp coming from a market feed is an IST wall-clock reading; treating it
as UTC would shift Indian market data by five and a half hours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from backend.core.database import app_cursor
from backend.marketdata import ticks
from backend.marketdata.timeutils import IST, epoch, parse_ist, to_ist

log = logging.getLogger("alphatrade.yfinance")

# Well-known indices cannot be inferred by appending .NS/.BO.
# Keys are compared upper-cased, so both the Kotak master token (26000) and
# the Angel One index token (99926000) resolve to the same Yahoo ticker.
YF_TICKERS: dict[str, str] = {
    "26000": "^NSEI", "99926000": "^NSEI",
    "NIFTY": "^NSEI", "NIFTY 50": "^NSEI", "NIFTY50": "^NSEI", "NIFTY 50 INDEX": "^NSEI",
    "^NSEI": "^NSEI",
    "26009": "^NSEBANK", "99926009": "^NSEBANK",
    "BANKNIFTY": "^NSEBANK", "NIFTY BANK": "^NSEBANK", "NIFTYBANK": "^NSEBANK",
    "^NSEBANK": "^NSEBANK",
    "265": "^BSESN", "99919000": "^BSESN",
    "SENSEX": "^BSESN", "^BSESN": "^BSESN",
}

#: Angel One index tokens are the ``999``-prefixed form of the master token.
ANGEL_INDEX_PREFIX = "999"


class HistoricalDataError(RuntimeError):
    """Neither yfinance nor the optional broker fallback returned candles."""


@dataclass(frozen=True)
class ResolvedSymbol:
    """What a caller-supplied token means to every data source.

    ``token`` is echoed back exactly as it was received: it is the key the
    candles are stored under and the key the backtest query reads them with.
    """

    token: str
    exchange_segment: str
    yf_ticker: str
    angel_token: str
    label: str
    is_index: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "exchange_segment": self.exchange_segment,
            "yf_ticker": self.yf_ticker,
            "angel_token": self.angel_token,
            "label": self.label,
            "is_index": self.is_index,
        }


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
    # The token the caller sent is checked first: "99926000" must resolve to
    # "^NSEI" even when the instrument master has not been synced yet.
    candidates = (
        str(symbol),
        instrument["token"], instrument["symbol"], instrument["trading_symbol"], instrument["name"],
    )
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


def resolve_symbol(symbol: str, exchange_segment: str | None = None) -> ResolvedSymbol:
    """Resolve a token/symbol into everything the downloader needs.

    The returned ``token`` is exactly the token the caller sent, so the rows
    land in the database under the key the backtest query will look for.
    """
    token = str(symbol).strip()
    instrument = _instrument(token, exchange_segment)
    _, ticker = resolve_yf_ticker(token, exchange_segment)

    segment = str(
        exchange_segment
        or instrument.get("exchange_segment")
        or "nse_cm"
    )
    is_index = bool(int(instrument.get("is_index") or 0))
    if token.startswith(ANGEL_INDEX_PREFIX) and token.isdigit():
        is_index = True

    # Angel One quotes indices with the 999-prefixed token; everything else
    # uses the token as-is.
    angel_token = token
    if is_index and token.isdigit() and not token.startswith(ANGEL_INDEX_PREFIX):
        angel_token = f"{ANGEL_INDEX_PREFIX}{token}"

    label = str(
        instrument.get("name")
        or instrument.get("symbol")
        or token
    )
    return ResolvedSymbol(
        token=token,
        exchange_segment=segment,
        yf_ticker=ticker,
        angel_token=angel_token,
        label=label,
        is_index=is_index,
    )


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
            # A timezone-aware stamp is converted to IST before it is turned
            # into an epoch second, so the bar keeps its absolute instant.
            # A naive stamp is an IST wall-clock reading, not UTC.
            stamp_seconds = epoch(moment)
            open_, high, low, close = (float(row[key]) for key in ("Open", "High", "Low", "Close"))
            volume = float(row.get("Volume", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if any(value != value for value in (open_, high, low, close)):  # NaN
            continue
        rows.append({
            "time": int(stamp_seconds // seconds) * seconds, "open": open_, "high": high,
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


def store_candles(
    token: str,
    exchange_segment: str,
    interval: str,
    candles: list[dict[str, Any]],
) -> int:
    """Persist candles under *exactly* the token and segment given."""
    if not candles:
        return 0
    seconds = ticks.INTERVAL_SECONDS[interval]
    updated_at = datetime.now(tz=timezone.utc).timestamp()
    rows = [
        (
            str(token), exchange_segment, interval,
            int(candle["time"] // seconds), candle["open"], candle["high"],
            candle["low"], candle["close"], candle["volume"], updated_at,
        )
        for candle in candles
    ]
    return ticks.upsert_candles(str(token), exchange_segment, interval, rows)


def download_yf_data(
    symbol: str,
    interval: str,
    start_date: date | datetime | str,
    end_date: date | datetime | str,
    *,
    exchange_segment: str | None = None,
    angel_call: Callable[..., dict[str, Any]] | None = None,
    store_token: str | None = None,
    store_segment: str | None = None,
) -> list[dict[str, Any]]:
    """Download and persist OHLCV in the shared ``candles`` schema.

    ``angel_call`` is deliberately injected by the API boundary.  This module
    never creates a broker session and therefore cannot affect live ticks or
    execution.  When supplied it is only used after Yahoo returns no data.

    ``store_token`` / ``store_segment`` override where the rows are written.
    The default is the resolved instrument, but the API passes the caller's
    own token so the backtest query finds the data under the same key.
    """
    if interval not in ticks.INTERVAL_SECONDS:
        raise ValueError(f"Unsupported interval {interval!r}")
    resolved = resolve_symbol(symbol, exchange_segment)
    try:
        candles = _download(resolved.yf_ticker, interval, start_date, end_date)
    except HistoricalDataError as error:
        log.warning("yfinance historical download failed for %s (%s): %s", symbol, resolved.yf_ticker, error)
        candles = []
    if candles:
        store_candles(
            store_token or resolved.token,
            store_segment or resolved.exchange_segment,
            interval,
            candles,
        )
        return candles

    log.warning(
        "yfinance returned no historical data for %s (%s); trying Angel fallback",
        symbol, resolved.yf_ticker,
    )
    if angel_call is not None:
        from backend.angel import candles as angel_candles
        try:
            start = _as_datetime(start_date)
            end = _as_datetime(end_date)
            fallback = angel_candles.fetch(
                angel_call, resolved.angel_token, resolved.exchange_segment, interval,
                start=start, end=end,
            )
            if fallback:
                angel_candles.store(
                    store_token or resolved.token,
                    store_segment or resolved.exchange_segment,
                    interval, fallback,
                )
                return fallback
        except Exception as error:  # loader must report a useful no-history result
            log.warning("Angel historical fallback failed for %s: %s", symbol, error)
    raise HistoricalDataError(f"No historical data available for {symbol} ({resolved.yf_ticker})")


def download_history(
    symbol_token: str,
    exchange_segment: str,
    interval: str,
    start: datetime,
    end: datetime,
    *,
    angel_call: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Download a window and store it under the caller's exact token.

    yfinance is tried first; Angel One is the rate-limited fallback when
    Yahoo has no series for the instrument. Returns a summary of what was
    written, so the API can answer the UI without a second round trip.
    """
    if interval not in ticks.INTERVAL_SECONDS:
        raise ValueError(f"Unsupported interval {interval!r}")

    # The window is IST, whether it arrives as a string, a date or a datetime.
    start = parse_ist(start)
    end = parse_ist(end)
    if end <= start:
        raise ValueError("start_date must be before end_date")

    resolved = resolve_symbol(symbol_token, exchange_segment)
    source = "yfinance"
    candles: list[dict[str, Any]] = []

    try:
        candles = _download(resolved.yf_ticker, interval, start, end)
    except HistoricalDataError as error:
        log.warning(
            "yfinance historical download failed for %s (%s): %s",
            resolved.token, resolved.yf_ticker, error,
        )

    if not candles and angel_call is not None:
        from backend.angel import candles as angel_candles
        try:
            fallback = angel_candles.fetch(
                angel_call, resolved.angel_token, resolved.exchange_segment, interval,
                start=_as_datetime(start), end=_as_datetime(end),
            )
        except Exception as error:  # noqa: BLE001 - the fallback must not mask the real cause
            log.warning("Angel historical fallback failed for %s: %s", resolved.token, error)
            fallback = []
        if fallback:
            candles = fallback
            source = "angel_one"

    if not candles:
        raise HistoricalDataError(
            f"No historical data available for {resolved.token} ({resolved.yf_ticker})"
        )

    stored = store_candles(resolved.token, resolved.exchange_segment, interval, candles)
    log.info(
        "Downloaded %d rows for %s from %s to %s",
        len(candles), resolved.token, start, end,
    )
    return {
        "symbol_token": resolved.token,
        "symbol": resolved.label,
        "yf_ticker": resolved.yf_ticker,
        "angel_token": resolved.angel_token,
        "exchange_segment": resolved.exchange_segment,
        "interval": interval,
        "source": source,
        "rows": len(candles),
        "stored": stored,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "first_bar": candles[0]["time"] if candles else None,
        "last_bar": candles[-1]["time"] if candles else None,
    }


def _as_datetime(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        return to_ist(value)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=IST)
    return to_ist(datetime.fromisoformat(str(value)))


def default_backtest_window(interval: str) -> tuple[datetime, datetime]:
    """Stay within Yahoo's intraday availability while giving daily tests depth."""
    end = datetime.now(tz=timezone.utc)
    days = 59 if interval in {"1m", "3m", "5m", "15m", "30m", "60m"} else 3650
    return end - timedelta(days=days), end
