"""Backtest engine.

Production-ready historical backtester for the AlphaTradePro platform.

Core entry point: :func:`run_backtest`. Given an OHLCV DataFrame and strategy
parameters it simulates a **VWAP bounce + EMA trend** strategy with fixed
take-profit / stop-loss and forced end-of-day square-off, returning trade-level
and aggregate metrics.

The engine never invents prices. Historical candles are read from the shared
``candles`` table (populated by the Historical Loader / Angel One) via
:func:`load_candles`, and the API refuses to run when no data covers the
requested symbol, timeframe and date range.

Indicators are calculated with :mod:`pandas_ta` when installed (VWAP is reset
each trading day); a pure-pandas fallback is used otherwise so the engine
stays runnable in any environment.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

try:  # pandas_ta is the preferred indicator engine
    import pandas_ta as ta  # type: ignore

    _HAS_PANDAS_TA = True
except Exception:  # pragma: no cover - environment without pandas_ta
    ta = None  # type: ignore
    _HAS_PANDAS_TA = False

from backend.core.database import market_cursor
from backend.marketdata import ticks
from backend.marketdata.timeutils import IST, ist_stamp, parse_ist
from backend.marketdata.yf_historical_loader import _instrument

log = logging.getLogger("alphatrade.backtest")

REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

DEFAULT_STRATEGY_PARAMS: dict[str, Any] = {
    "tp_pct": 0.5,           # fixed take-profit, percent of entry price
    "sl_pct": 0.3,           # fixed stop-loss, percent of entry price
    "ema_period": 50,        # EMA trend filter (close above EMA = uptrend)
    "rsi_period": 14,        # RSI overbought guard for longs
    "use_rsi_filter": True,  # require RSI < rsi_max_long on long entry
    "rsi_max_long": 70.0,
    "quantity": 1,           # fixed quantity per trade
    "side": "long",          # the platform models the long side
}

#: The message a backtest shows when the token has no bars in the database.
NO_DATA_ERROR = "No data in DB for token {token}. Run the downloader first."


class BacktestError(ValueError):
    """Raised when a backtest cannot be executed."""


# --------------------------------------------------------------------------- parameter handling


def _coerce_params(strategy_params: dict[str, Any] | None) -> dict[str, Any]:
    """Merge user-supplied params over the defaults and coerce types."""
    params = dict(DEFAULT_STRATEGY_PARAMS)
    if strategy_params:
        for key, default in DEFAULT_STRATEGY_PARAMS.items():
            value = strategy_params.get(key)
            if value is not None:
                params[key] = value
    params["tp_pct"] = float(params["tp_pct"])
    params["sl_pct"] = float(params["sl_pct"])
    params["ema_period"] = max(1, int(params["ema_period"]))
    params["rsi_period"] = max(1, int(params["rsi_period"]))
    params["quantity"] = max(1, int(params.get("quantity") or 1))
    params["use_rsi_filter"] = bool(params.get("use_rsi_filter", True))
    params["rsi_max_long"] = float(params.get("rsi_max_long", 70.0))
    params["side"] = str(params.get("side", "long")).lower()
    return params


def _strategy_params_from_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Extract backtest-relevant params from a Pine-converted / DSL strategy definition.

    Reads TP/SL from the ``risk`` block and indicator periods from the entry
    conditions, so a strategy imported from Pine Script trades exactly as
    specified without the user re-entering numbers.
    """
    params: dict[str, Any] = {}
    risk = definition.get("risk") or {}
    if risk.get("target_pct") is not None:
        params["tp_pct"] = float(risk["target_pct"])
    if risk.get("stop_loss_pct") is not None:
        params["sl_pct"] = float(risk["stop_loss_pct"])
    if risk.get("quantity") is not None:
        params["quantity"] = int(risk["quantity"])
    sizing = definition.get("position_sizing") or {}
    if sizing.get("quantity") is not None:
        params["quantity"] = int(sizing["quantity"])

    # Scan entry conditions for ema / rsi periods used by the strategy.
    entry = definition.get("entry") or {}
    for node in _iter_conditions(entry):
        indicator = str(node.get("indicator", "")).lower()
        p = node.get("params") or {}
        if indicator == "ema" and p.get("period"):
            params.setdefault("ema_period", int(p["period"]))
        if indicator == "rsi" and p.get("period"):
            params.setdefault("rsi_period", int(p["period"]))
    return params


def _iter_conditions(node: Any):
    """Yield every leaf condition node from an all/any/not condition tree."""
    if not isinstance(node, dict):
        return
    if "all" in node or "any" in node or "not" in node:
        key = "all" if "all" in node else ("any" if "any" in node else "not")
        children = node[key] if key != "not" else [node[key]]
        for child in children:
            yield from _iter_conditions(child)
    elif node.get("indicator"):
        yield node
# --------------------------------------------------------------------------- dataframe preparation


def _to_dataframe(candles: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert the DB candle rows into a backtest-ready DataFrame.

    Returns columns: timestamp (tz-aware UTC datetime), open, high, low,
    close, volume, plus a normalized ``date`` (IST) column used for EOD
    square-off grouping and per-day VWAP reset.
    """
    if not candles:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))

    rows = []
    for candle in candles:
        rows.append(
            {
                "timestamp": candle.get("time"),
                "open": candle.get("open"),
                "high": candle.get("high"),
                "low": candle.get("low"),
                "close": candle.get("close"),
                "volume": candle.get("volume") or 0,
            }
        )
    df = pd.DataFrame(rows)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Normalise timestamps to tz-aware UTC datetimes.
    ts = df["timestamp"]
    if pd.api.types.is_numeric_dtype(ts):
        # epoch seconds -> UTC
        df["timestamp"] = pd.to_datetime(ts, unit="s", utc=True)
    else:
        df["timestamp"] = pd.to_datetime(ts, utc=True, errors="coerce")

    # Drop fully-invalid bars.
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if df.empty:
        return df

    # IST calendar date (UTC + 5:30): used for per-day VWAP reset and EOD square-off.
    # Computed from UTC epoch seconds to stay robust across pandas tz quirks.
    epoch_seconds = (
        df["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None).astype("int64")
        // 1_000_000_000
    )
    df["date"] = [
        datetime.fromtimestamp(int(s) + 19800, tz=timezone.utc).date()
        for s in epoch_seconds
    ]
    # Mark the last bar of each IST day so the simulator can square off.
    last_idx = df.groupby("date", sort=False).tail(1).index
    df["is_last_of_day"] = False
    df.loc[last_idx, "is_last_of_day"] = True
    return df


def _find_col(df: pd.DataFrame, prefix: str) -> str | None:
    """Return the first DataFrame column whose lower-cased name starts with prefix."""
    prefix = prefix.lower()
    for col in df.columns:
        if str(col).lower().startswith(prefix):
            return col
    return None


def _add_indicators(df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    """Calculate VWAP, EMA and RSI on the DataFrame (pandas_ta, with pure-pandas fallback).

    VWAP is reset at the start of every trading day (standard intraday VWAP),
    which is what a VWAP-bounce strategy needs. EMA and RSI run over the full
    series.
    """
    df = df.copy()
    ema_col = f"EMA_{params['ema_period']}"
    rsi_col = f"RSI_{params['rsi_period']}"

    if _HAS_PANDAS_TA:
        try:
            # VWAP per trading day via pandas_ta (reset each day).
            vwap_series = df.groupby("date", group_keys=False).apply(
                lambda g: ta.vwap(
                    high=g["high"], low=g["low"], close=g["close"], volume=g["volume"]
                )
            )
            df["VWAP"] = vwap_series.reindex(df.index)
            # EMA / RSI over the full series via pandas_ta.
            df[ema_col] = ta.ema(close=df["close"], length=params["ema_period"])
            df[rsi_col] = ta.rsi(close=df["close"], length=params["rsi_period"])
            log.debug(
                "Indicators computed with pandas_ta (ema=%s, rsi=%s)",
                params["ema_period"],
                params["rsi_period"],
            )
            return df
        except Exception as error:  # pragma: no cover - pandas_ta runtime issue
            log.warning(
                "pandas_ta indicator calculation failed (%s); using pure-pandas fallback",
                error,
            )

    # ------------------------------------------------------------------ pure-pandas fallback
    # Standard VWAP: cumulative (typical_price * volume) / cumulative volume, reset per day.
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    df["VWAP"] = (
        (typical * df["volume"]).groupby(df["date"]).cumsum()
        / df["volume"].groupby(df["date"]).cumsum()
    )
    # EMA (recursive, adjust=False matches the common convention).
    df[ema_col] = df["close"].ewm(span=params["ema_period"], adjust=False).mean()
    # Wilder's RSI.
    period = params["rsi_period"]
    delta = df["close"].diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    roll_up = up.ewm(alpha=1.0 / period, min_periods=period).mean()
    roll_down = down.ewm(alpha=1.0 / period, min_periods=period).mean()
    rs = roll_up / roll_down.replace(0, pd.NA)
    df[rsi_col] = 100.0 - (100.0 / (1.0 + rs))
    log.debug(
        "Indicators computed with pure-pandas fallback (ema=%s, rsi=%s)",
        params["ema_period"],
        params["rsi_period"],
    )
    return df


def _as_float(value: Any) -> float | None:
    """Coerce a cell to float, returning None for NaN / missing."""
    try:
        if value is None:
            return None
        result = float(value)
        if result != result:  # NaN check
            return None
        return result
    except (TypeError, ValueError):
        return None
# --------------------------------------------------------------------------- core simulation


def run_backtest(df: pd.DataFrame, strategy_params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Simulate a VWAP-bounce + EMA-trend strategy over OHLCV data.

    Parameters
    ----------
    df:
        DataFrame containing ``timestamp, open, high, low, close, volume``.
        ``timestamp`` may be epoch seconds, an ISO string or a datetime.
    strategy_params:
        Backtest parameters. Supported keys (all optional):

        * ``tp_pct``            — fixed take-profit in percent (default 0.5)
        * ``sl_pct``            — fixed stop-loss in percent (default 0.3)
        * ``ema_period``        — EMA trend period (default 50)
        * ``rsi_period``        — RSI period (default 14)
        * ``use_rsi_filter``    — require RSI < rsi_max_long on long entry (default True)
        * ``rsi_max_long``      — RSI ceiling for long entries (default 70)
        * ``quantity``          — fixed quantity per trade (default 1)
        * ``side``              — ``"long"`` (platform models the long side)

    Returns
    -------
    dict
        ``{"total_trades", "win_rate", "total_pnl", "max_drawdown", "trades"}``
        plus ``equity_curve`` and ``parameters`` for debugging.
    """
    params = _coerce_params(strategy_params)

    if df is None or len(df) == 0:
        return _empty_result(params, "No data to backtest")

    records = df.to_dict("records") if isinstance(df, pd.DataFrame) else df
    work = _to_dataframe(records)
    if work.empty:
        return _empty_result(params, "No valid bars after parsing")

    work = _add_indicators(work, params)

    vwap_col = _find_col(work, "vwap")
    ema_col = _find_col(work, "ema_")
    rsi_col = _find_col(work, "rsi")

    trades: list[dict[str, Any]] = []
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0

    position: dict[str, Any] | None = None
    prev_close: float | None = None
    prev_vwap: float | None = None

    for row in work.itertuples(index=False):
        close = _as_float(getattr(row, "close"))
        high = _as_float(getattr(row, "high"))
        low = _as_float(getattr(row, "low"))
        ts = getattr(row, "timestamp")
        is_eod = bool(getattr(row, "is_last_of_day", False))

        vwap = _as_float(getattr(row, vwap_col)) if vwap_col else None
        ema = _as_float(getattr(row, ema_col)) if ema_col else None
        rsi = _as_float(getattr(row, rsi_col)) if rsi_col else None

        # ------------------------------------------------------------------ manage open position
        if position is not None:
            exit_price: float | None = None
            exit_reason: str | None = None

            # 1) fixed stop-loss / take-profit evaluated against the bar's high/low
            if low is not None and position["stop"] is not None and low <= position["stop"]:
                exit_price = position["stop"]
                exit_reason = "sl"
            elif high is not None and position["target"] is not None and high >= position["target"]:
                exit_price = position["target"]
                exit_reason = "tp"

            # 2) strategy exit: close crosses below VWAP (trend/bounce invalidated)
            if (
                exit_price is None
                and vwap is not None
                and prev_close is not None
                and prev_vwap is not None
            ):
                if close < vwap and prev_close >= prev_vwap:
                    exit_price = close
                    exit_reason = "exit_signal"

            # 3) end-of-day square-off (exit at the day's closing price)
            if exit_price is None and is_eod:
                exit_price = close
                exit_reason = "eod"
            if exit_price is not None:
                quantity = float(position["quantity"])
                pnl = round((exit_price - position["entry_price"]) * quantity, 2)
                entry_price = position["entry_price"]
                pnl_pct = (
                    round((exit_price / entry_price - 1.0) * 100.0, 4) if entry_price else 0.0
                )
                trades.append(
                    {
                        "entry_time": _iso(position["entry_time"]),
                        "exit_time": _iso(ts),
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "quantity": quantity,
                        "side": position["side"],
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "exit_reason": exit_reason,
                    }
                )
                equity = round(equity + pnl, 2)
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, round(peak - equity, 2))
                position = None

        # ------------------------------------------------------------------ look for a new entry
        if position is None:
            # Never open a fresh position on the final bar of the day; it would
            # be squared off immediately and only adds noise.
            if not is_eod:
                entry_ok = (
                    vwap is not None
                    and ema is not None
                    and prev_close is not None
                    and prev_vwap is not None
                    and close is not None
                )
                if entry_ok and params["side"] == "long":
                    # VWAP bounce: close crosses above VWAP from below.
                    crossed_above_vwap = close > vwap and prev_close <= prev_vwap
                    # EMA trend filter: price above EMA => uptrend.
                    above_ema = close > ema
                    # RSI guard: not overbought.
                    rsi_ok = True
                    if params["use_rsi_filter"] and rsi is not None:
                        rsi_ok = rsi < params["rsi_max_long"]

                    if crossed_above_vwap and above_ema and rsi_ok:
                        entry_price = close
                        stop = round(entry_price * (1 - params["sl_pct"] / 100.0), 2)
                        target = round(entry_price * (1 + params["tp_pct"] / 100.0), 2)
                        position = {
                            "entry_price": entry_price,
                            "entry_time": ts,
                            "entry_date": getattr(row, "date", None),
                            "stop": stop,
                            "target": target,
                            "quantity": params["quantity"],
                            "side": "long",
                        }

        prev_close = close
        prev_vwap = vwap

    # ------------------------------------------------------------------ flush any position still open at the very end of data
    if position is not None and len(work) > 0:
        last_row = work.iloc[-1]
        last_close = _as_float(last_row["close"])
        last_ts = last_row["timestamp"]
        if last_close is not None:
            quantity = float(position["quantity"])
            pnl = round((last_close - position["entry_price"]) * quantity, 2)
            entry_price = position["entry_price"]
            pnl_pct = (
                round((last_close / entry_price - 1.0) * 100.0, 4) if entry_price else 0.0
            )
            trades.append(
                {
                    "entry_time": _iso(position["entry_time"]),
                    "exit_time": _iso(last_ts),
                    "entry_price": entry_price,
                    "exit_price": last_close,
                    "quantity": quantity,
                    "side": position["side"],
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "exit_reason": "end_of_data",
                }
            )
            equity = round(equity + pnl, 2)
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, round(peak - equity, 2))

    # ------------------------------------------------------------------ aggregate metrics
    total_trades = len(trades)
    winning = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = round((winning / total_trades) * 100.0, 2) if total_trades else 0.0
    total_pnl = round(sum(t["pnl"] for t in trades), 2)

    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "max_drawdown": round(max_drawdown, 2),
        "win_count": winning,
        "loss_count": total_trades - winning,
        "trades": trades,
        "equity_curve": None,
        "parameters": params,
    }


def _empty_result(params: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "total_trades": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "max_drawdown": 0.0,
        "win_count": 0,
        "loss_count": 0,
        "trades": [],
        "equity_curve": None,
        "parameters": params,
        "note": reason,
    }


def _iso(ts: Any) -> str | None:
    """ISO-8601 string (IST) for a timestamp, best-effort."""
    if ts is None:
        return None
    try:
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.astimezone(IST).isoformat()
        dt = pd.to_datetime(ts, utc=True)
        return dt.astimezone(IST).isoformat()
    except Exception:
        return str(ts)
# --------------------------------------------------------------------------- data access (DB)


def _parse_date(value: str | date | datetime | None) -> datetime:
    """Parse a user-supplied date into an **IST**-aware datetime.

    The frontend sends calendar dates ("2024-01-04"). The candles are stored
    as epoch seconds taken from IST wall-clock readings, so a date must be
    interpreted in IST as well — reading it as UTC would shift the whole
    window by five and a half hours and silently drop the session.
    """
    if value is None:
        raise BacktestError("missing date")
    try:
        return parse_ist(value)
    except ValueError as error:
        raise BacktestError(f"Unrecognised date '{value}'") from error


def resolve_instrument(symbol: str, exchange_segment: str | None = None) -> dict[str, Any]:
    """Resolve a symbol/token to a durable instrument row (reuses the historical loader)."""
    try:
        return _instrument(symbol, exchange_segment)
    except Exception as error:  # pragma: no cover - defensive
        raise BacktestError(f"Unable to resolve instrument '{symbol}': {error}") from error


def _bucket_range(interval: str, start: datetime, end: datetime) -> tuple[int, int]:
    if interval not in ticks.INTERVAL_SECONDS:
        raise BacktestError(
            f"Unsupported interval '{interval}'. Expected one of {sorted(ticks.INTERVAL_SECONDS)}"
        )
    seconds = ticks.INTERVAL_SECONDS[interval]
    start_bucket = int(start.timestamp() // seconds)
    end_bucket = int(end.timestamp() // seconds)
    return start_bucket, end_bucket


def stored_range(token: str, interval: str) -> dict[str, Any]:
    """What is actually stored for a token: bar count and the first/last bar.

    Used to explain *why* a window came back empty instead of leaving the UI
    with a bare "no historical data found".
    """
    with market_cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS bars, MIN(bucket) AS first_bucket, MAX(bucket) AS last_bucket
            FROM candles WHERE token = ? AND interval = ?
            """,
            (str(token), interval),
        )
        row = cursor.fetchone()
    bars = int(row["bars"] or 0)
    seconds = ticks.INTERVAL_SECONDS.get(interval, 300)
    first_bucket = row["first_bucket"]
    last_bucket = row["last_bucket"]
    return {
        "bars": bars,
        "first_bar": int(first_bucket) * seconds if first_bucket is not None else None,
        "last_bar": int(last_bucket) * seconds if last_bucket is not None else None,
    }


def _fetch_rows(
    tokens: list[str], segment: str | None, interval: str,
    start_bucket: int, end_bucket: int,
) -> list[dict[str, Any]]:
    """Read the bars of a window, optionally across every exchange segment."""
    if not tokens:
        return []
    placeholders = ",".join("?" for _ in tokens)
    segment_clause = " AND exchange_segment = ?" if segment else ""
    params: list[Any] = [*tokens]
    if segment:
        params.append(segment)
    params.extend([interval, start_bucket, end_bucket])
    with market_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT token, exchange_segment, bucket, open, high, low, close, volume
            FROM candles
            WHERE token IN ({placeholders}){segment_clause} AND interval = ?
              AND bucket BETWEEN ? AND ?
            ORDER BY bucket
            """,
            tuple(params),
        )
        return [dict(row) for row in cursor.fetchall()]


def load_candles(
    symbol: str,
    timeframe: str,
    from_date: str | date | datetime,
    to_date: str | date | datetime,
    *,
    exchange_segment: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load historical candles for a symbol/timeframe/date range from the DB.

    Returns ``(candles, instrument)``. Raises :class:`BacktestError` if the
    instrument cannot be resolved or the interval is unsupported. An empty
    candle list means no data covers the request.

    The lookup is deliberately forgiving about *where* the rows live but never
    about *when* they are:

    * the token the caller sent is tried first — the downloader writes under
      exactly that token, so the two always meet;
    * the token the instrument master resolves to is tried as well, so a
      trading symbol ("RELIANCE") still finds its master token;
    * if nothing matches the requested segment, the same token is looked for
      in every other segment, because a download can land under ``nse_cm``
      while a query asks for ``nse_idx``;
    * the window itself is strict: bars outside the requested IST dates are
      never returned.
    """
    instrument = resolve_instrument(symbol, exchange_segment)
    segment = instrument.get("exchange_segment") or exchange_segment or "nse_cm"

    tokens: list[str] = []
    for candidate in (str(symbol), str(instrument.get("token") or "")):
        if candidate and candidate not in tokens:
            tokens.append(candidate)

    start = _parse_date(from_date)
    end = _parse_date(to_date)
    if end < start:
        start, end = end, start
    # Include the whole end day, in IST — the same clock the bars were written with.
    end = end.replace(hour=23, minute=59, second=59, microsecond=999_999)

    start_bucket, end_bucket = _bucket_range(timeframe, start, end)

    rows = _fetch_rows(tokens, segment, timeframe, start_bucket, end_bucket)
    if not rows:
        # Same token, any exchange segment: the download may have been filed
        # under a different segment than the one being queried.
        rows = _fetch_rows(tokens, None, timeframe, start_bucket, end_bucket)
        if rows:
            found = sorted({str(row["exchange_segment"]) for row in rows})
            log.info(
                "Found %d bars for %s in segment(s) %s (requested %s)",
                len(rows), tokens[0], ", ".join(found), segment,
            )

    candles = [
        {
            "time": row["bucket"] * ticks.INTERVAL_SECONDS[timeframe],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"] or 0,
        }
        for row in rows
    ]
    log.info(f"Backtest query returned {len(candles)} rows for token {token}")
    return candles, instrument


def data_exists(
    symbol: str,
    timeframe: str,
    from_date: str | date | datetime,
    to_date: str | date | datetime,
    *,
    exchange_segment: str | None = None,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Check whether historical data covers the request.

    Returns ``(exists, instrument, error)``. When ``exists`` is False the
    caller should surface the error message to the UI. The message says which
    window was asked for and what is actually stored, so a mismatch is obvious
    instead of being a dead end.
    """
    try:
        candles, instrument = load_candles(
            symbol, timeframe, from_date, to_date, exchange_segment=exchange_segment
        )
    except BacktestError as error:
        return False, None, str(error)
    except Exception as error:  # pragma: no cover - defensive
        log.exception("data_exists check failed")
        return False, None, f"Unable to check historical data: {error}"
    if candles:
        return True, instrument, None

    window_text = f"{from_date} to {to_date}"
    stored = stored_range(str(symbol), timeframe)
    if not stored["bars"]:
        return False, instrument, NO_DATA_ERROR.format(token=symbol)
    return False, instrument, (
        f"No historical data found in DB for {symbol} between {window_text} (IST). "
        f"Stored: {stored['bars']} bars from {ist_stamp(stored['first_bar'])} "
        f"to {ist_stamp(stored['last_bar'])}."
    )
